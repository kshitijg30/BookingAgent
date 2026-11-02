"""Agent-side durable state. Tables, plus runtime settings:

- ``conversations``: chat messages as a JSON column, versioned.
- ``searches``: criteria and results snapshots.
- ``quotes``: one row per exact action shown with Confirm; the approval record.
- ``bookings``: bookings made and verified; at most one ``booked`` per user, provider and start.
- ``jobs``: the background job queue (see ``rows.Job``), enqueued in the transaction of the state it follows.
- ``user_profiles``: what we know about the user (name, phones, addresses, memory JSON).

Traces are files, see tracing.py.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from booking_agent.db import Database, JSONType, TZDateTime
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
from booking_agent.ports import ist_day_bounds
from booking_agent.rows import Job
from booking_agent.schema import Migration

ERROR_LIMIT = 2000
OLD_TABLE_NAMES = ("sessions", "messages", "approvals", "bookings", "jobs")
ACTIVE_JOB_STATUSES = ("pending", "running")

md = sa.MetaData()

conversations = sa.Table(
    "conversations",
    md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("messages", JSONType, nullable=False),
    sa.Column("summary", sa.Text, nullable=False, server_default=""),
    sa.Column("summarized_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_at", TZDateTime, nullable=False),
    sa.Column("updated_at", TZDateTime, nullable=False),
    sa.Index("conversations_user_idx", "user_id", "updated_at"),
)
searches = sa.Table(
    "searches",
    md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("conversation_id", sa.Text, nullable=False),
    sa.Column("criteria", JSONType, nullable=False),
    sa.Column("results", JSONType, nullable=False),
    sa.Column("created_at", TZDateTime, nullable=False),
    sa.Index("searches_conversation_idx", "conversation_id", "created_at"),
)
quotes = sa.Table(
    "quotes",
    md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("conversation_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("search_id", sa.Text),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("booking_id", sa.Text),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("provider_name", sa.Text, nullable=False),
    sa.Column("service", sa.Text, nullable=False),
    sa.Column("area", sa.Text, nullable=False),
    sa.Column("start", TZDateTime, nullable=False),
    sa.Column("end", TZDateTime, nullable=False),
    sa.Column("price", sa.Integer, nullable=False),
    sa.Column("hold_id", sa.Text),
    sa.Column("hold_expires_at", TZDateTime),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("confirmed_at", TZDateTime),
    sa.Column("created_at", TZDateTime, nullable=False),
    sa.Column("updated_at", TZDateTime, nullable=False),
    sa.Index("quotes_conversation_idx", "conversation_id", "created_at"),
    sa.Index("quotes_user_status_idx", "user_id", "status"),
)
bookings = sa.Table(
    "bookings",
    md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("conversation_id", sa.Text, nullable=False),
    sa.Column("quote_id", sa.Text, nullable=False),
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("provider_name", sa.Text, nullable=False),
    sa.Column("service", sa.Text, nullable=False),
    sa.Column("area", sa.Text, nullable=False),
    sa.Column("start", TZDateTime, nullable=False),
    sa.Column("end", TZDateTime, nullable=False),
    sa.Column("price", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("provider_booking_id", sa.Text),
    sa.Column("created_at", TZDateTime, nullable=False),
    sa.Column("updated_at", TZDateTime, nullable=False),
    sa.Index(
        "bookings_one_booked_slot",
        "user_id",
        "provider_id",
        "start",
        unique=True,
        sqlite_where=sa.text("status = 'booked'"),
        postgresql_where=sa.text("status = 'booked'"),
    ),
    sa.Index("bookings_user_start_idx", "user_id", "start"),
)
user_profiles = sa.Table(
    "user_profiles",
    md,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text),
    sa.Column("phones", JSONType, nullable=False),
    sa.Column("addresses", JSONType, nullable=False),
    sa.Column("memory", JSONType, nullable=False, server_default="{}"),
    sa.Column("updated_at", TZDateTime, nullable=False),
)
app_settings = sa.Table(
    "app_settings",
    md,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", JSONType, nullable=False),
    sa.Column("updated_at", TZDateTime, nullable=False),
)
jobs = sa.Table(
    "jobs",
    md,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("quote_id", sa.Text),
    sa.Column("payload", JSONType, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("run_at", TZDateTime, nullable=False),
    sa.Column("locked_until", TZDateTime),
    sa.Column("last_error", sa.Text),
    sa.Column("created_at", TZDateTime, nullable=False),
    sa.Column("updated_at", TZDateTime, nullable=False),
    sa.Index("jobs_status_run_at_idx", "status", "run_at"),
    sa.Index("jobs_quote_idx", "quote_id"),
    sa.Index(
        "jobs_one_active_per_quote",
        "quote_id",
        unique=True,
        sqlite_where=sa.text("status in ('pending', 'running')"),
        postgresql_where=sa.text("status in ('pending', 'running')"),
    ),
)


def _add_jobs_table(connection: Connection) -> None:
    """Schema version 2: databases created before the job queue get a ``jobs`` table.

    Version 3 drops and recreates it, so this only has to leave a table behind to drop.
    """
    legacy = sa.MetaData()
    sa.Table("jobs", legacy, sa.Column("id", sa.Text, primary_key=True))
    legacy.create_all(connection, checkfirst=True)


def _rework_tables(connection: Connection) -> None:
    """Schema version 3: drop the sessions, messages, approvals, bookings and jobs tables of the earlier
    design (demo data only) and create every table of the current design."""
    legacy = sa.MetaData()
    for name in OLD_TABLE_NAMES:
        sa.Table(name, legacy, sa.Column("placeholder", sa.Text))
    legacy.drop_all(connection, checkfirst=True)
    md.create_all(connection)


def _prices_to_rupees(connection: Connection) -> None:
    """Schema version 4: quoted and booked prices were integer paise, now whole rupees."""
    for table in (quotes, bookings):
        connection.execute(table.update().values(price=table.c.price // 100))


MIGRATIONS = (Migration(2, _add_jobs_table), Migration(3, _rework_tables), Migration(4, _prices_to_rupees))


def _job(row: sa.RowMapping) -> Job:
    return Job(**{**row, "payload": dict(row["payload"])})


def _json_row(row: sa.RowMapping) -> dict[str, Any]:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in dict(row).items()}


def _due_jobs_condition(now: datetime) -> sa.ColumnElement[bool]:
    return sa.or_(
        sa.and_(jobs.c.status == "pending", jobs.c.run_at <= now),
        sa.and_(jobs.c.status == "running", jobs.c.locked_until <= now),
    )


def _quote(row: sa.RowMapping | None) -> Quote | None:
    return Quote.model_validate(dict(row)) if row else None


def _booking(row: sa.RowMapping | None) -> Booking | None:
    return Booking.model_validate(dict(row)) if row else None


def _without_id(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key != "id"}


class Store:
    """SQLAlchemy ``Repository`` on SQLite or Postgres. Sections follow the protocols in ports.py."""

    def __init__(self, url_or_path: str | Path) -> None:
        self.db = Database(url_or_path, md, schema="booking_agent", migrations=MIGRATIONS)

    # ---------- coordination ----------
    def coordination_lock(self, key: str) -> AbstractContextManager[object]:
        return self.db.coordination_lock(key)

    def close(self) -> None:
        self.db.close()

    def tx(self) -> AbstractContextManager[object]:
        return self.db.tx()

    # ---------- conversations ----------
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self.db.one(sa.select(conversations).where(conversations.c.id == conversation_id))
        return Conversation.model_validate(dict(row)) if row else None

    def save_conversation(self, conversation: Conversation, *, expected_version: int) -> Conversation:
        saved = conversation.model_copy(update={"version": expected_version + 1})
        values = saved.model_dump(mode="json") | {"created_at": saved.created_at, "updated_at": saved.updated_at}
        changes = {key: value for key, value in values.items() if key not in ("id", "user_id", "created_at")}
        with self.db.tx() as conn:
            updated = conn.execute(
                conversations.update()
                .where(conversations.c.id == saved.id, conversations.c.version == expected_version)
                .values(**changes)
            ).rowcount
            if updated == 1:
                return saved
            exists = conn.execute(sa.select(conversations.c.id).where(conversations.c.id == saved.id)).first()
            if expected_version != 0 or exists:
                raise VersionConflict(saved.id)
            conn.execute(conversations.insert().values(**values))
        return saved

    def list_conversations(self, user_id: str, *, limit: int = 100) -> list[Conversation]:
        rows = self.db.rows(
            sa.select(conversations)
            .where(conversations.c.user_id == user_id)
            .order_by(conversations.c.updated_at.desc(), conversations.c.id.desc())
            .limit(limit)
        )
        return [Conversation.model_validate(dict(row)) for row in rows]

    # ---------- searches ----------
    def add_search(self, search: Search) -> None:
        values = search.model_dump(mode="json", by_alias=True) | {"created_at": search.created_at}
        self.db.exec(searches.insert().values(**values))

    def get_search(self, search_id: str) -> Search | None:
        row = self.db.one(sa.select(searches).where(searches.c.id == search_id))
        return Search.model_validate(dict(row)) if row else None

    def newest_search_for_conversation(self, conversation_id: str) -> Search | None:
        row = self.db.one(
            sa.select(searches)
            .where(searches.c.conversation_id == conversation_id)
            .order_by(searches.c.created_at.desc(), searches.c.id.desc())
            .limit(1)
        )
        return Search.model_validate(dict(row)) if row else None

    # ---------- quotes ----------
    def add_quote(self, quote: Quote) -> None:
        self.db.exec(quotes.insert().values(**quote.model_dump()))

    def save_quote(self, quote: Quote) -> None:
        values = _without_id(quote.model_dump())
        if not self.db.exec(quotes.update().where(quotes.c.id == quote.id).values(**values)):
            raise LookupError(quote.id)

    def get_quote(self, quote_id: str) -> Quote | None:
        return _quote(self.db.one(sa.select(quotes).where(quotes.c.id == quote_id)))

    def current_quote_for_conversation(self, conversation_id: str) -> Quote | None:
        return _quote(
            self.db.one(
                sa.select(quotes)
                .where(quotes.c.conversation_id == conversation_id, quotes.c.status.in_(("shown", "confirmed")))
                .order_by(quotes.c.created_at.desc(), quotes.c.id.desc())
                .limit(1)
            )
        )

    def list_quotes_for_user(
        self, user_id: str, *, status: QuoteStatus | None = None, service: str | None = None, limit: int = 100
    ) -> list[Quote]:
        query = sa.select(quotes).where(quotes.c.user_id == user_id)
        if status is not None:
            query = query.where(quotes.c.status == status)
        if service is not None:
            query = query.where(sa.func.lower(quotes.c.service) == service.lower())
        rows = self.db.rows(query.order_by(quotes.c.created_at.desc(), quotes.c.id.desc()).limit(limit))
        return [Quote.model_validate(dict(row)) for row in rows]

    def confirmed_quote_for_slot(
        self, user_id: str, provider_id: str, start: datetime, *, exclude_quote_id: str | None = None
    ) -> Quote | None:
        query = sa.select(quotes).where(
            quotes.c.user_id == user_id,
            quotes.c.provider_id == provider_id,
            quotes.c.start == start,
            quotes.c.status == "confirmed",
            quotes.c.action.in_(("book", "reschedule")),
        )
        if exclude_quote_id is not None:
            query = query.where(quotes.c.id != exclude_quote_id)
        return _quote(self.db.one(query.order_by(quotes.c.created_at, quotes.c.id).limit(1)))

    # ---------- bookings ----------
    def add_booking(self, booking: Booking) -> None:
        """Insert. The check runs in the same transaction; the partial unique index is the backstop."""
        clash = bookings.c.id == booking.id
        if booking.status == "booked":
            clash = sa.or_(
                clash,
                sa.and_(
                    bookings.c.user_id == booking.user_id,
                    bookings.c.provider_id == booking.provider_id,
                    bookings.c.start == booking.start,
                    bookings.c.status == "booked",
                ),
            )
        with self.db.tx() as conn:
            if conn.execute(sa.select(bookings.c.id).where(clash)).first():
                raise RequestConflict("booking id or booked slot already exists")
            try:
                conn.execute(bookings.insert().values(**booking.model_dump()))
            except IntegrityError as error:
                raise RequestConflict("booking id or booked slot already exists") from error

    def save_booking(self, booking: Booking) -> None:
        values = _without_id(booking.model_dump())
        try:
            updated = self.db.exec(bookings.update().where(bookings.c.id == booking.id).values(**values))
        except IntegrityError as error:
            raise RequestConflict("the user already has a booked booking for this slot") from error
        if not updated:
            raise LookupError(booking.id)

    def get_booking(self, booking_id: str) -> Booking | None:
        return _booking(self.db.one(sa.select(bookings).where(bookings.c.id == booking_id)))

    def list_bookings_for_user(
        self,
        user_id: str,
        *,
        service: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: BookingStatus | None = None,
    ) -> list[Booking]:
        query = sa.select(bookings).where(bookings.c.user_id == user_id)
        if service is not None:
            query = query.where(sa.func.lower(bookings.c.service) == service.lower())
        if status is not None:
            query = query.where(bookings.c.status == status)
        lower, upper = ist_day_bounds(date_from, date_to)
        if lower is not None:
            query = query.where(bookings.c.start >= lower)
        if upper is not None:
            query = query.where(bookings.c.start < upper)
        return [Booking.model_validate(dict(row)) for row in self.db.rows(query.order_by(bookings.c.start))]

    def booked_for_slot(self, user_id: str, provider_id: str, start: datetime) -> Booking | None:
        return _booking(
            self.db.one(
                sa.select(bookings).where(
                    bookings.c.user_id == user_id,
                    bookings.c.provider_id == provider_id,
                    bookings.c.start == start,
                    bookings.c.status == "booked",
                )
            )
        )

    def booked_since(self, user_id: str, since: datetime) -> int:
        row = self.db.one(
            sa.select(sa.func.count().label("n"))
            .select_from(bookings)
            .where(bookings.c.user_id == user_id, bookings.c.created_at >= since)
        )
        return int(row["n"]) if row else 0

    def booked_all(self) -> list[Booking]:
        rows = self.db.rows(sa.select(bookings).where(bookings.c.status == "booked").order_by(bookings.c.start))
        return [Booking.model_validate(dict(row)) for row in rows]

    # ---------- profiles ----------
    def get_profile(self, user_id: str) -> UserProfile:
        row = self.db.one(sa.select(user_profiles).where(user_profiles.c.user_id == user_id))
        return UserProfile.model_validate(dict(row)) if row else UserProfile(user_id=user_id)

    def save_profile(self, profile: UserProfile, *, now: datetime) -> UserProfile:
        saved = profile.model_copy(update={"updated_at": now})
        values = saved.model_dump(mode="json") | {"updated_at": now}
        values.pop("user_id")
        self.db.upsert(user_profiles, {"user_id": saved.user_id}, values)
        return saved

    # ---------- job queue ----------
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
        """Insert a pending job. A quote with a pending or running job raises ``RequestConflict``.

        The check runs in the same transaction as the insert; the partial unique index
        ``jobs_one_active_per_quote`` is the backstop if two writers race.
        """
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            quote_id=quote_id,
            payload=payload,
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            run_at=run_at or now,
            locked_until=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        active = sa.select(jobs.c.id).where(jobs.c.quote_id == quote_id, jobs.c.status.in_(ACTIVE_JOB_STATUSES))
        with self.db.tx() as conn:
            if quote_id is not None and conn.execute(active).first():
                raise RequestConflict("quote already has an active job")
            try:
                conn.execute(jobs.insert().values(**job.__dict__))
            except IntegrityError as error:
                raise RequestConflict("quote already has an active job") from error
        return job

    def claim_job(self, *, now: datetime, lease: timedelta) -> Job | None:
        """Take the oldest due job. SQLite: one UPDATE ... RETURNING. Postgres: FOR UPDATE SKIP LOCKED."""
        changes: dict[str, Any] = {
            "status": "running",
            "locked_until": now + lease,
            "attempts": jobs.c.attempts + 1,
            "updated_at": now,
        }
        oldest_due = (
            sa.select(jobs.c.id)
            .where(_due_jobs_condition(now))
            .order_by(jobs.c.run_at, jobs.c.created_at, jobs.c.id)
            .limit(1)
        )
        with self.db.tx() as conn:
            if self.db.is_sqlite:
                claimed = conn.execute(
                    jobs.update().where(jobs.c.id == oldest_due.scalar_subquery()).values(**changes).returning(jobs)
                )
                row = claimed.mappings().first()
                return _job(row) if row else None
            locked = conn.execute(oldest_due.with_for_update(skip_locked=True)).scalar_one_or_none()
            if locked is None:
                return None
            row = (
                conn.execute(jobs.update().where(jobs.c.id == locked).values(**changes).returning(jobs))
                .mappings()
                .one()
            )
            return _job(row)

    def complete_job(self, job_id: str, *, now: datetime) -> None:
        self.db.exec(jobs.update().where(jobs.c.id == job_id).values(status="done", locked_until=None, updated_at=now))

    def fail_job(self, job_id: str, *, error: str, now: datetime, retry_at: datetime | None) -> None:
        changes: dict[str, Any] = {
            "status": "pending" if retry_at is not None else "dead",
            "last_error": error[:ERROR_LIMIT],
            "locked_until": None,
            "updated_at": now,
        }
        if retry_at is not None:
            changes["run_at"] = retry_at
        self.db.exec(jobs.update().where(jobs.c.id == job_id).values(**changes))

    def mark_job_failed(self, job_id: str, *, error: str, now: datetime) -> None:
        self.db.exec(
            jobs.update()
            .where(jobs.c.id == job_id)
            .values(status="failed", last_error=error[:ERROR_LIMIT], locked_until=None, updated_at=now)
        )

    def get_job(self, job_id: str) -> Job | None:
        row = self.db.one(sa.select(jobs).where(jobs.c.id == job_id))
        return _job(row) if row else None

    def jobs_for_quote(self, quote_id: str) -> list[Job]:
        rows = self.db.rows(sa.select(jobs).where(jobs.c.quote_id == quote_id).order_by(jobs.c.created_at, jobs.c.id))
        return [_job(row) for row in rows]

    # ---------- settings ----------
    def get_setting(self, key: str) -> dict[str, Any] | None:
        row = self.db.one(sa.select(app_settings.c.value).where(app_settings.c.key == key))
        return dict(row["value"]) if row else None

    def put_setting(self, key: str, value: dict[str, Any], now: datetime) -> None:
        self.db.upsert(app_settings, {"key": key}, {"value": value, "updated_at": now})

    # ---------- inspection (admin only) ----------
    def counts(self) -> dict[str, Any]:
        def count_by(table: sa.Table, column: str) -> dict[str, int]:
            rows = self.db.rows(sa.select(table.c[column], sa.func.count().label("n")).group_by(table.c[column]))
            return {row[column]: int(row["n"]) for row in rows}

        def total(table: sa.Table) -> int:
            row = self.db.one(sa.select(sa.func.count().label("n")).select_from(table))
            return int(row["n"]) if row else 0

        return {
            "conversations": total(conversations),
            "searches": total(searches),
            "quotes": count_by(quotes, "status"),
            "bookings": count_by(bookings, "status"),
            "jobs": count_by(jobs, "status"),
        }

    def recent_quotes(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.rows(sa.select(quotes).order_by(quotes.c.updated_at.desc(), quotes.c.id.desc()).limit(limit))
        return [_json_row(row) for row in rows]

    def recent_bookings(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.rows(
            sa.select(bookings).order_by(bookings.c.created_at.desc(), bookings.c.id.desc()).limit(limit)
        )
        return [_json_row(row) for row in rows]

    def recent_jobs(self, limit: int = 50) -> list[Job]:
        rows = self.db.rows(sa.select(jobs).order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit))
        return [_job(row) for row in rows]
