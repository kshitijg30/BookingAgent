"""The cancel and reschedule jobs: call the provider, then read the booking back to verify."""

from __future__ import annotations

from booking_agent.gateway.verify import cancel_with_retry, read_back
from booking_agent.jobs.common import JobContext, Verdict
from booking_agent.jobs.write import WriteContext, run_write
from booking_agent.rows import Job


def handle_cancel(context: JobContext, job: Job) -> bool:
    return run_write(context, job, cancel_step)


def handle_reschedule(context: JobContext, job: Job) -> bool:
    return run_write(context, job, reschedule_step)


def provider_booking_id(ctx: WriteContext) -> str | None:
    booking = ctx.deps.store.get_booking(ctx.quote.booking_id or "")
    return booking.provider_booking_id if booking is not None else None


def verify_cancelled(ctx: WriteContext, provider_id: str, *, call_failed: bool) -> Verdict:
    """Read the booking back. Only a booking the provider shows as cancelled counts as done."""
    back = read_back(ctx.turn.calls, ctx.quote.user_id, provider_id)
    if not back.reachable:
        return "unknown"
    if back.booking is not None and back.booking.status == "cancelled":
        return ctx.settle_cancelled()
    if call_failed and ctx.deps.provider_client.capabilities_for().read_back_authoritative:
        return ctx.reissue()
    return "unknown"


def verify_moved(ctx: WriteContext, provider_id: str, *, call_failed: bool) -> Verdict:
    """Read the booking back. Only a booking at the new time counts as done."""
    back = read_back(ctx.turn.calls, ctx.quote.user_id, provider_id)
    if not back.reachable or back.booking is None:
        return "unknown"
    if back.booking.slot_start == ctx.quote.start:
        return ctx.settle_moved()
    if call_failed and ctx.deps.provider_client.capabilities_for().read_back_authoritative:
        return ctx.reissue()
    return "unknown"


def cancel_step(ctx: WriteContext) -> Verdict:
    provider_id = provider_booking_id(ctx)
    if provider_id is None:
        return ctx.fail_quote("I couldn't find that booking any more. Nothing was changed.")
    call = cancel_with_retry(ctx.turn.calls, provider_id, ctx.key)
    if call.kind == "err" and call.error != "transient_503":
        return ctx.fail_quote("The provider couldn't cancel that booking. Nothing was changed.")
    return verify_cancelled(ctx, provider_id, call_failed=call.kind == "err")


def reschedule_step(ctx: WriteContext) -> Verdict:
    provider_id = provider_booking_id(ctx)
    if provider_id is None:
        return ctx.fail_quote("I couldn't find that booking any more. Nothing was changed.")
    calls, start = ctx.turn.calls, ctx.quote.start
    call = calls.run(
        "reschedule",
        {"booking_id": provider_id, "new_slot": start, "idem_key": ctx.key},
        lambda: calls.provider_client.reschedule(provider_id, start, ctx.key),
        verifying=True,
    )
    if call.kind == "err":
        if call.error == "slot_conflict":
            return ctx.fail_slot()
        return ctx.fail_quote("The provider couldn't move that booking. Nothing was changed.")
    return verify_moved(ctx, provider_id, call_failed=False)
