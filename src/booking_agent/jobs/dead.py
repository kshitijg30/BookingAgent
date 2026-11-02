"""What happens when a job runs out of attempts. Each hook runs once, after the job is marked dead."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from booking_agent.domain.approval import idempotency_key
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.jobs.common import JobContext, Verdict, run_job_turn
from booking_agent.jobs.outbox import CHECK_MAX_ATTEMPTS
from booking_agent.render import NOTICE_CARD
from booking_agent.rows import Job

log = logging.getLogger("booking_agent.jobs")

CHECK_DELAY = timedelta(seconds=30)
WRITE_DEAD_NOTICE = (
    "I couldn't confirm the result of your request with the provider yet. I'll keep checking and tell you here. "
    "Please don't try it again in the meantime."
)
CHECK_DEAD_NOTICE = "I still can't confirm your earlier request with the provider. Please check your bookings."
RESEARCH_DEAD_NOTICE = "I couldn't search for other slots just now. Please try again."


def _log_dead(job: Job, error: str) -> None:
    log.error("job_dead job_id=%s kind=%s quote_id=%s error=%s", job.id, job.kind, job.quote_id, error)


def write_dead(context: JobContext, job: Job, error: str) -> None:
    """A write job is dead. The quote stays confirmed; a check job asks with the ORIGINAL key until answered."""
    _log_dead(job, error)

    def step(deps: Deps, turn: TurnContext) -> Verdict:
        quote = deps.store.get_quote(job.payload["quote_id"])
        if quote is None or quote.status != "confirmed":
            return "done"
        key = deps.provider_client.route_key(quote.provider_id, idempotency_key(job.kind, job.id))
        turn.say(WRITE_DEAD_NOTICE, NOTICE_CARD)
        turn.queue_job(
            "check",
            quote.id,
            {**job.payload, "key": key, "action": job.kind},
            run_at=turn.now + CHECK_DELAY,
            max_attempts=CHECK_MAX_ATTEMPTS,
        )
        return "done"

    run_job_turn(context, job, step, job.payload)


def _notice_dead(text: str) -> Callable[[JobContext, Job, str], None]:
    def hook(context: JobContext, job: Job, error: str) -> None:
        _log_dead(job, error)

        def step(deps: Deps, turn: TurnContext) -> Verdict:
            turn.say(text, NOTICE_CARD)
            return "done"

        run_job_turn(context, job, step, job.payload)

    return hook


check_dead = _notice_dead(CHECK_DEAD_NOTICE)
research_dead = _notice_dead(RESEARCH_DEAD_NOTICE)


def memory_dead(context: JobContext, job: Job, error: str) -> None:
    _log_dead(job, error)
