"""Background jobs: a small queue worker that runs provider writes, slot research and memory updates.

- ``worker``: ``JobWorker`` claims due jobs and runs them; also the context every handler shares.
- ``writes``: the book, cancel, reschedule and check handlers, their shared outcome rules, and the dead hooks.
- ``research``: find other slots after a slot failed.
- ``memory``: update remembered facts and the conversation summary.

``JOB_HANDLERS`` below maps a job kind to its handler. It lives here, not in ``worker``, because the handler modules
import the shared context from ``worker``.
"""

from __future__ import annotations

from booking_agent.jobs.memory import handle_memory_update
from booking_agent.jobs.research import handle_research
from booking_agent.jobs.worker import JobContext, JobHandler
from booking_agent.jobs.writes import (
    check_dead,
    handle_book,
    handle_cancel,
    handle_check,
    handle_reschedule,
    memory_dead,
    research_dead,
    write_dead,
)

__all__ = ["JOB_HANDLERS", "JobContext", "JobHandler"]

JOB_HANDLERS: dict[str, JobHandler] = {
    "book": JobHandler("book", handle_book, on_dead=write_dead),
    "cancel": JobHandler("cancel", handle_cancel, on_dead=write_dead),
    "reschedule": JobHandler("reschedule", handle_reschedule, on_dead=write_dead),
    "check": JobHandler("check", handle_check, on_dead=check_dead),
    "research": JobHandler("research", handle_research, on_dead=research_dead),
    "memory_update": JobHandler("memory_update", handle_memory_update, on_dead=memory_dead),
}
