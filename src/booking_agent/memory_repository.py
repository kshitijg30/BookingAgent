"""Thread-safe in-memory repository for embedding, examples, and disposable workloads.

State lasts for the lifetime of this object. Use the SQLAlchemy adapter for durability.
"""

from __future__ import annotations

import copy
import dataclasses
import threading
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from booking_agent.domain.entities import (
    Booking,
    BookingStatus,
    Conversation,
    JobKind,
    Quote,
    QuoteStatus,
    Search,
    UserProfile,
)
from booking_agent.errors import RequestConflict, VersionConflict
from booking_agent.locks import KeyedLocks
from booking_agent.ports import ist_day_bounds
from booking_agent.rows import Job

_ABSENT = object()  # journal marker: the key did not exist before the transaction
_ACTIVE_JOB_STATUSES = ("pending", "running")


class _UndoJournal:
    """What the current transaction changed, so it can be undone.

    Keeps the original of each row the first time the transaction changes it. Rows are immutable
    entities, so keeping the reference is a safe copy.
    """

    def __init__(self) -> None:
        self.originals: dict[tuple[str, str], Any] = {}

    def remember(self, table_name: str, table: dict[str, Any], key: str) -> None:
        if (table_name, key) not in self.originals:
            self.originals[(table_name, key)] = copy.deepcopy(table[key]) if key in table else _ABSENT


class MemoryRepository:
    """In-memory ``Repository``. One re-entrant lock serializes all access.

    Rollback cost: ``tx()`` does not copy the whole store. Each change inside a transaction first
    saves the row it touches (once per row per transaction), so a rollback costs time in
    proportion to the rows that transaction changed. Every write method runs inside ``tx()``.
    """

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._searches: dict[str, Search] = {}
        self._quotes: dict[str, Quote] = {}
        self._bookings: dict[str, Booking] = {}
        self._profiles: dict[str, UserProfile] = {}
        self._settings: dict[str, Any] = {}
        self._jobs: dict[str, Job] = {}
        self._tables: dict[str, dict[str, Any]] = {
            "conversations": self._conversations,
            "searches": self._searches,
            "quotes": self._quotes,
            "bookings": self._bookings,
            "profiles": self._profiles,
            "settings": self._settings,
            "jobs": self._jobs,
        }
        self._lock = threading.RLock()
        self._journal: _UndoJournal | None = None  # set while a transaction is open (the lock is held)
        self._keys = KeyedLocks()

    # ---------- coordination ----------
    def close(self) -> None:
        """No external resources are owned. Data remains available to its owner."""

    def coordination_lock(self, key: str) -> AbstractContextManager[object]:
        return self._keys.get(key)

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Hold the lock for the transaction. A nested ``tx`` joins the outer one."""
        with self._lock:
            if self._journal is not None:
                yield
                return
            journal = self._journal = _UndoJournal()
            try:
                yield
            except BaseException:
                self._undo(journal)
                raise
            finally:
                self._journal = None

    def _undo(self, journal: _UndoJournal) -> None:
        for (table_name, key), original in journal.originals.items():
            table = self._tables[table_name]
            if original is _ABSENT:
                table.pop(key, None)
            else:
                table[key] = original

    def _put(self, table_name: str, key: str, row: Any) -> None:
        """Journal and write one row. Call inside ``tx()``."""
        if self._journal is not None:
            self._journal.remember(table_name, self._tables[table_name], key)
        self._tables[table_name][key] = row

    # ---------- conversations ----------
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    def save_conversation(self, conversation: Conversation, *, expected_version: int) -> Conversation:
        with self.tx():
            stored = self._conversations.get(conversation.id)
            if (stored and stored.version != expected_version) or (not stored and expected_version != 0):
                raise VersionConflict(conversation.id)
            update: dict[str, Any] = {"version": expected_version + 1}
            if stored:
                update |= {"user_id": stored.user_id, "created_at": stored.created_at}
            saved = conversation.model_copy(update=update)
            self._put("conversations", saved.id, saved)
            return saved

    def list_conversations(self, user_id: str, *, limit: int = 100) -> list[Conversation]:
        with self._lock:
            mine = [row for row in self._conversations.values() if row.user_id == user_id]
            return sorted(mine, key=lambda row: (row.updated_at, row.id), reverse=True)[:limit]

    # ---------- searches ----------
    def add_search(self, search: Search) -> None:
        with self.tx():
            if search.id in self._searches:
                raise RequestConflict("duplicate search")
            self._put("searches", search.id, search)

    def get_search(self, search_id: str) -> Search | None:
        with self._lock:
            return self._searches.get(search_id)

    def newest_search_for_conversation(self, conversation_id: str) -> Search | None:
        with self._lock:
            mine = [row for row in self._searches.values() if row.conversation_id == conversation_id]
            return max(mine, key=lambda row: (row.created_at, row.id), default=None)

    # ---------- quotes ----------
    def add_quote(self, quote: Quote) -> None:
        with self.tx():
            if quote.id in self._quotes:
                raise RequestConflict("duplicate quote")
            self._put("quotes", quote.id, quote)

    def save_quote(self, quote: Quote) -> None:
        with self.tx():
            if quote.id not in self._quotes:
                raise LookupError(quote.id)
            self._put("quotes", quote.id, quote)

    def get_quote(self, quote_id: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(quote_id)

    def current_quote_for_conversation(self, conversation_id: str) -> Quote | None:
        with self._lock:
            open_quotes = [
                row
                for row in self._quotes.values()
                if row.conversation_id == conversation_id and row.status in ("shown", "confirmed")
            ]
            return max(open_quotes, key=lambda row: (row.created_at, row.id), default=None)

    def list_quotes_for_user(
        self, user_id: str, *, status: QuoteStatus | None = None, service: str | None = None, limit: int = 100
    ) -> list[Quote]:
        with self._lock:
            rows = [
                row
                for row in self._quotes.values()
                if row.user_id == user_id
                and (status is None or row.status == status)
                and (service is None or row.service.lower() == service.lower())
            ]
            return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)[:limit]

    def confirmed_quote_for_slot(
        self, user_id: str, provider_id: str, start: datetime, *, exclude_quote_id: str | None = None
    ) -> Quote | None:
        with self._lock:
            rows = [
                row
                for row in self._quotes.values()
                if row.user_id == user_id
                and row.provider_id == provider_id
                and row.start == start
                and row.status == "confirmed"
                and row.action in ("book", "reschedule")
                and row.id != exclude_quote_id
            ]
            return min(rows, key=lambda row: (row.created_at, row.id), default=None)

    # ---------- bookings ----------
    def _booked_clash(self, booking: Booking) -> bool:
        if booking.id in self._bookings:
            return True
        return booking.status == "booked" and any(
            row.id != booking.id
            and row.status == "booked"
            and (row.user_id, row.provider_id, row.start) == (booking.user_id, booking.provider_id, booking.start)
            for row in self._bookings.values()
        )

    def add_booking(self, booking: Booking) -> None:
        with self.tx():
            if self._booked_clash(booking):
                raise RequestConflict("booking id or booked slot already exists")
            self._put("bookings", booking.id, booking)

    def save_booking(self, booking: Booking) -> None:
        with self.tx():
            if booking.id not in self._bookings:
                raise LookupError(booking.id)
            others = {key: row for key, row in self._bookings.items() if key != booking.id}
            if booking.status == "booked" and any(
                row.status == "booked"
                and (row.user_id, row.provider_id, row.start) == (booking.user_id, booking.provider_id, booking.start)
                for row in others.values()
            ):
                raise RequestConflict("the user already has a booked booking for this slot")
            self._put("bookings", booking.id, booking)

    def get_booking(self, booking_id: str) -> Booking | None:
        with self._lock:
            return self._bookings.get(booking_id)

    def list_bookings_for_user(
        self,
        user_id: str,
        *,
        service: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: BookingStatus | None = None,
    ) -> list[Booking]:
        lower, upper = ist_day_bounds(date_from, date_to)
        with self._lock:
            rows = [
                row
                for row in self._bookings.values()
                if row.user_id == user_id
                and (service is None or row.service.lower() == service.lower())
                and (status is None or row.status == status)
                and (lower is None or row.start >= lower)
                and (upper is None or row.start < upper)
            ]
            return sorted(rows, key=lambda row: (row.start, row.id))

    def booked_for_slot(self, user_id: str, provider_id: str, start: datetime) -> Booking | None:
        with self._lock:
            return next(
                (
                    row
                    for row in self._bookings.values()
                    if (row.user_id, row.provider_id, row.start) == (user_id, provider_id, start)
                    and row.status == "booked"
                ),
                None,
            )

    def booked_since(self, user_id: str, since: datetime) -> int:
        with self._lock:
            return sum(row.user_id == user_id and row.created_at >= since for row in self._bookings.values())

    def booked_all(self) -> list[Booking]:
        with self._lock:
            return sorted(
                (row for row in self._bookings.values() if row.status == "booked"),
                key=lambda row: (row.start, row.id),
            )

    # ---------- profiles ----------
    def get_profile(self, user_id: str) -> UserProfile:
        with self._lock:
            profile = self._profiles.get(user_id)
            return profile.model_copy(deep=True) if profile else UserProfile(user_id=user_id)

    def save_profile(self, profile: UserProfile, *, now: datetime) -> UserProfile:
        with self.tx():
            saved = profile.model_copy(update={"updated_at": now}, deep=True)
            self._put("profiles", saved.user_id, saved)
            return saved

    # ---------- job queue ----------
    @staticmethod
    def _job_copy(job: Job) -> Job:
        return dataclasses.replace(job, payload=copy.deepcopy(job.payload))

    @staticmethod
    def _is_due(job: Job, now: datetime) -> bool:
        if job.status == "pending":
            return job.run_at <= now
        return job.status == "running" and job.locked_until is not None and job.locked_until <= now

    def enqueue_job(
        self,
        *,
        kind: JobKind,
        quote_id: str | None,
        payload: dict[str, Any],
        now: datetime,
        run_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> Job:
        with self.tx():
            if quote_id is not None and any(
                job.quote_id == quote_id and job.status in _ACTIVE_JOB_STATUSES for job in self._jobs.values()
            ):
                raise RequestConflict("quote already has an active job")
            job = Job(
                id=uuid.uuid4().hex,
                kind=kind,
                quote_id=quote_id,
                payload=copy.deepcopy(payload),
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                run_at=run_at or now,
                locked_until=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            self._put("jobs", job.id, job)
            return self._job_copy(job)

    def _change_job(self, job_id: str, **changes: Any) -> None:
        """Apply ``changes`` to a stored job, journaled. An unknown id does nothing."""
        job = self._jobs.get(job_id)
        if job is not None:
            self._put("jobs", job_id, dataclasses.replace(job, **changes))

    def claim_job(self, *, now: datetime, lease: timedelta) -> Job | None:
        with self.tx():
            due = [job for job in self._jobs.values() if self._is_due(job, now)]
            if not due:
                return None
            oldest = min(due, key=lambda job: (job.run_at, job.created_at, job.id))
            self._change_job(
                oldest.id, status="running", locked_until=now + lease, attempts=oldest.attempts + 1, updated_at=now
            )
            return self._job_copy(self._jobs[oldest.id])

    def complete_job(self, job_id: str, *, now: datetime) -> None:
        with self.tx():
            self._change_job(job_id, status="done", locked_until=None, updated_at=now)

    def fail_job(self, job_id: str, *, error: str, now: datetime, retry_at: datetime | None) -> None:
        with self.tx():
            changes: dict[str, Any] = {
                "status": "pending" if retry_at is not None else "dead",
                "last_error": error[:2000],
                "locked_until": None,
                "updated_at": now,
            }
            if retry_at is not None:
                changes["run_at"] = retry_at
            self._change_job(job_id, **changes)

    def mark_job_failed(self, job_id: str, *, error: str, now: datetime) -> None:
        with self.tx():
            self._change_job(job_id, status="failed", last_error=error[:2000], locked_until=None, updated_at=now)

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._job_copy(job) if job else None

    def jobs_for_quote(self, quote_id: str) -> list[Job]:
        with self._lock:
            mine = [job for job in self._jobs.values() if job.quote_id == quote_id]
            return [self._job_copy(job) for job in sorted(mine, key=lambda job: (job.created_at, job.id))]

    # ---------- settings ----------
    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._settings.get(key))

    def put_setting(self, key: str, value: dict[str, Any], now: datetime) -> None:
        with self.tx():
            self._put("settings", key, copy.deepcopy(value))

    # ---------- inspection (admin only) ----------
    def counts(self) -> dict[str, Any]:
        with self._lock:
            return {
                "conversations": len(self._conversations),
                "searches": len(self._searches),
                "quotes": self._counts_by_status(self._quotes.values()),
                "bookings": self._counts_by_status(self._bookings.values()),
                "jobs": self._counts_by_status(self._jobs.values()),
            }

    @staticmethod
    def _counts_by_status(rows: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    def recent_quotes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            newest = sorted(self._quotes.values(), key=lambda row: (row.updated_at, row.id), reverse=True)
            return [row.model_dump(mode="json") for row in newest[:limit]]

    def recent_bookings(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            newest = sorted(self._bookings.values(), key=lambda row: (row.created_at, row.id), reverse=True)
            return [row.model_dump(mode="json") for row in newest[:limit]]

    def recent_jobs(self, limit: int = 50) -> list[Job]:
        with self._lock:
            newest = sorted(self._jobs.values(), key=lambda job: (job.created_at, job.id), reverse=True)
            return [self._job_copy(job) for job in newest[:limit]]
