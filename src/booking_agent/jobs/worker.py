"""The job worker: claims due jobs and runs their handlers, with retries, backoff and a dead state.

``run_due`` is synchronous and deterministic (tests and the library use it). ``run_forever`` polls in a thread the
host owns; ``wake`` ends its wait early, for a host that knows a job was just enqueued.

A failed attempt retries after ``min(5 * 2**(attempts - 1), 300)`` seconds. A job that fails with no attempts left is
marked dead and its ``on_dead`` hook runs. A running job whose lease expired is claimable again, and nothing marks
it dead automatically, so the worker treats ``attempts > max_attempts`` as dead without running the handler.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import timedelta

from booking_agent.flow.context import Deps
from booking_agent.jobs.common import JobContext
from booking_agent.jobs.registry import JobHandler
from booking_agent.rows import Job
from booking_agent.tracing import Tracer
from booking_agent.turn_runner import TurnRunner

log = logging.getLogger("booking_agent.jobs")

BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 300
UNKNOWN_KIND = "unknown job kind"
LEASE_EXPIRED = "lease expired after the last attempt"


def retry_delay(attempts: int) -> timedelta:
    """Wait before the next attempt, doubling from 5 seconds up to 5 minutes."""
    return timedelta(seconds=min(BACKOFF_BASE_S * 2 ** (attempts - 1), BACKOFF_CAP_S))


class JobWorker:
    """Runs jobs from the store's queue. Safe to run in several threads or processes: a claim is atomic."""

    def __init__(
        self,
        deps: Deps,
        runner: TurnRunner,
        handlers: Mapping[str, JobHandler],
        *,
        lease: timedelta = timedelta(seconds=60),
        on_session_updated: Callable[[str, str | None, str], None] | None = None,
    ) -> None:
        self._deps = deps
        self._handlers = handlers
        self._lease = lease
        self._on_session_updated = on_session_updated
        self._context = JobContext(deps, runner, self._session_updated)
        self._wake = threading.Event()

    def wake(self) -> None:
        """End the wait in ``run_forever`` now, for example after enqueueing a job in this process."""
        self._wake.set()

    def run_due(self, max_jobs: int | None = None) -> int:
        """Claim and process jobs until none is due or ``max_jobs`` were processed. Returns how many ran."""
        processed = 0
        while max_jobs is None or processed < max_jobs:
            job = self._deps.store.claim_job(now=self._deps.clock.now(), lease=self._lease)
            if job is None:
                break
            self._process(job)
            processed += 1
        return processed

    def run_forever(self, stop: threading.Event, poll_interval_s: float = 0.5) -> None:
        """Process due jobs, then wait up to ``poll_interval_s`` (or until ``wake``), until ``stop`` is set."""
        while not stop.is_set():
            try:
                processed = self.run_due()
            except Exception:
                log.exception("job worker loop failed")
                processed = 0
            if processed == 0:
                self._wake.wait(poll_interval_s)
            self._wake.clear()

    def _session_updated(self, session_id: str, trace_id: str | None, exit_kind: str) -> None:
        if self._on_session_updated is None:
            return
        try:
            self._on_session_updated(session_id, trace_id, exit_kind)
        except Exception:
            log.exception("on_session_updated failed for session %s", session_id)

    def _process(self, job: Job) -> None:
        store = self._deps.store
        tracer = Tracer(self._deps.telemetry)
        now = self._deps.clock.now()
        tracer.start(
            session_id=str(job.payload.get("conversation_id", "")),
            user_id=str(job.payload.get("user_id", "")),
            now=now,
            name="job",
            **{"job.kind": job.kind, "job.id": job.id, "job.attempt": job.attempts},
        )
        tracer.record("job_status", now=now, job_id=job.id, kind=job.kind, **{"from": "pending", "to": "running"})
        outcome, error = self._run_handler(job)
        if outcome == "completed":
            store.complete_job(job.id, now=self._deps.clock.now())
        elif outcome == "settled":
            outcome = "completed"  # the handler already ended the job record in its own transaction
        else:
            outcome = self._record_failure(job, error)
        final = store.get_job(job.id)
        tracer.record(
            "job_status",
            now=self._deps.clock.now(),
            job_id=job.id,
            kind=job.kind,
            **{"from": "running", "to": final.status if final else "unknown"},
        )
        status = final.status if final else "unknown"
        tracer.record(
            "job_attempt",
            now=self._deps.clock.now(),
            job_id=job.id,
            kind=job.kind,
            attempt=job.attempts,
            outcome="retry" if status == "pending" else status,
        )
        tracer.finish(error=outcome != "completed", **{"job.outcome": outcome, "job.error": error or ""})

    def _run_handler(self, job: Job) -> tuple[str, str | None]:
        """Run the job's handler. Returns ``("completed", None)`` or ``("failed", error text)``."""
        handler = self._handlers.get(job.kind)
        if handler is None:
            return "failed", UNKNOWN_KIND
        if job.attempts > job.max_attempts:
            return "failed", LEASE_EXPIRED
        try:
            settled = handler.handle(self._context, job)
        except Exception as error:
            log.exception("job %s (%s) failed on attempt %s", job.id, job.kind, job.attempts)
            return "failed", f"{type(error).__name__}: {error}"
        return ("settled" if settled else "completed"), None

    def _record_failure(self, job: Job, error: str | None) -> str:
        """Retry with backoff, or mark the job dead and run its hook. Returns ``"retry"`` or ``"dead"``."""
        message = error or "failed"
        now = self._deps.clock.now()
        handler = self._handlers.get(job.kind)
        exhausted = handler is None or job.attempts >= job.max_attempts
        retry_at = None if exhausted else now + retry_delay(job.attempts)
        self._deps.store.fail_job(job.id, error=message, now=now, retry_at=retry_at)
        if not exhausted:
            return "retry"
        if handler is not None and handler.on_dead is not None:
            self._run_on_dead(handler.on_dead, job, message)
        return "dead"

    def _run_on_dead(self, hook: Callable[[JobContext, Job, str], None], job: Job, error: str) -> None:
        try:
            hook(self._context, job, error)
        except Exception:
            log.exception("on_dead hook failed for job %s (%s)", job.id, job.kind)
