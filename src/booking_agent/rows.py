"""Typed rows that repositories return to the core logic and that are not pydantic entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from booking_agent.domain.entities import JobKind, JobStatus


@dataclass(frozen=True)
class Job:
    """One unit of background work, stored in the same database as the state it changes.

    ``pending`` waits for ``run_at``. A worker claims it and it becomes ``running`` with a lease until
    ``locked_until``. It ends ``done``, or goes back to ``pending`` with a later ``run_at`` after a
    failed attempt, or becomes ``dead`` once retries are used up. ``failed`` means the provider
    verifiably did not apply the write; a new job is created if it should be tried again. A ``running``
    job whose lease has expired can be claimed again.

    ``quote_id`` is None only for ``memory_update``. ``payload`` holds ids only. The provider
    idempotency key is ``f"{kind}:{id}"``.
    """

    id: str
    kind: JobKind
    quote_id: str | None
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    max_attempts: int
    run_at: datetime
    locked_until: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
