"""Cancel, reschedule and listing the user's bookings.

Cancel and reschedule act on bookings in our table, so each test makes a real booking first.
"""

from __future__ import annotations

import pytest

from booking_agent.model import call, say
from tests.conftest import MakeWorld, Record
from tests.helpers import book_done, cards, scenario_with, tools


@pytest.mark.scenario(id="S17", title="Cancel with first-call 503", expected="same key retried; verified cancelled")
def test_cancel_503(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    booking = book_done(world)
    world.model.then(call("propose_cancellation", booking_id=booking.id), say("Tap Confirm to cancel."))
    assert cards(world.say("cancel my booking")) == ["quote"]
    quote = world.current_quote()
    assert quote is not None and quote.action == "cancel"
    assert world.confirm().exit.kind == "queued" and world.bookings()[0].status == "booked"
    (result,) = world.run_jobs()
    attempts = tools(world, "cancel")
    assert [c["error"] for c in attempts] == ["transient_503", None]
    assert attempts[0]["args"]["idem_key"] == attempts[1]["args"]["idem_key"]
    assert cards(result) == ["confirmation"] and world.bookings(status="cancelled")[0].id == booking.id
    assert world.provider_bookings(status="cancelled")
    record(actual="503 then success with the same key; provider read-back shows cancelled")


@pytest.mark.scenario(
    id="S18", title="Reschedule slot conflict", expected="original intact; slot failure starts research"
)
def test_reschedule_conflict(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",), reschedule_conflict_hours=(8, 10, 12, 14, 16, 18)))
    booking = book_done(world)
    world.model.then(
        call("propose_reschedule", booking_id=booking.id, date_from="2026-11-04", date_to="2026-11-05"),
        say("Here's Wednesday."),
    )
    world.say("Move my booking to Wednesday")
    quote = world.current_quote()
    assert quote is not None and quote.action == "reschedule" and quote.booking_id == booking.id
    world.confirm()
    results = world.run_jobs()
    assert any("just taken" in r.text for r in results)
    assert world.quotes()[1].status == "failed" and world.bookings()[0].start == booking.start
    assert len(world.provider_bookings()) == 1 and world.jobs("research", "done")
    record(actual="provider refused the move; booking unchanged, user told, research job ran")


@pytest.mark.scenario(
    id="S19", title="Reschedule a booking that does not exist", expected="tool refuses; nothing changes"
)
def test_reschedule_no_booking(make_world: MakeWorld, record: Record) -> None:
    world = make_world()
    world.model.then(
        call("propose_reschedule", booking_id="bk-nope", date_from="2026-11-07", date_to="2026-11-07"),
        say("I couldn't find that booking."),
    )
    reply = world.say("Move my AC servicing to Saturday")
    assert "quote" not in cards(reply) and not tools(world, "reschedule") and world.current_quote() is None
    assert "no booking with that id" in world.model.calls[-1][-1]["content"]
    record(actual="propose_reschedule returned an error to the model; no quote, no reschedule call")


@pytest.mark.scenario(id="S30", title="Show my bookings", expected="listed from our bookings table")
def test_list_bookings(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    booking = book_done(world)
    world.model.then(call("list_my_bookings", status="booked"), say("You have one booking."))
    world.say("when are my bookings")
    listed = world.model.calls[-1][-1]["content"]
    assert booking.id in listed and booking.provider_name in listed
    record(actual="list_my_bookings returned the one booked row")


def test_reschedule_moves_the_booking(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    booking = book_done(world)
    world.model.then(
        call("propose_reschedule", booking_id=booking.id, date_from="2026-11-04", date_to="2026-11-06"), say("Confirm.")
    )
    world.say("move it to Wednesday or later")
    quote = world.current_quote()
    assert quote is not None and quote.start != booking.start
    world.confirm()
    world.run_jobs()
    moved = world.bookings()[0]
    assert moved.id == booking.id and moved.start == quote.start
