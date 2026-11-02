"""Booking: search, picking a slot, quotes, slot failures and the research job.

The chat model is scripted; assertions are on outcomes, the stores, the provider and the trace, not on wording.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from booking_agent.domain.entities import Criteria, Search, TimeRange
from booking_agent.domain.planning import relaxed_variants
from booking_agent.model import ChatResponse, call, say
from tests.conftest import MakeWorld, Record, World
from tests.helpers import (
    BOOK_MSG,
    ask_booking,
    book_to_confirmation,
    cards,
    find,
    first_slot,
    offers,
    pick_first,
    quote_on_screen,
    scenario_with,
    tools,
)


@pytest.mark.scenario(id="S01", title="Happy path booking", expected="model proposes; code books after confirm")
def test_happy_path(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    first = ask_booking(world)
    assert cards(first) == ["offers"] and not world.bookings()
    picked = pick_first(world)
    quote = quote_on_screen(world)
    assert cards(picked) == ["quote"] and quote.status == "shown" and not world.bookings()
    queued = world.confirm()
    assert queued.exit.kind == "queued" and queued.answered_by == "code" and not world.bookings()
    (worker_result,) = world.run_jobs()
    assert cards(worker_result) == ["confirmation"]
    (booking,) = world.bookings()
    assert (booking.provider_id, booking.price, booking.start) == (quote.provider_id, quote.price, quote.start)
    assert world.quotes()[0].status == "done"
    assert len(world.model.calls) == 2  # one tool call, one reply: pick and confirm never reached the model
    record(actual=f"1 booking at {booking.price // 100} rupees; pick and confirm handled in code")


@pytest.mark.scenario(id="S02", title="Correction keeps other fields", expected="only the dates change")
def test_correction_keeps_fields(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    ask_booking(world)
    before = world.search()
    assert before is not None
    world.model.then(
        call("find_and_propose_booking", date_from="2026-11-07", date_to="2026-11-07"), say("Moved it to Saturday.")
    )
    world.say("actually make it saturday")
    after = world.search()
    assert after is not None and after.id != before.id  # a new snapshot; the old one is untouched
    assert after.criteria.date_from == datetime(2026, 11, 7).date()
    assert after.criteria.model_copy(update={"date_from": None, "date_to": None}) == before.criteria.model_copy(
        update={"date_from": None, "date_to": None}
    )
    record(actual="model sent only the new date; code kept service, area, times and budget")


@pytest.mark.scenario(
    id="S05", title="Price changes at hold", expected="quote shows the held price; no book before confirm"
)
def test_price_drift_at_hold(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(quote_drift_paise=20_000, book_outcomes=("success",)))
    ask_booking(world, max_price_rupees=None, no_budget=True)
    offered = offers(world)[0]
    pick_first(world)
    quote = quote_on_screen(world)
    assert quote.price == offered.price + 20_000 and not tools(world, "book")
    world.confirm()
    world.run_jobs()
    assert world.bookings()[0].price == quote.price
    record(actual=f"quote {quote.price // 100} vs search {offered.price // 100}; booked at the quote")


@pytest.mark.scenario(
    id="S06", title="Price changes after approval", expected="approval voided; new version needs a new confirm"
)
def test_price_change_after_approval(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    old = book_to_confirmation(world, max_price_rupees=None, no_budget=True)
    assert world.confirm().exit.kind == "queued"
    world.clock.advance(minutes=11)  # the hold runs out before the worker gets to it
    world.provider.scenario = dataclasses.replace(world.provider.scenario, quote_drift_paise=15_000)
    (worker_result,) = world.run_jobs()
    current = quote_on_screen(world)
    assert current.id == old.id and current.version == 2 and current.status == "shown"
    assert current.price == old.price + 15_000 and cards(worker_result) == ["quote"]
    assert not world.bookings() and not tools(world, "book")
    stale = world.confirm(old.id, 1)  # the old approval does nothing
    assert stale.exit.detail == "stale_proposal" and not world.jobs("book", "pending")
    world.confirm()
    world.run_jobs()
    assert [b.price for b in world.bookings()] == [current.price]
    record(actual=f"price {old.price // 100} -> {current.price // 100}: version 2, booked only after second confirm")


@pytest.mark.scenario(
    id="S07", title="Slot unavailable at hold", expected="told at once; research offers the next best slot"
)
def test_slot_unavailable_at_pick(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(hold_unavailable_first=1))
    ask_booking(world)
    provider_id, start = first_slot(world)
    immediate = world.pick(provider_id, start)
    assert "just taken" in immediate.text and immediate.exit.kind == "queued"
    (research,) = world.jobs("research", "pending")
    assert world.quotes()[0].status == "failed" and world.current_quote() is None
    (result,) = [r for r in world.run_jobs() if cards(r)]
    assert cards(result) == ["offers", "quote"] and "next best option" in result.text
    quote = quote_on_screen(world)
    assert (quote.provider_id, quote.start) != (provider_id, start)
    assert research.id in {job.id for job in world.jobs("research", "done")}
    record(actual="first hold failed: message at once, research job held the next best slot")


@pytest.mark.scenario(
    id="S14", title="slot_taken at book", expected="quote failed; next slot offered; booked only after confirm"
)
def test_slot_taken_at_book(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("slot_taken", "success")))
    failed = book_to_confirmation(world)
    world.confirm()
    messages = [r.text for r in world.run_jobs() if r.text]
    assert any("just taken" in text for text in messages) and not world.bookings()
    assert world.quotes()[0].id == failed.id and world.quotes()[0].status == "failed"
    replacement = quote_on_screen(world)
    assert replacement.id != failed.id and replacement.start != failed.start and replacement.status == "shown"
    world.confirm()
    world.run_jobs()
    assert [b.quote_id for b in world.bookings()] == [replacement.id]
    record(actual="slot_taken -> quote failed -> research -> new quote -> booked after second confirm")


@pytest.mark.scenario(
    id="S15", title="Hold expires before confirmation", expected="re-hold at write time; same price keeps approval"
)
def test_hold_expiry(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    quote = book_to_confirmation(world)
    world.clock.advance(minutes=12)
    world.confirm()
    (result,) = world.run_jobs()
    assert cards(result) == ["confirmation"] and len(tools(world, "hold_slot")) == 2
    assert world.quotes()[0].version == quote.version == 1
    record(actual="hold re-taken at write time at the same price; booked without asking again")


@pytest.mark.scenario(
    id="S20", title="Same electrician as last time", expected="last completed provider from provider history"
)
def test_same_provider_as_last_time(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    history = [b for b in world.provider.all_bookings() if b["service"] == "electrician" and b["status"] == "completed"]
    last = max(history, key=lambda b: b["slot_start"])
    ask_booking(
        world,
        "same electrician as last time, not before 10",
        service="electrician",
        times=[{"from": "10:00", "to": "22:00"}],
        max_price_rupees=None,
        no_budget=True,
        same_provider_as_last_time=True,
    )
    (offer,) = offers(world)
    assert offer.provider_id == last["provider_id"]
    assert all(slot.start.hour >= 10 for slot in offer.slots)
    record(actual=f"only {offer.provider_id} offered (from provider history), none before 10:00")


@pytest.mark.scenario(
    id="S25", title="Slot already booked from another conversation", expected="store backstop; no book call"
)
def test_slot_already_booked_elsewhere(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success", "success")))
    first = book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    assert len(world.bookings()) == 1
    # A second approval for the very same slot, made in another conversation, is queued as well.
    twin = first.model_copy(
        update={"id": "q-twin", "conversation_id": "c2", "status": "confirmed", "confirmed_at": world.clock.now()}
    )
    world.store.add_quote(twin)
    world.store.enqueue_job(
        kind="book",
        quote_id=twin.id,
        payload={"quote_id": twin.id, "conversation_id": "c2", "user_id": "u1"},
        now=world.clock.now(),
    )
    calls_before = len(tools(world, "book"))
    world.run_jobs()
    assert len(tools(world, "book")) == calls_before and len(world.bookings()) == 1
    assert world.store.get_quote("q-twin").status == "failed"  # type: ignore[union-attr]
    record(actual="second approval for a booked slot failed before any book call; one booking")


@pytest.mark.scenario(id="S29", title="Unsupported service", expected="tool refuses before any search")
def test_unsupported_service(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    world.model.then(
        call("find_and_propose_booking", service="pest_control", area="Koramangala"), say("Sorry, I can't book that.")
    )
    world.say("I need pest control")
    assert not tools(world, "search_providers")
    assert "unsupported_service" in world.model.calls[-1][-1]["content"]
    record(actual="could_not_propose: unsupported_service, no search")


@pytest.mark.scenario(id="S32", title="Calendar pick and other options", expected="pick re-holds; forged slot refused")
def test_calendar_pick_and_forged_slot(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    ask_booking(world)
    pick_first(world)
    first = quote_on_screen(world)
    provider_id, start = first_slot(world, 1)
    world.pick(provider_id, start)
    second = quote_on_screen(world)
    assert (second.provider_id, second.start) == (provider_id, start)
    assert world.store.get_quote(first.id).status == "declined"  # type: ignore[union-attr]
    forged = world.pick(provider_id, start + timedelta(minutes=30))
    assert "isn't one I can offer" in forged.text and quote_on_screen(world).id == second.id
    record(actual="second pick replaced the first quote; a slot not on the card was refused")


@pytest.mark.scenario(
    id="S33", title="Slot changes after approval", expected="a new pick cannot replace a confirmed quote"
)
def test_slot_change_after_approval(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    quote = book_to_confirmation(world)
    world.confirm()
    provider_id, start = first_slot(world, 1)
    refused = world.pick(provider_id, start)
    assert "waiting" in refused.text or refused.exit.kind != "queued"
    assert world.current_quote() is not None and world.current_quote().id == quote.id  # type: ignore[union-attr]
    world.run_jobs()
    (booking,) = world.bookings()
    assert (booking.provider_id, booking.start) == (quote.provider_id, quote.start)
    record(actual="pick after confirm was refused; the confirmed slot is the one booked")


@pytest.mark.scenario(
    id="S34", title="Option taken between two tool calls", expected="slot_unavailable to the model; research queued"
)
def test_choose_option_taken_in_same_turn(make_world: MakeWorld, record: Record) -> None:
    world = make_world()

    def choose_second(messages: list[dict[str, Any]]) -> ChatResponse:
        world.provider.scenario = dataclasses.replace(world.provider.scenario, hold_unavailable_first=1)
        shown = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])["providers"][1]
        return call("choose_option", provider_id=shown["provider_id"], slot_start=shown["first_slot"])

    world.model.then(find(), choose_second, say("That one was just taken."))
    reply = world.say(BOOK_MSG + ", and show me the second provider")
    tool_results = [m["content"] for m in world.model.calls[-1] if m["role"] == "tool"]
    assert "slot_unavailable" in tool_results[-1]
    assert "just taken" in reply.text and len(world.jobs("research", "pending")) == 1
    assert not tools(world, "book")
    record(actual="hold failed mid-turn: model told slot_unavailable, user told at once, research queued")


# ---------------------------------------------------------------- research after a slot failure


def _fail_first_slot(world: World, criteria: Criteria | None = None) -> None:
    """Ask, tap the first slot (its hold fails) and, if given, make ``criteria`` the newest search."""
    ask_booking(world)
    world.pick(*first_slot(world))
    if criteria is not None:
        world.store.add_search(
            Search(id="s-forced", conversation_id="c1", criteria=criteria, created_at=world.clock.now())
        )
        world.clock.advance(seconds=1)


def _narrow(**changes: Any) -> Criteria:
    base = Criteria(
        service="plumber",
        area="Koramangala",
        date_from=datetime(2026, 11, 2).date(),
        date_to=datetime(2026, 11, 3).date(),
        times=(TimeRange(from_time=datetime(2026, 1, 1, 10).time(), to_time=datetime(2026, 1, 1, 11).time()),),
        max_price=80_000,
    )
    return base.model_copy(update=changes)


def test_research_prefers_the_original_search_over_relaxed(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(hold_unavailable_first=1))
    _fail_first_slot(world, _narrow(max_price=200_000))  # several slots fit; only one failed
    world.run_jobs()
    search = world.search()
    assert search is not None and search.criteria.relaxed == []
    assert world.current_quote() is not None  # best slot of the original search is held


def test_research_relaxed_results_are_shown_without_a_hold(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(hold_unavailable_first=1))
    _fail_first_slot(world, _narrow(date_to=datetime(2026, 11, 2).date()))  # one day, one hour, one cheap provider
    world.provider.scenario = dataclasses.replace(world.provider.scenario, hold_unavailable_first=0)
    holds_before = len(tools(world, "hold_slot"))
    results = world.run_jobs()
    search = world.search()
    assert search is not None and search.criteria.relaxed == ["times"]  # times is tried before dates and budget
    assert search.results and world.current_quote() is None
    assert len(tools(world, "hold_slot")) == holds_before
    assert any("Nothing matches" in r.text and "offers" in cards(r) for r in results)


def test_research_with_nothing_found_asks_the_user(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(hold_unavailable_first=1))
    _fail_first_slot(world, _narrow(max_price=1_000))  # no provider costs ten rupees, even 25% more
    results = world.run_jobs()
    assert world.current_quote() is None and not world.bookings()
    assert any("What would you like to change?" in r.text for r in results)


def test_research_searches_run_at_the_same_time(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(hold_unavailable_first=1))
    wide = _narrow(days=("mon", "tue"), max_price=200_000)
    _fail_first_slot(world, wide)
    assert len(relaxed_variants(wide)) == 4
    running, peak, lock = 0, 0, threading.Lock()
    original = world.provider.search_providers

    def slow_search(*args: Any, **kwargs: Any) -> Any:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.15)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                running -= 1

    world.provider.search_providers = slow_search  # type: ignore[method-assign]
    world.run_jobs()
    assert peak > 1
    (event,) = world.events("research_searches")
    assert event["variants"] == ["original", "times", "dates", "days", "budget"]


def test_relaxation_order_is_times_dates_days_budget() -> None:
    names = [name for name, _ in relaxed_variants(_narrow(days=("mon",)))]
    assert names == ["times", "dates", "days", "budget"]


def test_confirmed_quote_has_no_failed_slot_offered_again(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("slot_taken",)))
    failed = book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    offered = [(o.provider_id, s.start) for o in offers(world) for s in o.slots]
    assert (failed.provider_id, failed.start) not in offered
