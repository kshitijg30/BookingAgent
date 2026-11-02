"""What every flow function receives, and what a turn returns.

- ``Deps``: long-lived services (store, clock, limits, ...). Built once by the Agent.
- ``TurnContext``: scratch space for one turn. Database changes are buffered in ``writes`` and ``outbox``
  and committed together with the conversation by the turn runner (turn_runner.py).
- ``TurnRequest``: the caller's inputs for one turn, and whether a user or the background worker started it.
- ``TurnResult``: what the Agent returns for the turn.

Also the buttons on a quote card, confirm and decline (``apply_card_action``). Decided in code, never by the model.
Confirm needs the current quote, status ``shown`` and the same version. One transaction (the turn save) sets the
quote ``confirmed`` and inserts the job. Typed text never confirms anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from booking_agent.clock import Clock, KeyedLocks
from booking_agent.config import ServiceCatalog
from booking_agent.domain.entities import ChatMessage, Conversation, JobRequest, Quote, Search
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.planning import confirm_refusal
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.limits import Limits, TurnBudget
from booking_agent.model import ChatModel
from booking_agent.ports import Repository
from booking_agent.prompts import Prompts
from booking_agent.provider.client import ProviderClient
from booking_agent.render import NOTICE_CARD
from booking_agent.tracing import Telemetry, Tracer

Write = Callable[[Repository], object]


@dataclass(frozen=True)
class Deps:
    """Services the flows use, passed explicitly instead of through ``self``."""

    store: Repository
    provider_client: ProviderClient
    clock: Clock
    limits: Limits
    new_id: Callable[[], str]
    conversation_locks: KeyedLocks
    user_locks: KeyedLocks
    telemetry: Telemetry
    prompts: Prompts
    catalog: ServiceCatalog
    memory_model: ChatModel | None = None


@dataclass(frozen=True)
class CardAction:
    """A button tapped on a quote card. Confirming needs the card's version; declining does not."""

    kind: Literal["confirm", "decline"]
    quote_id: str
    version: int | None = None


@dataclass(frozen=True)
class TurnRequest:
    """What the caller asked for in one turn. It does not change while the turn runs."""

    conversation_id: str
    user_id: str
    text: str = ""
    pick_slot: tuple[str, datetime] | None = None  # (provider_id, slot_start) tapped on an offers card
    card_action: CardAction | None = None  # confirm or decline tapped on a quote card
    operation_data: dict[str, object] | None = None  # a typed command's or job's arguments, for the trace
    source: Literal["user", "worker"] = "user"  # a worker turn skips the rate limit and adds no user message


@dataclass
class TurnContext:
    """Scratch space for one turn.

    ``say`` adds an agent message right away. ``reply``, ``attach`` and ``quote_message`` are put in order when
    the turn ends: the model's reply (with the attached card), then the quote card message.
    """

    calls: ProviderCalls
    turn_limits: TurnBudget
    tracer: Tracer
    now: datetime
    conversation: Conversation
    text: str = ""  # the user's newest message of this turn
    search: Search | None = None  # the newest search of the conversation
    quote: Quote | None = None  # the current quote: newest with status shown or confirmed
    messages: list[ChatMessage] = field(default_factory=list)  # agent messages added this turn
    exit: ExitReason | None = None
    answered_by: str = ""
    reply: str | None = None  # the model's reply text
    attach: tuple[str, dict[str, Any]] | None = None  # (fallback text, card) for an offers or question card
    quote_message: tuple[str, dict[str, Any]] | None = None  # (text, card) of the quote shown this turn
    writes: list[Write] = field(default_factory=list)
    outbox: list[JobRequest] = field(default_factory=list)
    confirm_quote_id: str | None = None  # set when this turn confirms a quote (its save is the enqueue)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # trace events, emitted after the commit

    @property
    def user_id(self) -> str:
        return self.conversation.user_id

    @property
    def conversation_id(self) -> str:
        return self.conversation.id

    def say(self, text: str, card: dict[str, Any] | None = None) -> None:
        """Add an agent message now."""
        self.messages.append(ChatMessage(role="agent", text=text, card=card, at=self.now))

    def end(self, exit_reason: ExitReason) -> None:
        self.exit = exit_reason

    def write(self, change: Write) -> None:
        """Buffer a database change. It runs in the transaction that saves the conversation."""
        self.writes.append(change)

    def queue_job(
        self,
        kind: str,
        quote_id: str | None,
        payload: dict[str, Any],
        *,
        run_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> None:
        """Ask for a background job. It is written in the same transaction as the conversation."""
        self.outbox.append(JobRequest(kind, quote_id, payload, run_at, max_attempts))  # type: ignore[arg-type]

    def set_search(self, search: Search) -> None:
        self.search = search
        self.write(lambda store: store.add_search(search))

    def add_quote(self, quote: Quote) -> None:
        """Store a new quote. A newer shown quote replaces the one on screen, which is declined."""
        previous = self.quote
        if previous is not None and previous.status == "shown" and quote.status == "shown":
            self.save_quote(previous.model_copy(update={"status": "declined", "updated_at": self.now}))

        def insert(store: Repository) -> None:
            store.add_quote(quote)
            self.event("quote_status", quote_id=quote.id, **{"from": "none", "to": quote.status})

        self.write(insert)
        self.quote = quote if quote.status in ("shown", "confirmed") else self.quote

    def save_quote(self, quote: Quote) -> None:
        """Replace a stored quote and keep ``self.quote`` in step."""

        def replace(store: Repository) -> None:
            before = store.get_quote(quote.id)
            store.save_quote(quote)
            if before is None or before.status != quote.status:
                old = before.status if before else "none"
                self.event("quote_status", quote_id=quote.id, **{"from": old, "to": quote.status})

        self.write(replace)
        if self.quote is not None and self.quote.id == quote.id:
            self.quote = quote if quote.status in ("shown", "confirmed") else None

    def event(self, name: str, **fields: Any) -> None:
        """Queue a trace event. It is recorded only after the turn's transaction commits."""
        self.events.append((name, fields))

    def settle_job(self, job_id: str, kind: str, *, failed_error: str | None = None) -> None:
        """End a job record in this turn's transaction: ``done``, or ``failed`` when ``failed_error`` is given."""

        def end(store: Repository) -> None:
            before = store.get_job(job_id)
            if failed_error is None:
                store.complete_job(job_id, now=self.now)
            else:
                store.mark_job_failed(job_id, error=failed_error, now=self.now)
            to = "done" if failed_error is None else "failed"
            self.event("job_status", job_id=job_id, kind=kind, **{"from": before.status if before else "", "to": to})

        self.write(end)

    def clear_outcome(self) -> None:
        """Forget what a failed turn produced, so only the error reply is saved."""
        self.messages.clear()
        self.writes.clear()
        self.events.clear()
        self.outbox.clear()
        self.reply = None
        self.attach = None
        self.quote_message = None
        self.confirm_quote_id = None

    def finalize_messages(self) -> None:
        """Put the model reply, its card and the quote message in order after the messages said so far."""
        if self.attach is not None:
            fallback, card = self.attach
            self.say(self.reply or fallback, card)
        elif self.reply:
            self.say(self.reply)
        if self.quote_message is not None:
            self.say(*self.quote_message)


@dataclass
class TurnResult:
    """What the Agent returns: the agent messages added this turn and why the turn ended."""

    text: str
    exit: ExitReason
    trace_id: str | None
    answered_by: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON form returned by the API. ``card`` is the card of the last agent message, or None."""
        card = self.messages[-1].card if self.messages else None
        return {
            "text": self.text,
            "exit": self.exit.model_dump(),
            "trace_id": self.trace_id,
            "answered_by": self.answered_by,
            "messages": [message.model_dump(mode="json") for message in self.messages],
            "card": card,
        }


STALE_CARD = "That quote has been replaced. Please confirm the current card."
ALREADY = "I already have your request and I'm waiting for the provider."
DECLINED = "Okay, I won't go ahead with that. Nothing was booked or changed."
TOO_SOON = "That slot starts too soon to book safely now. Tell me if you'd like another time."
QUEUED_NOTICE = "We've taken your request. I'll confirm here as soon as the provider responds."


def confirm_quote(deps: Deps, turn: TurnContext, quote_id: str, version: int) -> None:
    """Confirm the quote on screen if it is the one named, and queue its write."""
    refusal = confirm_refusal(turn.quote, quote_id, version, turn.now)
    quote = turn.quote
    if refusal is None and quote is not None:
        confirmed = quote.model_copy(update={"status": "confirmed", "confirmed_at": turn.now, "updated_at": turn.now})
        turn.save_quote(confirmed)
        turn.queue_job(
            quote.action,
            quote.id,
            {"quote_id": quote.id, "conversation_id": turn.conversation_id, "user_id": turn.user_id},
        )
        turn.confirm_quote_id = quote.id
        turn.tracer.record("quote_confirmed", now=turn.now, quote_id=quote.id, version=version, action=quote.action)
        turn.say(QUEUED_NOTICE, NOTICE_CARD)
        turn.end(ExitReason(kind="queued"))
        return
    turn.tracer.record("confirm_refused", now=turn.now, quote_id=quote_id, reason=refusal)
    if refusal == "too_soon" and quote is not None:
        turn.save_quote(quote.model_copy(update={"status": "expired", "updated_at": turn.now}))
        turn.say(TOO_SOON, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="too_soon"))
        return
    detail = "already_confirmed" if refusal == "already_confirmed" else "stale_proposal"
    turn.say(ALREADY if detail == "already_confirmed" else STALE_CARD, NOTICE_CARD)
    turn.end(ExitReason(kind="needs_input", detail=detail))


def decline_quote(turn: TurnContext, quote_id: str) -> None:
    """Drop the quote on screen if it is the one named and still only shown."""
    quote = turn.quote
    if quote is None or quote.id != quote_id or quote.status != "shown":
        turn.say(STALE_CARD, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="stale_proposal"))
        return
    turn.save_quote(quote.model_copy(update={"status": "declined", "updated_at": turn.now}))
    turn.say(DECLINED, NOTICE_CARD)


def apply_card_action(deps: Deps, turn: TurnContext, action: CardAction) -> None:
    """Run a confirm or decline tapped on a card."""
    if action.kind == "decline":
        decline_quote(turn, action.quote_id)
    elif action.version is None:
        turn.say(STALE_CARD, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="stale_proposal"))
    else:
        confirm_quote(deps, turn, action.quote_id, action.version)
