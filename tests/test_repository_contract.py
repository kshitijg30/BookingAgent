"""One contract for every ``Repository`` implementation: the SQLAlchemy ``Store`` and ``MemoryRepository``.

Each test runs once per implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from booking_agent.domain.entities import (
    Address,
    Booking,
    BookingStatus,
    ChatMessage,
    Conversation,
    Criteria,
    ProviderOffer,
    Quote,
    QuoteStatus,
    Search,
    Slot,
    TimeRange,
    UserProfile,
)
from booking_agent.errors import RequestConflict, VersionConflict
from booking_agent.memory_repository import MemoryRepository
from booking_agent.ports import Repository
from booking_agent.rows import Job
from booking_agent.store import Store

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SLOT = NOW + timedelta(days=1)
LEASE = timedelta(minutes=5)


@pytest.fixture(params=["sqlite", "memory"])
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Repository]:
    built: Repository = Store(tmp_path / "contract.sqlite3") if request.param == "sqlite" else MemoryRepository()
    yield built
    built.close()


def later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def conversation(
    conversation_id: str = "c1", user_id: str = "u1", *, texts: tuple[str, ...] = (), updated: datetime = NOW
) -> Conversation:
    messages = tuple(ChatMessage(role="user", text=text, at=NOW) for text in texts)
    return Conversation(
        id=conversation_id, user_id=user_id, title="Plumber", messages=messages, created_at=NOW, updated_at=updated
    )


def search(search_id: str = "s1", conversation_id: str = "c1", *, created: datetime = NOW) -> Search:
    return Search(
        id=search_id,
        conversation_id=conversation_id,
        criteria=Criteria(service="plumber", area="Koramangala", any_date=True, no_budget=True),
        results=(
            ProviderOffer(
                provider_id="p1", name="Iyer Plumbing", rating=4.5, price=50_000, slots=(Slot(start=SLOT, end=SLOT),)
            ),
        ),
        created_at=created,
    )


def quote(
    quote_id: str = "q1",
    *,
    conversation_id: str = "c1",
    user_id: str = "u1",
    status: QuoteStatus = "shown",
    search_id: str | None = "s1",
    service: str = "plumber",
    action: str = "book",
    provider_id: str = "p1",
    start: datetime = SLOT,
    created: datetime = NOW,
) -> Quote:
    return Quote.model_validate(
        {
            "id": quote_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "search_id": search_id,
            "action": action,
            "provider_id": provider_id,
            "provider_name": "Iyer Plumbing",
            "service": service,
            "area": "Koramangala",
            "start": start,
            "end": start + timedelta(hours=1),
            "price": 50_000,
            "hold_id": "h1",
            "hold_expires_at": created + timedelta(minutes=10),
            "status": status,
            "created_at": created,
            "updated_at": created,
        }
    )


def booking(
    booking_id: str = "b1",
    *,
    user_id: str = "u1",
    status: BookingStatus = "booked",
    provider_id: str = "p1",
    start: datetime = SLOT,
    service: str = "plumber",
    created: datetime = NOW,
) -> Booking:
    return Booking(
        id=booking_id,
        user_id=user_id,
        conversation_id="c1",
        quote_id="q1",
        provider_id=provider_id,
        provider_name="Iyer Plumbing",
        service=service,
        area="Koramangala",
        start=start,
        end=start + timedelta(hours=1),
        price=50_000,
        status=status,
        provider_booking_id="pb1",
        created_at=created,
        updated_at=created,
    )


AREA = {"area": {"value": "Koramangala", "source": "stated", "confirmed": True}}


def remember(repository: Repository, memory: Mapping[str, object] | None = None, user_id: str = "u1") -> None:
    """Save the user's personal memory in their profile."""
    profile = repository.get_profile(user_id).model_copy(update={"memory": AREA if memory is None else memory})
    repository.save_profile(profile, now=NOW)


def enqueue(
    repository: Repository,
    quote_id: str | None = "q1",
    *,
    kind: str = "book",
    run_at: datetime | None = None,
    created: datetime = NOW,
) -> Job:
    return repository.enqueue_job(
        kind=kind,  # type: ignore[arg-type]
        quote_id=quote_id,
        payload={"quote_id": quote_id},
        now=created,
        run_at=run_at,
    )


# ---------------------------------------------------------------- criteria


def test_criteria_missing_follows_the_spec_rule() -> None:
    assert Criteria().missing() == ["service", "area", "dates", "budget"]
    assert Criteria(service="plumber", area="Indiranagar", date_from=date(2026, 11, 3), max_price=1).missing() == []
    assert Criteria(service="plumber", area="x", any_date=True, no_budget=True).missing() == []
    assert Criteria(service="plumber", area="x", any_date=True).missing() == ["budget"]


def test_time_range_uses_from_and_to_in_json() -> None:
    criteria = Criteria(times=(TimeRange(**{"from": time(10), "to": time(22)}),))
    dumped = criteria.model_dump(mode="json", by_alias=True)
    assert dumped["times"] == [{"from": "10:00:00", "to": "22:00:00"}]
    assert Criteria.model_validate(dumped) == criteria


# ---------------------------------------------------------------- conversations


def test_conversation_save_and_get_bumps_the_version(repository: Repository) -> None:
    assert repository.get_conversation("c1") is None
    saved = repository.save_conversation(conversation(), expected_version=0)
    assert saved.version == 1
    longer = conversation(texts=("hi", "plumber please"), updated=later(1))
    again = repository.save_conversation(longer, expected_version=1)
    loaded = repository.get_conversation("c1")
    assert loaded == again
    assert loaded is not None and loaded.version == 2 and [m.text for m in loaded.messages] == ["hi", "plumber please"]


def test_conversation_messages_keep_cards(repository: Repository) -> None:
    card = {"type": "quote", "quote_id": "q1", "version": 1}
    message = ChatMessage(role="agent", text="Confirm?", card=card, at=NOW)
    repository.save_conversation(conversation().model_copy(update={"messages": (message,)}), expected_version=0)
    loaded = repository.get_conversation("c1")
    assert loaded is not None and loaded.messages == (message,)


def test_conversation_save_with_a_stale_version_conflicts(repository: Repository) -> None:
    repository.save_conversation(conversation(), expected_version=0)
    with pytest.raises(VersionConflict):
        repository.save_conversation(conversation(), expected_version=0)
    with pytest.raises(VersionConflict):
        repository.save_conversation(conversation("c2"), expected_version=3)


def test_conversations_are_listed_per_user_newest_first(repository: Repository) -> None:
    repository.save_conversation(conversation("c1", updated=NOW), expected_version=0)
    repository.save_conversation(conversation("c2", updated=later(5)), expected_version=0)
    repository.save_conversation(conversation("c3", "u2"), expected_version=0)
    assert [row.id for row in repository.list_conversations("u1")] == ["c2", "c1"]
    assert [row.id for row in repository.list_conversations("u1", limit=1)] == ["c2"]
    assert repository.list_conversations("nobody") == []


# ---------------------------------------------------------------- searches


def test_search_round_trips_criteria_and_results(repository: Repository) -> None:
    shown = search().model_copy(
        update={
            "criteria": Criteria(
                service="plumber",
                area="x",
                date_from=date(2026, 11, 3),
                date_to=date(2026, 11, 5),
                days=("mon", "tue"),
                times=(TimeRange(**{"from": time(6), "to": time(12)}),),
                max_price=100_000,
                provider_id="p1",
                replaces_booking_id="b9",
            )
        }
    )
    repository.add_search(shown)
    assert repository.get_search("s1") == shown
    assert repository.get_search("missing") is None


def test_newest_search_is_per_conversation(repository: Repository) -> None:
    assert repository.newest_search_for_conversation("c1") is None
    repository.add_search(search("s1", created=NOW))
    repository.add_search(search("s2", created=later(2)))
    repository.add_search(search("s3", "c2", created=later(9)))
    newest = repository.newest_search_for_conversation("c1")
    assert newest is not None and newest.id == "s2"


# ---------------------------------------------------------------- quotes


def test_quote_add_get_and_save(repository: Repository) -> None:
    repository.add_quote(quote())
    assert repository.get_quote("q1") == quote()
    assert repository.get_quote("missing") is None
    confirmed = quote().model_copy(update={"status": "confirmed", "version": 2, "confirmed_at": later(1)})
    repository.save_quote(confirmed)
    assert repository.get_quote("q1") == confirmed
    with pytest.raises(LookupError):
        repository.save_quote(quote("unknown"))


def test_current_quote_is_the_newest_shown_or_confirmed(repository: Repository) -> None:
    assert repository.current_quote_for_conversation("c1") is None
    repository.add_quote(quote("q1", status="shown", created=NOW))
    repository.add_quote(quote("q2", status="declined", created=later(2)))
    repository.add_quote(quote("q3", status="confirmed", created=later(1)))
    repository.add_quote(quote("q4", status="shown", conversation_id="c2", created=later(9)))
    current = repository.current_quote_for_conversation("c1")
    assert current is not None and current.id == "q3"
    repository.save_quote(quote("q3", status="done", created=later(1)))
    current = repository.current_quote_for_conversation("c1")
    assert current is not None and current.id == "q1"


def test_list_quotes_filters_by_user_status_and_service(repository: Repository) -> None:
    repository.add_quote(quote("q1", service="plumber", created=NOW))
    repository.add_quote(quote("q2", service="Electrician", status="done", created=later(1)))
    repository.add_quote(quote("q3", service="plumber", status="done", created=later(2)))
    repository.add_quote(quote("q4", user_id="u2", created=later(3)))
    assert [row.id for row in repository.list_quotes_for_user("u1")] == ["q3", "q2", "q1"]
    assert [row.id for row in repository.list_quotes_for_user("u1", status="done")] == ["q3", "q2"]
    assert [row.id for row in repository.list_quotes_for_user("u1", service="ELECTRICIAN")] == ["q2"]
    assert [row.id for row in repository.list_quotes_for_user("u1", status="done", service="plumber")] == ["q3"]
    assert [row.id for row in repository.list_quotes_for_user("u1", limit=1)] == ["q3"]


def test_confirmed_quote_for_slot_excludes_the_given_quote_and_other_slots(repository: Repository) -> None:
    repository.add_quote(quote("q1", status="confirmed"))
    repository.add_quote(quote("q2", status="shown"))
    repository.add_quote(quote("q3", status="confirmed", start=SLOT + timedelta(hours=3)))
    repository.add_quote(quote("q4", status="confirmed", action="cancel"))
    found = repository.confirmed_quote_for_slot("u1", "p1", SLOT, exclude_quote_id="q2")
    assert found is not None and found.id == "q1"
    assert repository.confirmed_quote_for_slot("u1", "p1", SLOT, exclude_quote_id="q1") is None
    assert repository.confirmed_quote_for_slot("u2", "p1", SLOT) is None
    assert repository.confirmed_quote_for_slot("u1", "p2", SLOT) is None


# ---------------------------------------------------------------- bookings


def test_booking_add_get_save(repository: Repository) -> None:
    repository.add_booking(booking())
    assert repository.get_booking("b1") == booking()
    assert repository.get_booking("missing") is None
    moved = booking().model_copy(update={"start": SLOT + timedelta(days=1), "updated_at": later(5)})
    repository.save_booking(moved)
    assert repository.get_booking("b1") == moved
    with pytest.raises(LookupError):
        repository.save_booking(booking("unknown"))


def test_a_second_booked_booking_for_the_same_slot_is_refused(repository: Repository) -> None:
    repository.add_booking(booking("b1"))
    with pytest.raises(RequestConflict):
        repository.add_booking(booking("b2"))
    with pytest.raises(RequestConflict):
        repository.add_booking(booking("b1", start=SLOT + timedelta(days=2)))
    repository.add_booking(booking("b3", user_id="u2"))
    repository.add_booking(booking("b4", provider_id="p2"))
    repository.add_booking(booking("b5", start=SLOT + timedelta(hours=5)))
    assert len(repository.booked_all()) == 4


def test_a_cancelled_slot_can_be_booked_again_but_not_reactivated_over_a_booked_one(repository: Repository) -> None:
    repository.add_booking(booking("b1", status="cancelled"))
    repository.add_booking(booking("b2"))
    with pytest.raises(RequestConflict):
        repository.save_booking(booking("b1", status="booked"))
    repository.save_booking(booking("b2", status="cancelled_by_provider"))
    repository.save_booking(booking("b1", status="booked"))
    assert [row.id for row in repository.booked_all()] == ["b1"]


def test_booked_for_slot_ignores_other_statuses_and_users(repository: Repository) -> None:
    repository.add_booking(booking("b1", status="cancelled"))
    assert repository.booked_for_slot("u1", "p1", SLOT) is None
    repository.add_booking(booking("b2"))
    found = repository.booked_for_slot("u1", "p1", SLOT)
    assert found is not None and found.id == "b2"
    assert repository.booked_for_slot("u2", "p1", SLOT) is None
    assert repository.booked_for_slot("u1", "p1", SLOT + timedelta(hours=1)) is None


def test_booked_since_counts_every_status(repository: Repository) -> None:
    repository.add_booking(booking("b1", created=NOW - timedelta(days=2), start=SLOT))
    repository.add_booking(booking("b2", created=NOW, start=SLOT + timedelta(hours=1)))
    repository.add_booking(booking("b3", status="cancelled", created=NOW, start=SLOT + timedelta(hours=2)))
    repository.add_booking(booking("b4", user_id="u2", created=NOW))
    assert repository.booked_since("u1", NOW - timedelta(days=1)) == 2
    assert repository.booked_since("u1", NOW - timedelta(days=3)) == 3
    assert repository.booked_since("nobody", NOW - timedelta(days=3)) == 0


def test_booked_all_returns_only_booked(repository: Repository) -> None:
    repository.add_booking(booking("b1"))
    repository.add_booking(booking("b2", status="cancelled", provider_id="p2"))
    repository.add_booking(booking("b3", user_id="u2", provider_id="p3"))
    assert sorted(row.id for row in repository.booked_all()) == ["b1", "b3"]


def test_list_bookings_filters(repository: Repository) -> None:
    day_one = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)  # 15:30 IST on 3 November
    late_night = datetime(2026, 11, 3, 19, 0, tzinfo=UTC)  # 00:30 IST on 4 November
    repository.add_booking(booking("b1", start=day_one, service="plumber"))
    repository.add_booking(booking("b2", start=late_night, service="Electrician", provider_id="p2"))
    repository.add_booking(booking("b3", start=day_one + timedelta(days=5), status="cancelled", provider_id="p3"))
    repository.add_booking(booking("b4", user_id="u2", start=day_one, provider_id="p4"))

    def ids(**filters: Any) -> list[str]:
        return [row.id for row in repository.list_bookings_for_user("u1", **filters)]

    assert ids() == ["b1", "b2", "b3"]
    assert ids(service="electrician") == ["b2"]
    assert ids(status="cancelled") == ["b3"]
    assert ids(date_from=date(2026, 11, 4)) == ["b2", "b3"]
    assert ids(date_to=date(2026, 11, 3)) == ["b1"]
    assert ids(date_from=date(2026, 11, 3), date_to=date(2026, 11, 4)) == ["b1", "b2"]
    assert ids(service="plumber", status="booked", date_to=date(2026, 11, 30)) == ["b1"]


# ---------------------------------------------------------------- profiles


def test_a_missing_profile_is_empty(repository: Repository) -> None:
    assert repository.get_profile("u1") == UserProfile(user_id="u1")


def test_profile_save_replaces_and_stamps_updated_at(repository: Repository) -> None:
    profile = UserProfile(
        user_id="u1",
        name="Asha",
        phones=("+91 90000 00000",),
        addresses=(Address(label="Home", address="12 5th Cross, Koramangala"),),
    )
    saved = repository.save_profile(profile, now=NOW)
    assert saved.updated_at == NOW
    assert repository.get_profile("u1") == saved
    renamed = repository.save_profile(saved.model_copy(update={"name": "Asha K", "addresses": ()}), now=later(3))
    assert repository.get_profile("u1") == renamed
    assert renamed.addresses == () and renamed.updated_at == later(3)
    assert repository.get_profile("u2") == UserProfile(user_id="u2")


# ---------------------------------------------------------------- memory


def test_memory_is_saved_in_the_profile_and_scoped_to_the_user(repository: Repository) -> None:
    inferred = {"earliest_time": {"value": "10:00", "source": "inferred", "confirmed": False, "asked": True}}
    remember(repository, AREA | inferred)
    assert repository.get_profile("u1").memory == AREA | inferred
    assert repository.get_profile("u2").memory == {}
    remember(repository, {}, "u2")
    assert repository.get_profile("u1").memory == AREA | inferred


def test_saving_a_profile_without_a_key_removes_it(repository: Repository) -> None:
    remember(repository, AREA | {"max_budget": {"value": 1500, "source": "stated", "confirmed": True}})
    remember(repository, AREA)
    assert list(repository.get_profile("u1").memory) == ["area"]
    assert repository.get_profile("u1").name is None  # memory changes leave the other profile fields alone


def test_conversation_summary_round_trips(repository: Repository) -> None:
    saved = repository.save_conversation(conversation(), expected_version=0)
    summarized = saved.model_copy(update={"summary": "Asked about plumbers.", "summarized_count": 4})
    repository.save_conversation(summarized, expected_version=1)
    loaded = repository.get_conversation("c1")
    assert loaded is not None and loaded.summary == "Asked about plumbers." and loaded.summarized_count == 4


# ---------------------------------------------------------------- settings and counts


def test_settings_round_trip_and_overwrite(repository: Repository) -> None:
    assert repository.get_setting("runtime") is None
    repository.put_setting("runtime", {"a": 1}, NOW)
    repository.put_setting("runtime", {"a": 2}, NOW)
    assert repository.get_setting("runtime") == {"a": 2}


def test_counts_and_recent_rows(repository: Repository) -> None:
    repository.save_conversation(conversation(), expected_version=0)
    repository.add_search(search())
    repository.add_quote(quote("q1", status="shown", created=NOW))
    repository.add_quote(quote("q2", status="done", created=later(1)))
    repository.add_booking(booking("b1", created=NOW))
    repository.add_booking(booking("b2", status="cancelled", provider_id="p2", created=later(1)))
    enqueue(repository, "q1")
    counts = {key: value for key, value in repository.counts().items() if key != "memory"}
    assert counts == {
        "conversations": 1,
        "searches": 1,
        "quotes": {"shown": 1, "done": 1},
        "bookings": {"booked": 1, "cancelled": 1},
        "jobs": {"pending": 1},
    }
    assert [row["id"] for row in repository.recent_quotes()] == ["q2", "q1"]
    assert [row["id"] for row in repository.recent_quotes(limit=1)] == ["q2"]
    assert [row["id"] for row in repository.recent_bookings()] == ["b2", "b1"]
    assert repository.recent_bookings()[0]["start"].startswith("2026-11-03T09:00:00")


# ---------------------------------------------------------------- transactions


def write_everything(repository: Repository) -> None:
    repository.save_conversation(conversation(), expected_version=0)
    repository.add_search(search())
    repository.add_quote(quote())
    repository.add_booking(booking())
    repository.save_profile(UserProfile(user_id="u1", name="Asha"), now=NOW)
    repository.put_setting("runtime", {"seed": 2}, NOW)
    enqueue(repository)


def run_in_tx(repository: Repository, body: Callable[[], None]) -> None:
    with repository.tx():
        body()


def run_then_fail(repository: Repository, body: Callable[[], object]) -> None:
    """Run ``body`` in a transaction, then raise so the transaction rolls back."""
    with repository.tx():
        body()
        raise RuntimeError("boom")


def assert_nothing_written(repository: Repository) -> None:
    assert repository.get_conversation("c1") is None
    assert repository.get_search("s1") is None
    assert repository.get_quote("q1") is None
    assert repository.get_booking("b1") is None
    assert repository.get_profile("u1").name is None
    assert repository.get_setting("runtime") is None
    assert repository.jobs_for_quote("q1") == []


def test_tx_rolls_back_every_change_on_exception(repository: Repository) -> None:
    with pytest.raises(RuntimeError):
        run_then_fail(repository, lambda: write_everything(repository))
    assert_nothing_written(repository)


def test_tx_rollback_restores_changed_rows(repository: Repository) -> None:
    repository.add_quote(quote())
    remember(repository)
    repository.save_conversation(conversation(), expected_version=0)

    def change_three_tables() -> None:
        repository.save_quote(quote().model_copy(update={"status": "confirmed"}))
        remember(repository, {})
        repository.save_conversation(conversation(texts=("hi",)), expected_version=1)

    with pytest.raises(RuntimeError):
        run_then_fail(repository, change_three_tables)
    stored = repository.get_quote("q1")
    assert stored is not None and stored.status == "shown"
    assert repository.get_profile("u1").memory == AREA
    loaded = repository.get_conversation("c1")
    assert loaded is not None and loaded.version == 1 and loaded.messages == ()


def test_tx_commits_when_the_block_ends_normally(repository: Repository) -> None:
    with repository.tx():
        write_everything(repository)
    assert repository.get_quote("q1") is not None
    assert len(repository.jobs_for_quote("q1")) == 1
    assert repository.get_setting("runtime") == {"seed": 2}


def test_nested_tx_exception_rolls_back_the_outer_one(repository: Repository) -> None:

    def outer() -> None:
        repository.add_quote(quote())
        run_then_fail(repository, lambda: remember(repository))

    with pytest.raises(RuntimeError):
        run_in_tx(repository, outer)
    assert_nothing_written(repository)


def test_a_failed_write_rolls_back_earlier_writes_in_its_tx(repository: Repository) -> None:
    repository.save_conversation(conversation(), expected_version=0)

    def add_then_conflict() -> None:
        repository.add_quote(quote())
        repository.save_conversation(conversation(), expected_version=0)

    with pytest.raises(VersionConflict):
        run_in_tx(repository, add_then_conflict)
    assert repository.get_quote("q1") is None


def test_a_refused_second_active_job_rolls_back_the_confirm_transaction(repository: Repository) -> None:
    repository.add_quote(quote())
    enqueue(repository)

    def confirm_and_enqueue() -> None:
        repository.save_quote(quote().model_copy(update={"status": "confirmed"}))
        enqueue(repository)

    with pytest.raises(RequestConflict):
        run_in_tx(repository, confirm_and_enqueue)
    stored = repository.get_quote("q1")
    assert stored is not None and stored.status == "shown"


# ---------------------------------------------------------------- jobs


def test_enqueue_returns_a_pending_job(repository: Repository) -> None:
    job = enqueue(repository, run_at=later(5))
    assert (job.kind, job.quote_id, job.status, job.attempts, job.max_attempts) == ("book", "q1", "pending", 0, 5)
    assert job.payload == {"quote_id": "q1"} and job.run_at == later(5)
    assert repository.get_job(job.id) == job
    assert repository.get_job("missing") is None


def test_a_quote_has_at_most_one_pending_or_running_job(repository: Repository) -> None:
    first = enqueue(repository)
    with pytest.raises(RequestConflict):
        enqueue(repository, kind="check")
    claimed = repository.claim_job(now=later(1), lease=LEASE)
    assert claimed is not None and claimed.status == "running"
    with pytest.raises(RequestConflict):
        enqueue(repository)
    repository.complete_job(first.id, now=later(2))
    second = enqueue(repository, kind="check", created=later(3))
    assert [job.id for job in repository.jobs_for_quote("q1")] == [first.id, second.id]


@pytest.mark.parametrize("finish", ["done", "failed", "dead"])
def test_a_finished_job_does_not_block_a_new_job_for_the_quote(repository: Repository, finish: str) -> None:
    first = enqueue(repository)
    if finish == "done":
        repository.complete_job(first.id, now=later(1))
    elif finish == "failed":
        repository.mark_job_failed(first.id, error="not applied", now=later(1))
    else:
        repository.fail_job(first.id, error="gave up", now=later(1), retry_at=None)
    again = enqueue(repository, created=later(2))
    assert again.id != first.id
    stored = repository.get_job(first.id)
    assert stored is not None and stored.status == finish


def test_jobs_without_a_quote_and_other_quotes_are_not_limited(repository: Repository) -> None:
    enqueue(repository, None, kind="memory_update")
    enqueue(repository, None, kind="memory_update")
    enqueue(repository, "q1")
    enqueue(repository, "q2")
    assert len(repository.recent_jobs()) == 4


def test_enqueue_inside_a_failing_tx_is_rolled_back(repository: Repository) -> None:
    with pytest.raises(RuntimeError):
        run_then_fail(repository, lambda: enqueue(repository))
    assert repository.recent_jobs() == []
    enqueue(repository)


def test_claim_returns_none_when_nothing_is_due(repository: Repository) -> None:
    assert repository.claim_job(now=NOW, lease=LEASE) is None


def test_claim_respects_run_at_and_picks_the_oldest_due_job(repository: Repository) -> None:
    future = enqueue(repository, "q1", run_at=later(10))
    older = enqueue(repository, "q2", created=NOW)
    newer = enqueue(repository, "q3", created=later(1))
    assert repository.claim_job(now=later(2), lease=timedelta(hours=1)) == repository.get_job(older.id)
    claimed = repository.claim_job(now=later(2), lease=timedelta(hours=1))
    assert claimed is not None and claimed.id == newer.id
    assert repository.claim_job(now=later(2), lease=timedelta(hours=1)) is None
    late = repository.claim_job(now=later(10), lease=timedelta(hours=1))
    assert late is not None and late.id == future.id


def test_claim_marks_the_job_running_with_a_lease_and_counts_the_attempt(repository: Repository) -> None:
    job = enqueue(repository)
    claimed = repository.claim_job(now=later(1), lease=LEASE)
    assert claimed is not None
    assert (claimed.id, claimed.status, claimed.attempts) == (job.id, "running", 1)
    assert claimed.locked_until == later(1) + LEASE


def test_running_job_is_claimable_only_after_its_lease_expires(repository: Repository) -> None:
    job = enqueue(repository)
    repository.claim_job(now=NOW, lease=LEASE)
    assert repository.claim_job(now=NOW + LEASE - timedelta(seconds=1), lease=LEASE) is None
    again = repository.claim_job(now=NOW + LEASE, lease=LEASE)
    assert again is not None and again.id == job.id and again.attempts == 2


def test_complete_marks_the_job_done_and_not_claimable(repository: Repository) -> None:
    job = enqueue(repository)
    repository.claim_job(now=NOW, lease=LEASE)
    repository.complete_job(job.id, now=later(1))
    stored = repository.get_job(job.id)
    assert stored is not None and stored.status == "done" and stored.locked_until is None
    assert repository.claim_job(now=later(60), lease=LEASE) is None


def test_fail_with_retry_at_returns_the_job_to_pending_at_that_time(repository: Repository) -> None:
    job = enqueue(repository)
    repository.claim_job(now=NOW, lease=LEASE)
    repository.fail_job(job.id, error="timeout", now=later(1), retry_at=later(6))
    stored = repository.get_job(job.id)
    assert stored is not None and (stored.status, stored.last_error, stored.run_at) == ("pending", "timeout", later(6))
    assert repository.claim_job(now=later(5), lease=LEASE) is None
    assert repository.claim_job(now=later(6), lease=LEASE) is not None


def test_fail_without_retry_at_makes_the_job_dead_and_truncates_the_error(repository: Repository) -> None:
    job = enqueue(repository)
    repository.claim_job(now=NOW, lease=LEASE)
    repository.fail_job(job.id, error="x" * 5000, now=later(1), retry_at=None)
    stored = repository.get_job(job.id)
    assert stored is not None and stored.status == "dead" and stored.last_error == "x" * 2000
    assert repository.claim_job(now=later(60), lease=LEASE) is None


def test_mark_job_failed_ends_the_job_without_retry(repository: Repository) -> None:
    job = enqueue(repository)
    repository.claim_job(now=NOW, lease=LEASE)
    repository.mark_job_failed(job.id, error="provider said not_found", now=later(1))
    stored = repository.get_job(job.id)
    assert stored is not None and stored.status == "failed" and stored.last_error == "provider said not_found"
    assert repository.claim_job(now=later(60), lease=LEASE) is None


def test_finishing_an_unknown_job_does_nothing(repository: Repository) -> None:
    repository.complete_job("missing", now=NOW)
    repository.fail_job("missing", error="x", now=NOW, retry_at=None)
    repository.mark_job_failed("missing", error="x", now=NOW)
    assert repository.recent_jobs() == []


def test_recent_jobs_are_newest_first_and_limited(repository: Repository) -> None:
    ids = [enqueue(repository, f"q{index}", created=later(index)).id for index in range(4)]
    assert [job.id for job in repository.recent_jobs()] == ids[::-1]
    assert [job.id for job in repository.recent_jobs(limit=2)] == ids[:1:-1]


def test_concurrent_claimers_never_get_the_same_job(repository: Repository) -> None:
    expected = {enqueue(repository, f"q{index}", created=NOW + timedelta(seconds=index)).id for index in range(20)}

    def drain() -> list[str]:
        claimed: list[str] = []
        while (job := repository.claim_job(now=NOW + timedelta(hours=1), lease=LEASE)) is not None:
            claimed.append(job.id)
        return claimed

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in [pool.submit(drain) for _ in range(4)]]
    claimed_ids = [job_id for result in results for job_id in result]
    assert len(claimed_ids) == 20
    assert set(claimed_ids) == expected
