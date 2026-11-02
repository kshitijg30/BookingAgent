"""Holding a slot and showing it as a quote, and what happens when a slot fails.

A quote is one exact action with a Confirm button. ``pick_slot`` turns a chosen (provider, slot) into a shown
quote. When the provider no longer has the slot, ``fail_slot`` records it as failed and queues a research job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from booking_agent.domain.contract import Hold
from booking_agent.domain.entities import Criteria, ProviderOffer, Quote, Search, Slot
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.untrusted import Untrusted
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.search import failed_slots
from booking_agent.render import NOTICE_CARD, day_time, quote_card, quote_text, review_lines, safe_name

PickStatus = Literal["quoted", "slot_failed", "not_offered", "busy", "provider_error", "over_budget"]


@dataclass(frozen=True)
class PickResult:
    status: PickStatus
    quote: Quote | None = None
    detail: str | None = None


def pick_slot(deps: Deps, turn: TurnContext, provider_id: str, slot_start: object) -> PickResult:
    """Hold the chosen slot and show it as a quote. Skips to the next provider if the hold costs too much."""
    search = turn.search
    if turn.quote is not None and turn.quote.status == "confirmed":
        return PickResult("busy")
    if search is None:
        return PickResult("not_offered")
    chosen = _find_slot(search, provider_id, slot_start)
    if chosen is None or (provider_id, chosen[1].start) in failed_slots(deps.store, turn.user_id, turn.conversation_id):
        return PickResult("not_offered")
    ranked = list(search.results)
    later = [(offer, offer.slots[0]) for offer in ranked[ranked.index(chosen[0]) + 1 :]]
    for offer, slot in [chosen, *later]:
        result = _try_slot(deps, turn, search, offer, slot)
        if result.status != "over_budget":
            return result
    return PickResult("over_budget")


def _find_slot(search: Search, provider_id: str, slot_start: object) -> tuple[ProviderOffer, Slot] | None:
    for offer in search.results:
        if offer.provider_id == provider_id:
            return next(((offer, slot) for slot in offer.slots if slot.start == slot_start), None)
    return None


def _try_slot(deps: Deps, turn: TurnContext, search: Search, offer: ProviderOffer, slot: Slot) -> PickResult:
    criteria = search.criteria
    if criteria.reschedule_booking_id is not None:
        return _quote_reschedule(deps, turn, search, offer, slot)
    outcome = turn.calls.hold(offer.provider_id, slot.start, user_id=turn.user_id, area=criteria.area or "")
    quote = _quote(deps, turn, search, offer, slot, hold=outcome.value)
    if outcome.kind == "ok" and outcome.value is not None:
        if (
            criteria.max_price is not None
            and not criteria.no_budget
            and outcome.value.quoted_price >= criteria.max_price
        ):
            turn.add_quote(quote.model_copy(update={"status": "failed"}))
            return PickResult("over_budget")
        show_quote(turn, quote)
        return PickResult("quoted", quote)
    if outcome.error == "slot_unavailable":
        fail_slot(deps, turn, quote, is_new=True)
        return PickResult("slot_failed", quote)
    return PickResult("provider_error", detail=outcome.error)


def _quote_reschedule(deps: Deps, turn: TurnContext, search: Search, offer: ProviderOffer, slot: Slot) -> PickResult:
    booking = deps.store.get_booking(search.criteria.reschedule_booking_id or "")
    if booking is None:
        return PickResult("not_offered")
    quote = _quote(deps, turn, search, offer, slot, hold=None).model_copy(
        update={"action": "reschedule", "booking_id": booking.id, "price": booking.price}
    )
    show_quote(turn, quote)
    return PickResult("quoted", quote)


def _quote(deps: Deps, turn: TurnContext, search: Search, offer: ProviderOffer, slot: Slot, hold: Hold | None) -> Quote:
    criteria: Criteria = search.criteria
    return Quote(
        id=f"q-{deps.new_id()}",
        conversation_id=turn.conversation_id,
        user_id=turn.user_id,
        search_id=search.id,
        action="book",
        provider_id=offer.provider_id,
        provider_name=offer.name,
        service=criteria.service or "",
        area=criteria.area or "",
        start=hold.slot.start if hold else slot.start,
        end=hold.slot.end if hold else slot.end,
        price=hold.quoted_price if hold else offer.price,
        hold_id=hold.hold_id if hold else None,
        hold_expires_at=hold.expires_at if hold else None,
        created_at=turn.now,
        updated_at=turn.now,
    )


def show_quote(turn: TurnContext, quote: Quote, *, note: str | None = None, is_new: bool = True) -> None:
    """Store the quote, fetch reviews for its card (display only) and queue the quote message."""
    if is_new:
        turn.add_quote(quote)
    else:
        turn.save_quote(quote)
        turn.quote = quote
    answer = turn.calls.get_provider(quote.provider_id)
    reviews: list[Untrusted] = list(answer.value.reviews) if answer.ok and answer.value is not None else []
    turn.quote_message = (quote_text(quote, note=note), quote_card(quote, review_lines(reviews)))
    turn.end(ExitReason(kind="needs_input", detail="confirmation"))


def fail_slot(deps: Deps, turn: TurnContext, quote: Quote, *, is_new: bool) -> None:
    """The slot is gone: record the quote as failed, tell the user and queue a search for other slots."""
    failed = quote.model_copy(update={"status": "failed", "updated_at": turn.now})
    if is_new:
        turn.add_quote(failed)
    else:
        turn.save_quote(failed)
    turn.say(
        f"{safe_name(quote.provider_name)} {day_time(quote.start)} was just taken. "
        "I'm looking for other slots for you…",
        NOTICE_CARD,
    )
    turn.queue_job(
        "research",
        None,
        {"conversation_id": turn.conversation_id, "user_id": turn.user_id, "failed_quote_id": quote.id},
    )
    turn.end(ExitReason(kind="queued"))
