"""What every job handler shares: running a worker turn, and the verdict a step gives.

A step returns a ``Verdict``:

- ``done``: finished; the worker completes the job.
- ``settled``: the step already changed the job record in the same transaction (failed, or replaced by a new job).
- ``unknown``: the provider's answer could not be verified; the worker retries the same job with backoff.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from booking_agent.flow.context import Deps, TurnContext, TurnRequest, TurnResult
from booking_agent.rows import Job
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
