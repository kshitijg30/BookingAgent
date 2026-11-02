"""One test per bug in docs/bugs.md."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytest

from booking_agent.domain.models import IST
from booking_agent.errors import EnqueueFailed
from booking_agent.model import call, say
from tests.conftest import MakeWorld
from tests.helpers import (
    BOOK_MSG,
    ask_booking,
    book_done,
    book_to_confirmation,
    cards,
    find,
    first_slot,
    scenario_with,
    tools,
)


def test_two_users_same_slot_second_gets_alternative(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success", "success")))
    ask_booking(world)  # user u1, conversation c1
    world.model.then(find(), say("Here are options."))
    offered_to_b = world.say(BOOK_MSG, conversation="c-b", user="u2")
    assert cards(offered_to_b) == ["offers"]
    provider_id, start = first_slot(world)
    assert first_slot(world, conversation="c-b") == (provider_id, start)  # both see the same first slot

    world.pick(provider_id, start)  # A holds the slot first
    quote_a = world.current_quote()
    assert quote_a is not None and quote_a.status == "shown"

    told = world.pick(provider_id, start, conversation="c-b", user="u2")  # B's hold fails
    assert "just taken" in told.text and told.exit.kind == "queued"
    (failed,) = world.quotes(user="u2")
    assert failed.status == "failed" and (failed.provider_id, failed.start) == (provider_id, start)
    (research,) = world.jobs("research", "pending")
    assert research.payload["user_id"] == "u2"

    results = [r for r in world.run_jobs() if cards(r)]
    quote_b = world.current_quote("c-b")
    assert cards(results[0]) == ["offers", "quote"] and quote_b is not None
    assert (quote_b.provider_id, quote_b.start) != (provider_id, start)

    world.confirm()
    world.confirm(conversation="c-b", user="u2")
    world.run_jobs()
    assert [(b.provider_id, b.start) for b in world.bookings()] == [(provider_id, start)]
    assert [(b.provider_id, b.start) for b in world.bookings(user="u2")] == [(quote_b.provider_id, quote_b.start)]


def test_timeout_after_success_is_reported_as_booked_once(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_booked",)))
    book_to_confirmation(world)
    world.confirm()
    results = world.run_jobs()
    texts = [m.text for r in results for m in r.messages]
    assert sum("Booked." in text for text in texts) == 1
    assert not any("couldn't" in text.lower() or "failed" in text.lower() for text in texts)
    (call,) = tools(world, "book")
    assert (
        call["outcome"] == "unknown"
        and tools(world, "get_booking_status")[0]["args"]["idem_key"] == call["args"]["idem_key"]
    )
    assert len(world.bookings()) == 1 and len(world.provider_bookings()) == 1
    assert world.run_jobs() == [] and len(tools(world, "book")) == 1  # nothing is written a second time


def test_confirm_save_failure_leaves_no_job_and_quote_shown(
    make_world: MakeWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    quote = book_to_confirmation(world)
    saved = world.saved_conversation()

    def conflict(*args: object, **kwargs: object) -> None:
        from booking_agent.errors import VersionConflict

        raise VersionConflict

    monkeypatch.setattr(world.store, "save_conversation", conflict)
    with caplog.at_level(logging.ERROR, logger="booking_agent"), pytest.raises(EnqueueFailed):
        world.confirm()
    monkeypatch.undo()
    assert any(f"enqueue_failed quote_id={quote.id}" in line for line in caplog.messages)
    assert not world.jobs("book") and world.current_quote().status == "shown"  # type: ignore[union-attr]
    assert world.saved_conversation().version == saved.version and len(world.saved_conversation().messages) == len(
        saved.messages
    )
    assert world.run_jobs() == [] and not world.bookings()


def _spy(world: Any, method: str, arg_index: int) -> list[str]:
    """Record the slot string each provider call receives (the provider boundary, below the trace)."""
    sent: list[str] = []
    original = getattr(world.provider, method)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        sent.append(args[arg_index])
        return original(*args, **kwargs)

    setattr(world.provider, method, wrapper)
    return sent


def test_rehold_after_expiry_sends_the_slot_in_business_timezone(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    sent = _spy(world, "hold_slot", 1)
    quote = book_to_confirmation(world)
    world.clock.advance(minutes=12)  # the hold expires
    world.confirm()
    world.run_jobs()
    reloaded = world.store.get_quote(quote.id)
    assert reloaded is not None and reloaded.start.utcoffset() is not None
    assert len(sent) == 2 and sent[0] == sent[1]  # both holds reached the provider as 10:00 IST
    assert datetime.fromisoformat(sent[1]).hour == 10
    assert len(tools(world, "hold_slot")) == 2 and all(c["outcome"] == "ok" for c in tools(world, "hold_slot"))
    assert [b.start for b in world.bookings()] == [quote.start]


def test_reschedule_sends_the_new_slot_in_business_timezone(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    booking = book_done(world)
    world.model.then(
        call("propose_reschedule", booking_id=booking.id, date_from="2026-11-04", date_to="2026-11-06"), say("Confirm.")
    )
    world.say("move it to Wednesday or later")
    quote = world.current_quote()
    assert quote is not None
    sent = _spy(world, "reschedule", 1)
    world.confirm()
    world.run_jobs()
    assert len(sent) == 1 and datetime.fromisoformat(sent[0]).hour == quote.start.astimezone(IST).hour
    assert world.bookings()[0].start == quote.start


def test_worker_turn_without_messages_posts_no_fallback_reply(make_world: MakeWorld) -> None:
    from booking_agent.flow.context import TurnRequest

    world = make_world()
    world.say("hello")
    before = world.agent_texts()
    result = world.agent.runner.run(
        TurnRequest(conversation_id="c1", user_id="u1", source="worker"), lambda deps, turn, request: None
    )
    assert result.messages == [] or not any("What would you like" in m.text for m in result.messages)
    assert world.agent_texts() == before
