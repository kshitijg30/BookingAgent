"""Which handler runs which job kind.

A handler returns True when it already ended the job record in its own transaction (failed, or replaced by a
new job) and False when the worker should complete the job. It raises when the attempt failed. ``on_dead`` runs
once when the job has no attempts left. A job whose kind has no entry is marked dead by the worker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from booking_agent.jobs.book import handle_book
from booking_agent.jobs.change import handle_cancel, handle_reschedule
from booking_agent.jobs.check import handle_check
from booking_agent.jobs.common import JobContext
from booking_agent.jobs.dead import check_dead, memory_dead, research_dead, write_dead
from booking_agent.jobs.memory import handle_memory_update
from booking_agent.jobs.research import handle_research
from booking_agent.rows import Job

__all__ = ["JOB_HANDLERS", "JobContext", "JobHandler"]

DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class JobHandler:
    """How one job kind runs: ``handle`` does the work, ``on_dead(context, job, error)`` runs after the last failure."""

    kind: str
    handle: Callable[[JobContext, Job], bool]
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    on_dead: Callable[[JobContext, Job, str], None] | None = None


JOB_HANDLERS: dict[str, JobHandler] = {
    "book": JobHandler("book", handle_book, on_dead=write_dead),
    "cancel": JobHandler("cancel", handle_cancel, on_dead=write_dead),
    "reschedule": JobHandler("reschedule", handle_reschedule, on_dead=write_dead),
    "check": JobHandler("check", handle_check, on_dead=check_dead),
    "research": JobHandler("research", handle_research, on_dead=research_dead),
    "memory_update": JobHandler("memory_update", handle_memory_update, on_dead=memory_dead),
}
