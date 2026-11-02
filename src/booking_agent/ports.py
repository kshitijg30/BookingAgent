"""Persistence contract used by the application; no database or web dependencies.

One protocol per table group, so a caller depends on only what it uses:

- ``ConversationStore``: conversations with their messages, versioned for optimistic concurrency.
- ``SearchStore``: searches (criteria and results snapshots).
- ``QuoteStore``: quotes, the approval records.
- ``BookingStore``: bookings the agent made and verified.
- ``ProfileStore``: name, phones, addresses and the memory JSON per user.
- ``JobQueue``: background jobs.
- ``SettingsRepository``: runtime settings saved from the admin page.
- ``InspectionRepository``: admin-only reads, returned as JSON-ready dicts.
- ``Coordination``: transactions, cross-process locks and shutdown.

``Repository`` combines all of them and is what the agent and the engine take.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

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
from booking_agent.domain.models import IST
from booking_agent.rows import Job


def ist_day_bounds(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """Start (inclusive) and end (exclusive) instants for inclusive IST days; None for an open end."""
    lower = datetime.combine(date_from, time.min, tzinfo=IST) if date_from else None
    upper = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=IST) if date_to else None
    return lower, upper


class ConversationStore(Protocol):
    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    def save_conversation(self, conversation: Conversation, *, expected_version: int) -> Conversation:
        """Save if the stored version is ``expected_version`` (0 for a new conversation).

        Raises ``VersionConflict`` otherwise. Returns the conversation with ``version`` set to
        ``expected_version + 1``. Appending a message is a save of the longer message tuple.
        """
        ...

    def list_conversations(self, user_id: str, *, limit: int = 100) -> list[Conversation]:
        """The user's conversations, newest ``updated_at`` first."""
        ...


class SearchStore(Protocol):
    def add_search(self, search: Search) -> None: ...
    def get_search(self, search_id: str) -> Search | None: ...
    def newest_search_for_conversation(self, conversation_id: str) -> Search | None: ...


class QuoteStore(Protocol):
    def add_quote(self, quote: Quote) -> None: ...

    def save_quote(self, quote: Quote) -> None:
        """Replace the stored quote with the same id. Raises ``LookupError`` if there is none."""
        ...

    def get_quote(self, quote_id: str) -> Quote | None: ...

    def current_quote_for_conversation(self, conversation_id: str) -> Quote | None:
        """The newest quote of the conversation with status ``shown`` or ``confirmed``."""
        ...

    def list_quotes_for_user(
        self, user_id: str, *, status: QuoteStatus | None = None, service: str | None = None, limit: int = 100
    ) -> list[Quote]:
        """Newest first. ``service`` matches case-insensitively."""
        ...

    def confirmed_quote_for_slot(
        self, user_id: str, provider_id: str, start: datetime, *, exclude_quote_id: str | None = None
    ) -> Quote | None:
        """A confirmed ``book`` or ``reschedule`` quote for this slot, for the double-booking check."""
        ...


class BookingStore(Protocol):
    def add_booking(self, booking: Booking) -> None:
        """Insert. Raises ``RequestConflict`` if the id exists or the user already has a ``booked``
        booking with the same provider and start."""
        ...

    def save_booking(self, booking: Booking) -> None:
        """Replace the stored booking with the same id. Raises ``LookupError`` if there is none."""
        ...

    def get_booking(self, booking_id: str) -> Booking | None: ...

    def list_bookings_for_user(
        self,
        user_id: str,
        *,
        service: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: BookingStatus | None = None,
    ) -> list[Booking]:
        """Ordered by ``start``. The dates are inclusive days in IST and filter on ``start``."""
        ...

    def booked_for_slot(self, user_id: str, provider_id: str, start: datetime) -> Booking | None:
        """The user's ``booked`` booking with this provider at this start, if any."""
        ...

    def booked_since(self, user_id: str, since: datetime) -> int:
        """How many bookings the user created at or after ``since``, in any status (the daily cap)."""
        ...

    def booked_all(self) -> list[Booking]:
        """Every booking with status ``booked``, for the provider-cancellation scan."""
        ...


class ProfileStore(Protocol):
    def get_profile(self, user_id: str) -> UserProfile:
        """The saved profile, or an empty one (no name, phones, addresses or memory) if none."""
        ...

    def save_profile(self, profile: UserProfile, *, now: datetime) -> UserProfile:
        """Insert or replace. Returns the profile with ``updated_at`` set to ``now``."""
        ...


class JobQueue(Protocol):
    """Background jobs stored next to the state they change (see ``rows.Job`` for the life of a job).

    Enqueue a job inside the same ``tx()`` as the state change that needs it, so both commit or neither
    does (a transactional outbox). There is no ``dedupe_key``: the one-active-job-per-quote rule below
    replaces it.
    """

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
        """Add a pending job.

        A quote has at most one ``pending`` or ``running`` job: another one raises ``RequestConflict``.
        ``done``, ``failed`` and ``dead`` jobs do not count. ``quote_id`` None (``memory_update``) is
        never limited.
        """
        ...

    def claim_job(self, *, now: datetime, lease: timedelta) -> Job | None:
        """Atomically take the oldest due job and mark it running until ``now + lease``.

        Due means ``pending`` with ``run_at <= now``, or ``running`` with ``locked_until <= now``.
        Claiming increments ``attempts``. Returns None when nothing is due. Two workers never claim
        the same job.
        """
        ...

    def complete_job(self, job_id: str, *, now: datetime) -> None:
        """Status ``done``."""
        ...

    def fail_job(self, job_id: str, *, error: str, now: datetime, retry_at: datetime | None) -> None:
        """Record a failed attempt. ``retry_at`` set: back to ``pending`` at that time. None: ``dead``."""
        ...

    def mark_job_failed(self, job_id: str, *, error: str, now: datetime) -> None:
        """Status ``failed``: the provider verifiably did not apply the write. No retry of this job."""
        ...

    def get_job(self, job_id: str) -> Job | None: ...
    def jobs_for_quote(self, quote_id: str) -> list[Job]:
        """Oldest first."""
        ...


class SettingsRepository(Protocol):
    def get_setting(self, key: str) -> dict[str, Any] | None: ...
    def put_setting(self, key: str, value: dict[str, Any], now: datetime) -> None: ...


class InspectionRepository(Protocol):
    """Admin-only reads. Rows are JSON-ready dicts (jobs are ``Job`` rows) passed to the admin page."""

    def counts(self) -> dict[str, Any]:
        """Row counts: ``conversations``, ``searches`` and, by status, ``quotes``,
        ``bookings`` and ``jobs``."""
        ...

    def recent_quotes(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def recent_bookings(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def recent_jobs(self, limit: int = 50) -> list[Job]:
        """Newest first."""
        ...


class Coordination(Protocol):
    def coordination_lock(self, key: str) -> AbstractContextManager[object]:
        """Serialize work on ``key`` across threads, and across processes for durable stores."""
        ...

    def tx(self) -> AbstractContextManager[object]:
        """One transaction. A nested ``tx`` joins the outer one; an exception rolls all of it back."""
        ...

    def close(self) -> None: ...


class Repository(
    ConversationStore,
    SearchStore,
    QuoteStore,
    BookingStore,
    ProfileStore,
    JobQueue,
    SettingsRepository,
    InspectionRepository,
    Coordination,
    Protocol,
):
    """Everything the agent and the engine persist."""
