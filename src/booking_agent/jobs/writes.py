"""What the provider-write jobs (book, cancel, reschedule, check) do, and what happens when a job runs out of attempts.

``WriteContext`` holds one write attempt: the confirmed quote, the idempotency key and the turn. Its methods
buffer the changes (quote, booking, job record, messages) so they commit together with the conversation.
The dead hooks run once, after the job is marked dead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from booking_agent.domain.entities import Booking, Job, Quote, idempotency_key
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.quotes import fail_slot, show_quote
from booking_agent.gateway.verify import cancel_with_retry, check_book_status, read_back
from booking_agent.jobs.worker import JobContext, Verdict, finish, run_job_turn
from booking_agent.render import NOTICE_CARD, confirmation_card, confirmation_text, format_rupees

CHECK_MAX_ATTEMPTS = 50  # a check job keeps asking the provider for a long time
MAX_REISSUES = 3  # new jobs for one quote after verified "not applied" answers


@dataclass
class WriteContext:
    deps: Deps
    turn: TurnContext
    job: Job
    quote: Quote
    key: str
    action: str  # book, cancel or reschedule (a check job carries the original action)

    @property
    def payload(self) -> dict[str, str]:
        return {"quote_id": self.quote.id, "conversation_id": self.turn.conversation_id, "user_id": self.turn.user_id}

    def fail_quote(self, text: str) -> Verdict:
        """The write cannot be done (for a reason that is not a lost slot): quote failed, tell the user."""
        self.turn.save_quote(self.quote.model_copy(update={"status": "failed", "updated_at": self.turn.now}))
        self.turn.say(text, NOTICE_CARD)
        return "done"

    def fail_slot(self) -> Verdict:
        """The slot is gone: quote failed, message, and a research job for other slots."""
        fail_slot(self.deps, self.turn, self.quote, is_new=False)
        return "done"

    def reissue(self) -> Verdict:
        """The provider verifiably did not apply the write: end this job and queue a new one (new key)."""
        failed = sum(1 for other in self.deps.store.jobs_for_quote(self.quote.id) if other.status == "failed")
        if failed + 1 >= MAX_REISSUES:
            return self.fail_quote("The provider couldn't complete that after several tries. Nothing was changed.")
        job_id = self.job.id
        if self.job.kind == "check":
            self.turn.settle_job(job_id, self.job.kind)
        else:
            self.turn.settle_job(job_id, self.job.kind, failed_error="not applied")
        self.turn.queue_job(self.action, self.quote.id, self.payload)
        return "settled"

    def settle_booked(self, provider_booking_id: str) -> Verdict:
        """The provider verified the booking: add our row, quote done, confirmation message."""
        booking = Booking(
            id=f"bk-{self.deps.new_id()}",
            user_id=self.quote.user_id,
            conversation_id=self.quote.conversation_id,
            quote_id=self.quote.id,
            provider_id=self.quote.provider_id,
            provider_name=self.quote.provider_name,
            service=self.quote.service,
            area=self.quote.area,
            start=self.quote.start,
            end=self.quote.end,
            price=self.quote.price,
            provider_booking_id=provider_booking_id,
            created_at=self.turn.now,
            updated_at=self.turn.now,
        )
        self.turn.write(lambda store: store.add_booking(booking))
        self.turn.event(
            "booking_created",
            booking_id=booking.id,
            quote_id=self.quote.id,
            provider_booking_id=provider_booking_id,
        )
        return self._done(confirmation_text(self.quote, provider_booking_id), booking.id)

    def settle_cancelled(self) -> Verdict:
        booking = self._booking()
        if booking is not None:
            cancelled = booking.model_copy(update={"status": "cancelled", "updated_at": self.turn.now})
            self.turn.write(lambda store: store.save_booking(cancelled))
            self.turn.event("booking_updated", booking_id=booking.id, **{"from": booking.status, "to": "cancelled"})
        return self._done(
            confirmation_text(self.quote, booking.provider_booking_id or "" if booking else ""),
            self.quote.booking_id or "",
        )

    def settle_moved(self) -> Verdict:
        booking = self._booking()
        if booking is not None:
            moved = booking.model_copy(
                update={"start": self.quote.start, "end": self.quote.end, "updated_at": self.turn.now}
            )
            self.turn.write(lambda store: store.save_booking(moved))
            self.turn.event(
                "booking_updated",
                booking_id=booking.id,
                **{"from": booking.start.isoformat(), "to": self.quote.start.isoformat()},
            )
        return self._done(
            confirmation_text(self.quote, booking.provider_booking_id or "" if booking else ""),
            self.quote.booking_id or "",
        )

    def _booking(self) -> Booking | None:
        return self.deps.store.get_booking(self.quote.booking_id) if self.quote.booking_id else None

    def _done(self, text: str, booking_id: str) -> Verdict:
        self.turn.save_quote(self.quote.model_copy(update={"status": "done", "updated_at": self.turn.now}))
        self.turn.say(text, confirmation_card(booking_id))
        return "done"


Step = Callable[[WriteContext], Verdict]


def run_write(context: JobContext, job: Job, step: Step) -> bool:
    """Run one attempt of a write job as a worker turn. Returns True if the step already settled the job record."""

    def run(deps: Deps, turn: TurnContext) -> Verdict:
        quote = deps.store.get_quote(job.payload["quote_id"])
        if quote is None or quote.status != "confirmed":
            return "done"  # nothing left to do: another attempt already settled it
        key = job.payload.get("key") or deps.provider_client.route_key(
            quote.provider_id, idempotency_key(job.kind, job.id)
        )
        action = job.payload.get("action", job.kind)
        turn.tracer.record(
            "write_attempt", now=turn.now, job_kind=job.kind, job_id=job.id, attempt=job.attempts, key=key
        )
        return step(WriteContext(deps, turn, job, quote, key, action))

    return finish(run_job_turn(context, job, run, job.payload))


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
