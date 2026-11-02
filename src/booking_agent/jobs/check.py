"""The check job: after a write job died, keep asking the provider with the ORIGINAL key until it answers."""

from __future__ import annotations

from booking_agent.gateway.verify import check_book_status
from booking_agent.jobs.change import provider_booking_id, verify_cancelled, verify_moved
from booking_agent.jobs.common import JobContext, Verdict
from booking_agent.jobs.write import WriteContext, run_write
from booking_agent.rows import Job


def handle_check(context: JobContext, job: Job) -> bool:
    return run_write(context, job, check_step)


def check_step(ctx: WriteContext) -> Verdict:
    """Settle the quote the way the dead write would have."""
    if ctx.action == "book":
        status = check_book_status(ctx.turn.calls, ctx.key)
        if status.status == "booked":
            return ctx.settle_booked(status.booking_id or "")
        if status.status == "not_booked":
            return ctx.reissue()
        if status.status == "failed":
            return ctx.fail_slot()
        return "unknown"
    provider_id = provider_booking_id(ctx)
    if provider_id is None:
        return ctx.fail_quote("I couldn't find that booking any more. Nothing was changed.")
    if ctx.action == "cancel":
        return verify_cancelled(ctx, provider_id, call_failed=True)
    return verify_moved(ctx, provider_id, call_failed=True)
