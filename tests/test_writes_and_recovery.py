"""Provider writes through the job queue: timeouts, verification, retries, dead jobs, restarts and follow-ups."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking_agent.api import create_app
from booking_agent.errors import EnqueueFailed
from tests.conftest import MakeWorld, Record, World
from tests.helpers import book_to_confirmation, cards, pick_first, scenario_with, tools
from tests.http_support import make_settings


def _book_keys(world: World) -> list[str]:
    return [call["args"]["idem_key"] for call in tools(world, "book")]


@pytest.mark.scenario(
    id="S08", title="Timeout after success", expected="status checked with the same key; exactly one booking"
)
def test_timeout_after_success(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_booked",)))
    book_to_confirmation(world)
    world.confirm()
    (result,) = world.run_jobs()
    (key,) = _book_keys(world)
    (status,) = tools(world, "get_booking_status")
    assert status["args"]["idem_key"] == key and status["outcome"] == "ok"
    assert cards(result) == ["confirmation"] and len(world.bookings()) == 1 and len(world.provider_bookings()) == 1
    assert world.quotes()[0].status == "done" and world.jobs("book")[0].status == "done"
    record(actual="book timed out; get_booking_status(same key) said booked; one booking, reported once")


@pytest.mark.scenario(
    id="S09", title="Timeout, not booked", expected="verified not applied; new job and new key; one booking"
)
def test_timeout_not_booked(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_not_booked", "success")))
    book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    first, second = world.jobs("book")
    assert (first.status, second.status) == ("failed", "done") and first.id != second.id
    assert _book_keys(world) == [f"book:{first.id}", f"book:{second.id}"]
    assert len(world.bookings()) == 1 and len(world.provider_bookings()) == 1
    record(actual="not_found twice -> job failed, new job with a new key booked it; one booking")


@pytest.mark.scenario(
    id="S10", title="Status check also times out", expected="same job and key retried; no claim of success"
)
def test_status_timeouts_retry_the_same_job(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_booked",), status_timeouts=1000))
    book_to_confirmation(world)
    world.confirm()
    for _ in range(3):
        results = world.run_jobs()
        assert not any("Booked." in r.text for r in results) and not world.bookings()
        world.clock.advance(seconds=60)
    (job,) = world.jobs("book")
    assert job.status == "pending" and job.attempts == 3 and world.current_quote().status == "confirmed"  # type: ignore[union-attr]
    assert set(_book_keys(world)) == {f"book:{job.id}"} and len(world.jobs("book")) == 1
    record(actual="status unknown: one job retried with backoff under one key; nothing reported as booked")


def test_dead_job_hands_over_to_a_check_job_with_the_original_key(
    make_world: MakeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_booked",), status_timeouts=1000))
    quote = book_to_confirmation(world)
    world.confirm()
    with caplog.at_level(logging.ERROR, logger="booking_agent.jobs"):
        results = []
        for _ in range(5):
            results += world.run_jobs()
            world.clock.advance(seconds=60)
    (dead,) = world.jobs("book", "dead")
    assert any(f"job_dead job_id={dead.id}" in line for line in caplog.messages)
    assert any("keep checking" in r.text for r in results)
    assert world.store.get_quote(quote.id).status == "confirmed"  # type: ignore[union-attr]
    (check,) = world.jobs("check", "pending")
    assert check.payload["key"] == f"book:{dead.id}" and check.quote_id == quote.id
    assert not world.bookings()
    # The provider answers again: the check job settles the quote the way the write would have.
    world.provider.scenario = dataclasses.replace(world.provider.scenario, status_timeouts=0)
    world.provider.forced_status_timeouts = 0
    world.clock.advance(seconds=60)
    world.run_jobs()
    assert len(world.bookings()) == 1 and world.store.get_quote(quote.id).status == "done"  # type: ignore[union-attr]
    assert len(world.provider_bookings()) == 1 and len(tools(world, "book")) == 5


@pytest.mark.scenario(
    id="S11", title="Repeated confirmation", expected="3 taps on one card: one job, one book call, one booking"
)
def test_repeated_confirmation(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    quote = book_to_confirmation(world)
    taps = [world.confirm(quote.id, 1) for _ in range(3)]
    assert [t.exit.kind for t in taps] == ["queued", "needs_input", "needs_input"]
    assert taps[1].exit.detail == "already_confirmed" and len(world.jobs("book")) == 1
    world.run_jobs()
    late = world.confirm(quote.id, 1)  # a tap after the booking is done
    assert late.exit.detail == "stale_proposal" and world.run_jobs() == []
    assert len(tools(world, "book")) == 1 and len(world.bookings()) == 1 and len(world.provider_bookings()) == 1
    record(actual="4 taps: one job, one book call, one booking")


@pytest.mark.scenario(id="S13", title="Restart mid-booking", expected="a new process finishes the job; one booking")
def test_restart_mid_booking(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    book_to_confirmation(world)
    world.confirm()
    world.restart()  # every in-memory object is gone; only the databases remain
    assert world.current_quote().status == "confirmed" and len(world.jobs("book", "pending")) == 1  # type: ignore[union-attr]
    (result,) = world.run_jobs()
    assert cards(result) == ["confirmation"] and len(world.bookings()) == 1
    world.restart()
    assert world.run_jobs() == [] and len(world.provider_bookings()) == 1
    record(actual="job survived the restart and ran once; a second restart found nothing to do")


def test_worker_crash_after_claim_is_retried_with_the_same_key(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    book_to_confirmation(world)
    world.confirm()
    claimed = world.store.claim_job(now=world.clock.now(), lease=timedelta(seconds=60))
    assert claimed is not None and claimed.kind in ("book", "memory_update")
    while claimed is not None and claimed.kind != "book":
        claimed = world.store.claim_job(now=world.clock.now(), lease=timedelta(seconds=60))
    world.restart()  # the worker died while holding the lease
    assert world.run_jobs() == []  # the lease is still valid
    world.clock.advance(seconds=61)
    world.run_jobs()
    assert len(world.bookings()) == 1 and _book_keys(world) == [f"book:{claimed.id}"]  # type: ignore[union-attr]


def test_restart_between_timeout_and_verification_keeps_the_key(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("timeout_booked",), status_timeouts=1000))
    book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    (job,) = world.jobs("book")
    assert job.status == "pending" and not world.bookings()
    world.restart()
    world.provider.scenario = dataclasses.replace(world.provider.scenario, status_timeouts=0)
    world.clock.advance(seconds=60)
    world.run_jobs()
    assert len(world.bookings()) == 1 and len(world.provider_bookings()) == 1
    assert set(_book_keys(world)) == {f"book:{job.id}"}


@pytest.mark.scenario(
    id="S26",
    title="Provider cancels; agent finds a replacement",
    expected="replacement offered; booked only after confirm",
)
def test_provider_cancellation_followup(make_world: MakeWorld, record: Record) -> None:
    world = make_world(scenario_with(book_outcomes=("success", "success")))
    first = book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    (booking,) = world.bookings()
    world.provider.provider_cancel(booking.provider_booking_id or "")
    assert world.agent.scan_provider_cancellations() == 1
    cancelled = world.bookings(status=None)[0]
    assert cancelled.status == "cancelled_by_provider"
    search = world.search()
    assert search is not None and search.criteria.replaces_booking_id == booking.id
    assert booking.provider_id in search.criteria.exclude_provider_ids
    assert all(offer.provider_id != booking.provider_id for offer in search.results)
    assert "cancelled" in world.agent_texts()[-1] and cards_of_last(world) == "offers"
    assert world.current_quote() is None and len(world.bookings(status="booked")) == 0
    pick_first(world)
    replacement = world.current_quote()
    assert replacement is not None and replacement.id != first.id and not world.bookings()
    world.confirm()
    world.run_jobs()
    (new_booking,) = world.bookings()
    assert new_booking.quote_id == replacement.id and new_booking.provider_id != booking.provider_id
    record(actual="provider cancelled: booking marked, replacement offers shown, rebooked only after confirm")


def cards_of_last(world: World) -> str:
    last = world.saved_conversation().messages[-1]
    return str(last.card["type"]) if last.card else ""


def test_confirm_enqueue_failure_leaves_the_quote_shown_and_no_job(
    make_world: MakeWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = make_world(scenario_with(book_outcomes=("success",)))
    quote = book_to_confirmation(world)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(world.store, "enqueue_job", broken)
    with caplog.at_level(logging.ERROR, logger="booking_agent"), pytest.raises(EnqueueFailed) as raised:
        world.confirm()
    assert raised.value.quote_id == quote.id
    assert any(f"enqueue_failed quote_id={quote.id}" in line for line in caplog.messages)
    monkeypatch.undo()
    assert world.current_quote().status == "shown" and not world.jobs("book")  # type: ignore[union-attr]
    world.confirm()  # the user can try again
    world.run_jobs()
    assert len(world.bookings()) == 1


def test_slot_taken_gives_a_message_at_the_worker_and_a_research_job(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("slot_taken",)))
    quote = book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    assert world.store.get_quote(quote.id).status == "failed"  # type: ignore[union-attr]
    assert len(world.jobs("research", "done")) == 1 and not world.bookings()


def _book_one(world: World):  # type: ignore[no-untyped-def]
    book_to_confirmation(world)
    world.confirm()
    world.run_jobs()
    (booking,) = world.bookings()
    assert booking.provider_booking_id and booking.provider_booking_id != booking.id
    return booking


def test_admin_provider_cancel_with_our_booking_id_cancels_and_queues_replacement(
    make_world: MakeWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BA_ADMIN_CODE", "adm")
    world = make_world(scenario_with(book_outcomes=("success", "success")))
    booking = _book_one(world)
    app = create_app(make_settings(tmp_path), agent=world.agent, provider=world.provider, run_workers=False)
    with TestClient(app) as client:
        headers = {"X-Admin-Code": "adm"}
        missing = client.post("/admin/api/demo/provider-cancel", headers=headers, json={"booking_id": "bk-nope"})
        assert missing.status_code == 404 and missing.json() == {"detail": "booking not found"}
        response = client.post("/admin/api/demo/provider-cancel", headers=headers, json={"booking_id": booking.id})
    assert response.status_code == 200 and response.json()["replacements_queued"] == 1
    assert world.bookings(status=None)[0].status == "cancelled_by_provider"
    search = world.search()
    assert search is not None and search.criteria.replaces_booking_id == booking.id


def test_provider_cancellation_scan_tells_the_open_chat_once(make_world: MakeWorld) -> None:
    world = make_world(scenario_with(book_outcomes=("success", "success")))
    booking = _book_one(world)
    calls: list[tuple[str, str | None, str]] = []
    world.agent.on_conversation_updated = lambda *args: calls.append(args)
    world.provider.provider_cancel(booking.provider_booking_id or "")
    assert world.agent.scan_provider_cancellations() == 1
    assert calls == [(booking.conversation_id, None, "provider_cancelled")]
    assert world.agent.scan_provider_cancellations() == 0 and len(calls) == 1
