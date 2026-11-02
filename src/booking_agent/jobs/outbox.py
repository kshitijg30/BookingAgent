"""The transactional outbox: jobs a turn asked for are written together with the conversation save."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from booking_agent.domain.entities import JobKind
from booking_agent.ports import Repository
from booking_agent.rows import Job

CHECK_MAX_ATTEMPTS = 50  # a check job keeps asking the provider for a long time


@dataclass(frozen=True)
class JobRequest:
    """A job a turn wants queued. ``payload`` holds ids only."""

    kind: JobKind
    quote_id: str | None
    payload: dict[str, Any]
    run_at: datetime | None = None
    max_attempts: int = 5


def flush_outbox(store: Repository, outbox: Sequence[JobRequest], now: datetime) -> list[Job]:
    """Enqueue each requested job. Call it inside the same ``store.tx()`` as the conversation save."""
    jobs = [
        store.enqueue_job(
            kind=request.kind,
            quote_id=request.quote_id,
            payload=request.payload,
            now=now,
            run_at=request.run_at,
            max_attempts=request.max_attempts,
        )
        for request in outbox
    ]
    return jobs
