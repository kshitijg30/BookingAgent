"""The research job: after a slot failed, search again for other slots, relaxing one constraint at a time.

The original criteria and the relaxed variants are searched at the same time (threads). The choice is fixed and
does not depend on which search returns first: the original criteria if it has results, else the first relaxed
variant (times, dates, days, budget) that has results. Only the chosen search is stored. Nothing is booked.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from booking_agent.domain.entities import Criteria
from booking_agent.domain.models import IST
from booking_agent.domain.money import fmt_rupees
from booking_agent.domain.planning import SlotKey, build_offers, relaxed_variants, search_window
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.quotes import pick_slot
from booking_agent.flow.search import SearchOutcome, failed_slots, store_search
from booking_agent.jobs.common import JobContext, Verdict, finish, run_job_turn
from booking_agent.provider.client import ProviderClient
from booking_agent.render import day_time, describe_criteria, offers_card, offers_text, safe_name
from booking_agent.rows import Job

MAX_WORKERS = 5
RELAXED_WORDS = {
    "times": "any time of day",
    "dates": "dates a week later",
    "days": "any day of the week",
    "budget": "a budget 25% higher",
}


def handle_research(context: JobContext, job: Job) -> bool:
    failed_quote_id = job.payload.get("failed_quote_id")

    def step(deps: Deps, turn: TurnContext) -> Verdict:
        return research_step(deps, turn, failed_quote_id)

    return finish(run_job_turn(context, job, step, job.payload))


def _base_criteria(deps: Deps, turn: TurnContext, failed_quote_id: str | None) -> Criteria | None:
    """The criteria to search again. A failed reschedule is restricted to the booking's own provider."""
    base = turn.search.criteria if turn.search is not None else None
    failed = deps.store.get_quote(failed_quote_id) if failed_quote_id else None
    if failed is None or failed.action != "reschedule" or failed.booking_id is None:
        return base
    booking = deps.store.get_booking(failed.booking_id)
    if booking is None:
        return base
    own = deps.store.get_search(failed.search_id) if failed.search_id else None
    start = own.criteria if own is not None else base or Criteria(service=booking.service, area=booking.area)
    return start.model_copy(
        update={"provider_id": booking.provider_id, "reschedule_booking_id": booking.id, "no_budget": True}
    )


def _search_one(client: ProviderClient, now: datetime, criteria: Criteria, excluded: set[SlotKey]) -> SearchOutcome:
    """One variant's search. Runs in a worker thread, so it touches only the provider client."""
    window = search_window(criteria, now.astimezone(IST).date())
    if window is None or criteria.service is None or criteria.area is None:
        return SearchOutcome((), "no_slots_in_window")
    answer = client.search_providers(criteria.service, criteria.area, window[0], window[1])
    if not answer.ok or answer.value is None:
        return SearchOutcome((), None, provider_failed=True)
    offers, reason = build_offers(answer.value, criteria, window, now, excluded)
    return SearchOutcome(offers, reason)


def search_variants(
    deps: Deps, turn: TurnContext, variants: list[tuple[str, Criteria]], excluded: set[SlotKey]
) -> list[SearchOutcome]:
    """Search every variant concurrently. Results come back in the order of ``variants``."""
    for _ in variants:
        turn.turn_limits.charge_tool(verifying=True)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_search_one, deps.provider_client, turn.now, crit, excluded) for _, crit in variants]
        outcomes = [future.result() for future in futures]
    turn.tracer.record(
        "research_searches",
        now=turn.now,
        variants=[name for name, _ in variants],
        results=[len(outcome.offers) for outcome in outcomes],
    )
    return outcomes


def research_step(deps: Deps, turn: TurnContext, failed_quote_id: str | None = None) -> Verdict:
    base = _base_criteria(deps, turn, failed_quote_id)
    if base is None or base.missing():
        turn.say("I couldn't search for other slots just now. Please tell me what you need again.")
        return "done"
    excluded = failed_slots(deps.store, turn.user_id, turn.conversation_id)
    if base.reschedule_booking_id is not None:
        booking = deps.store.get_booking(base.reschedule_booking_id)
        if booking is not None:
            excluded.add((booking.provider_id, booking.start))
    variants = [("original", base)] + [
        (name, crit.model_copy(update={"relaxed": [*base.relaxed, name]})) for name, crit in relaxed_variants(base)
    ]
    outcomes = search_variants(deps, turn, variants, excluded)
    if all(outcome.provider_failed for outcome in outcomes):
        return "unknown"
    chosen = next(((v, o) for v, o in zip(variants, outcomes, strict=True) if o.offers), None)
    if chosen is None:
        turn.say(
            f"I couldn't find any {base.service} in {base.area} even with wider dates, times or budget. "
            "What would you like to change?"
        )
        return "done"
    (name, criteria), outcome = chosen
    search = store_search(deps, turn, criteria, outcome.offers)
    if name == "original":
        _offer_best(deps, turn, search.id, len(outcome.offers))
    else:
        words = ", ".join(RELAXED_WORDS[item] for item in criteria.relaxed)
        turn.reply = f"Nothing matches {describe_criteria(base)}. Closest options: {words}."
        turn.attach = (offers_text(len(outcome.offers)), offers_card(search.id))
    return "done"


def _offer_best(deps: Deps, turn: TurnContext, search_id: str, count: int) -> None:
    """Hold the best slot of the fresh search and show it, with the other options on an offers card."""
    search = turn.search
    if search is None:
        return
    best = search.results[0]
    result = pick_slot(deps, turn, best.provider_id, best.slots[0].start)
    if result.status == "quoted" and result.quote is not None:
        quote = result.quote
        turn.reply = (
            f"The next best option is {safe_name(quote.provider_name)}, {day_time(quote.start)}, "
            f"{fmt_rupees(quote.price)}."
        )
        turn.attach = (offers_text(count), offers_card(search_id))
