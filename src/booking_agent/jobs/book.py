"""The book job: checks, re-hold if the hold is about to expire, book, then verify with get_booking_status."""

from __future__ import annotations

from datetime import datetime, timedelta

from booking_agent.domain.entities import Quote
from booking_agent.flow.quotes import show_quote
from booking_agent.gateway.verify import check_book_status
from booking_agent.jobs.common import JobContext, Verdict
from booking_agent.jobs.write import WriteContext, run_write
from booking_agent.render import format_rupees
from booking_agent.rows import Job

REHOLD_MARGIN = timedelta(seconds=30)  # re-hold when the hold expires sooner than this
ALREADY_BOOKED = "You already have this exact slot booked, so I didn't book it again."
CAP_REACHED = "You've reached today's limit on new bookings. Please try again tomorrow."


def handle_book(context: JobContext, job: Job) -> bool:
    return run_write(context, job, book_step)


def local_refusal(ctx: WriteContext) -> str | None:
    """Our own records say no: the slot is already booked or confirmed, or the user hit the daily cap."""
    store, quote = ctx.deps.store, ctx.quote
    if store.booked_for_slot(quote.user_id, quote.provider_id, quote.start) is not None:
        return ALREADY_BOOKED
    if store.confirmed_quote_for_slot(quote.user_id, quote.provider_id, quote.start, exclude_quote_id=quote.id):
        return ALREADY_BOOKED
    booked_today = store.booked_since(quote.user_id, ctx.turn.now - timedelta(days=1))
    return CAP_REACHED if booked_today >= ctx.deps.limits.bookings_per_user_per_day else None


def hold_expiring(quote: Quote, now: datetime) -> bool:
    return quote.hold_id is None or quote.hold_expires_at is None or quote.hold_expires_at - now < REHOLD_MARGIN


def book_step(ctx: WriteContext) -> Verdict:
    refusal = local_refusal(ctx)
    if refusal is not None:
        return ctx.fail_quote(refusal)
    quote = ctx.quote
    if hold_expiring(quote, ctx.turn.now):
        reheld = _rehold(ctx)
        if isinstance(reheld, str):
            return reheld
        quote = reheld
    return _book(ctx, quote)


def _rehold(ctx: WriteContext) -> Quote | Verdict:
    """Hold again. If the price or time moved, the quote gets a new version and the user must confirm again."""
    quote, turn = ctx.quote, ctx.turn
    outcome = turn.calls.hold(quote.provider_id, quote.start, user_id=quote.user_id, area=quote.area)
    if outcome.kind != "ok" or outcome.value is None:
        return ctx.fail_slot() if outcome.error == "slot_unavailable" else "unknown"
    hold = outcome.value
    renewed = quote.model_copy(
        update={
            "hold_id": hold.hold_id,
            "hold_expires_at": hold.expires_at,
            "price": hold.quoted_price,
            "start": hold.slot.start,
            "end": hold.slot.end,
            "updated_at": turn.now,
        }
    )
    if hold.quoted_price == quote.price and hold.slot.start == quote.start:
        turn.save_quote(renewed)
        return renewed
    changed = renewed.model_copy(update={"version": quote.version + 1, "status": "shown", "confirmed_at": None})
    note = (
        f"The price changed from {format_rupees(quote.price)} to {format_rupees(hold.quoted_price)} when I re-checked "
        "the slot, so I need your confirmation again."
    )
    show_quote(turn, changed, note=note, is_new=False)
    return "done"


def _book(ctx: WriteContext, quote: Quote) -> Verdict:
    calls, hold_id = ctx.turn.calls, quote.hold_id or ""
    call = calls.run(
        "book",
        {"hold_id": hold_id, "idem_key": ctx.key},
        lambda: calls.provider_client.book(hold_id, ctx.key),
        verifying=True,
    )
    if call.kind == "err" and call.error == "slot_taken":
        return ctx.fail_slot()
    if call.kind == "err" and call.error == "hold_expired":
        return ctx.reissue()
    status = check_book_status(calls, ctx.key)
    if status.status == "booked":
        return ctx.settle_booked(status.booking_id or "")
    if status.status == "not_booked":
        return ctx.reissue()
    if status.status == "failed":
        return ctx.fail_slot()
    return "unknown"
