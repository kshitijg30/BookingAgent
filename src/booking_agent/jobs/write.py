"""What the provider-write jobs (book, cancel, reschedule, check) do with a verified outcome.

``WriteContext`` holds one write attempt: the confirmed quote, the idempotency key and the turn. Its methods
buffer the changes (quote, booking, job record, messages) so they commit together with the conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from booking_agent.domain.approval import idempotency_key
from booking_agent.domain.entities import Booking, Quote
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.quotes import fail_slot
from booking_agent.jobs.common import JobContext, Verdict, finish, run_job_turn
from booking_agent.render import NOTICE_CARD, confirmation_card, confirmation_text
from booking_agent.rows import Job

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
