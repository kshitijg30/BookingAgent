"""The job worker: claims due jobs and runs their handlers, with retries, backoff and a dead state.

``run_due`` is synchronous and deterministic (tests and the library use it). ``run_forever`` polls in a thread the
host owns; ``wake`` ends its wait early, for a host that knows a job was just enqueued.

A failed attempt retries after ``min(5 * 2**(attempts - 1), 300)`` seconds. A job that fails with no attempts left is
marked dead and its ``on_dead`` hook runs. A running job whose lease expired is claimable again, and nothing marks
it dead automatically, so the worker treats ``attempts > max_attempts`` as dead without running the handler.

Also what every job handler shares: ``JobContext``, ``run_job_turn`` (a worker turn) and the ``Verdict`` a step
gives. A step returns a ``Verdict``:

- ``done``: finished; the worker completes the job.
- ``settled``: the step already changed the job record in the same transaction (failed, or replaced by a new job).
- ``unknown``: the provider's answer could not be verified; the worker retries the same job with backoff.

A handler returns True when it already ended the job record in its own transaction (failed, or replaced by a new
job) and False when the worker should complete the job. It raises when the attempt failed. ``JobHandler.on_dead``
runs once when the job has no attempts left. The kind-to-handler table is ``JOB_HANDLERS`` in
``booking_agent.jobs``; a job whose kind has no entry is marked dead by the worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from booking_agent.domain.entities import Job
from booking_agent.flow.context import Deps, TurnContext, TurnRequest, TurnResult
from booking_agent.tracing import Tracer
from booking_agent.turn_runner import TurnRunner

Verdict = Literal["done", "settled", "unknown"]
FAILED_ATTEMPT_EXITS = ("internal_error", "quarantined")


class OutcomeUnknown(RuntimeError):
    """The provider's answer could not be verified. The job is retried with the same idempotency key."""


@dataclass(frozen=True)
class JobContext:
    """What a handler receives. ``session_updated(conversation_id, trace_id, exit_kind)`` tells the host."""

    deps: Deps
    runner: TurnRunner
    session_updated: Callable[[str, str | None, str], None]


def run_job_turn(
    context: JobContext, job: Job, step: Callable[[Deps, TurnContext], Verdict], payload: dict[str, Any]
) -> Verdict:
    """Run ``step`` as a worker turn for the job's conversation and return its verdict."""
    box: list[Verdict] = []

    def operation(deps: Deps, turn: TurnContext, request: TurnRequest) -> None:
        box.append(step(deps, turn))

    request = TurnRequest(
        conversation_id=payload["conversation_id"],
        user_id=payload["user_id"],
        operation_data={"job": job.id, "kind": job.kind, "attempt": job.attempts},
        source="worker",
    )
    result: TurnResult = context.runner.run(request, operation)
    if result.exit.kind in FAILED_ATTEMPT_EXITS:
        raise RuntimeError(f"worker turn ended with {result.exit.kind} ({result.exit.detail})")
    context.session_updated(payload["conversation_id"], result.trace_id, result.exit.kind)
    return box[0] if box else "unknown"


def finish(verdict: Verdict) -> bool:
    """Map a verdict to the handler's return value: True if the job record is already settled."""
    if verdict == "unknown":
        raise OutcomeUnknown("the provider's answer could not be verified yet")
    return verdict == "settled"


DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class JobHandler:
    """How one job kind runs: ``handle`` does the work, ``on_dead(context, job, error)`` runs after the last failure."""

    kind: str
    handle: Callable[[JobContext, Job], bool]
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    on_dead: Callable[[JobContext, Job, str], None] | None = None


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
