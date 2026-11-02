"""The turn pipeline shared by the chat agent, BookingEngine and the job worker.

``TurnRunner.run`` takes a ``TurnRequest`` and an ``Operation`` and runs one turn:

1. Hold the user lock, then the conversation lock.
2. Load the conversation (created on the first message). Another user's conversation raises ``AccessDenied``.
3. Apply the per-user rate limit (user turns only).
4. Start a trace. Load the newest search and the current quote.
5. Recovery: provider cancellations for this user (user turns only), the turn limit and the input limit.
6. Run the operation. It adds messages with ``turn.say`` and buffers database changes and jobs on the turn.
7. Save the conversation (version check), the buffered changes and the queued jobs in ONE transaction.
   If the turn confirmed a quote and that save fails, nothing is saved and ``EnqueueFailed`` is raised.

A worker turn (``TurnRequest.source == "worker"``) adds agent messages only and skips the rate limiter.
If the operation raises, its buffered changes are dropped and the user gets an error reply.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Callable
from datetime import datetime

from booking_agent.catalog import ServiceCatalog
from booking_agent.clock import Clock
from booking_agent.domain.entities import ChatMessage, Conversation
from booking_agent.domain.exits import ExitReason, exit_message
from booking_agent.errors import AccessDenied, EnqueueFailed
from booking_agent.flow.context import Deps, TurnContext, TurnRequest, TurnResult
from booking_agent.flow.recovery import recover
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.jobs.outbox import flush_outbox
from booking_agent.limits import LimitHit, Limits, RateLimiter, TurnBudget
from booking_agent.locks import KeyedLocks
from booking_agent.model import ChatModel
from booking_agent.ports import Repository
from booking_agent.prompts import Prompts
from booking_agent.provider.client import ProviderClient
from booking_agent.render import NOTICE_CARD
from booking_agent.tracing import Telemetry, Tracer

log = logging.getLogger("booking_agent")

MAX_INPUT_CHARS = 2000
EMPTY_REPLY = "What would you like to book, reschedule or cancel?"

Operation = Callable[[Deps, TurnContext, TurnRequest], None]
"""The work done inside a turn. It adds messages with ``turn.say`` and may record an exit with ``turn.end``."""


def build_deps(
    *,
    store: Repository,
    provider_client: ProviderClient,
    clock: Clock,
    limits: Limits,
    telemetry: Telemetry,
    prompts: Prompts,
    catalog: ServiceCatalog,
    new_id: Callable[[], str] | None = None,
    memory_model: ChatModel | None = None,
) -> Deps:
    """Deps whose conversation and user locks are coordinated through the store."""
    return Deps(
        store=store,
        provider_client=provider_client,
        clock=clock,
        limits=limits,
        new_id=new_id or (lambda: uuid.uuid4().hex[:10]),
        conversation_locks=KeyedLocks(lambda key: store.coordination_lock(f"conversation:{key}")),
        user_locks=KeyedLocks(lambda key: store.coordination_lock(f"user:{key}")),
        telemetry=telemetry,
        prompts=prompts,
        catalog=catalog,
        memory_model=memory_model,
    )


class TurnRunner:
    """Runs turns for any operation. Holds the current settings and the rate limiter."""

    def __init__(self, deps: Deps, on_jobs_queued: Callable[[], None] | None = None) -> None:
        self._deps = deps
        self.on_jobs_queued = on_jobs_queued  # called after a commit that queued jobs, so the worker need not poll
        self._rate = RateLimiter(deps.clock, deps.limits.messages_per_minute)

    @property
    def deps(self) -> Deps:
        return self._deps

    def apply_settings(self, *, prompts: Prompts, limits: Limits) -> None:
        """Switch to new prompts and limits. Turns already running keep the old ones."""
        self._rate = RateLimiter(self._deps.clock, limits.messages_per_minute)
        self._deps = dataclasses.replace(self._deps, prompts=prompts, limits=limits)

    def run(self, request: TurnRequest, operation: Operation) -> TurnResult:
        """Run one turn. Raises AccessDenied for another user's conversation and EnqueueFailed for a failed confirm."""
        deps = self._deps
        with deps.user_locks.get(request.user_id), deps.conversation_locks.get(request.conversation_id):
            stored = deps.store.get_conversation(request.conversation_id)
            if stored is not None and stored.user_id != request.user_id:
                raise AccessDenied("conversation not found")
            if request.source == "user" and not self._rate.allow(request.user_id):
                reason = ExitReason(kind="rate_limited")
                return TurnResult(exit_message(reason) or "", reason, None)
            return self._run_locked(deps, request, stored, operation)

    def _run_locked(
        self, deps: Deps, request: TurnRequest, stored: Conversation | None, operation: Operation
    ) -> TurnResult:
        now = deps.clock.now()
        tracer = Tracer(deps.telemetry)
        trace_id = tracer.start(
            session_id=request.conversation_id, user_id=request.user_id, now=now, **{"user.message": request.text[:200]}
        )
        conversation = stored or Conversation(
            id=request.conversation_id, user_id=request.user_id, created_at=now, updated_at=now
        )
        turn = _start_turn(deps, tracer, now, conversation)
        if request.source == "user":
            shown = request.text or _describe_action(request)
            tracer.record("user_message", now=now, text=shown, chars=len(shown))
        else:
            tracer.record("worker_turn", now=now, operation=request.operation_data)
        text = self._run_operation(deps, request, operation, turn)
        result = self._build_result(deps, request, turn, trace_id)
        try:
            jobs_written = _save(deps, request, turn, conversation, text, now)
        except Exception:
            tracer.finish(error=True)
            if turn.confirm_quote_id is not None:
                log.exception("enqueue_failed quote_id=%s", turn.confirm_quote_id)
                raise EnqueueFailed(turn.confirm_quote_id) from None
            raise
        for name, fields in turn.events:
            tracer.record(name, now=now, **fields)
        if jobs_written and self.on_jobs_queued is not None:
            self.on_jobs_queued()
        tracer.finish(
            error=result.exit.kind == "internal_error",
            **{"exit.kind": result.exit.kind, "agent.answered_by": turn.answered_by},
        )
        return result

    def _run_operation(self, deps: Deps, request: TurnRequest, operation: Operation, turn: TurnContext) -> str:
        """Recovery, limits and the operation. Returns the text that was read."""
        text = request.text
        try:
            if request.source == "user":
                recover(deps, turn)
                if text and _user_messages(turn.conversation) >= deps.limits.turns_per_session:
                    raise LimitHit("session_turns")
                text = _within_input_limit(text, turn)
            operation(deps, turn, dataclasses.replace(request, text=text))
            turn.finalize_messages()
        except LimitHit as hit:
            turn.clear_outcome()
            turn.end(ExitReason(kind="limit", limit=hit.kind))
            turn.tracer.record("limit_hit", now=deps.clock.now(), limit=hit.kind)
        except Exception as error:
            log.exception("turn failed")
            turn.tracer.record("internal_error", now=deps.clock.now(), error=type(error).__name__)
            turn.clear_outcome()
            turn.end(ExitReason(kind="internal_error", detail=type(error).__name__))
        return text

    def _build_result(self, deps: Deps, request: TurnRequest, turn: TurnContext, trace_id: str) -> TurnResult:
        exit_reason = turn.exit or ExitReason(kind="completed")
        if (message := exit_message(exit_reason)) is not None:
            turn.say(message, NOTICE_CARD)
        if not turn.messages and request.source == "user":
            turn.say(EMPTY_REPLY, NOTICE_CARD)
        turn.tracer.record(
            "run_finished",
            now=deps.clock.now(),
            exit=exit_reason.model_dump(),
            tool_calls=turn.turn_limits.tool_calls,
            model_calls=turn.turn_limits.model_calls,
            elapsed_s=round(turn.turn_limits.elapsed(), 3),
            cards=[m.card.get("type") for m in turn.messages if m.card],
        )
        if request.source == "user" and request.text and deps.memory_model is not None:
            turn.queue_job(
                "memory_update", None, {"user_id": request.user_id, "conversation_id": request.conversation_id}
            )
        text = "\n\n".join(m.text for m in turn.messages if m.text)
        return TurnResult(text, exit_reason, trace_id, turn.answered_by, list(turn.messages))


def _start_turn(deps: Deps, tracer: Tracer, now: datetime, conversation: Conversation) -> TurnContext:
    budget = TurnBudget(deps.clock, deps.limits)
    calls = ProviderCalls(deps.provider_client, tracer, deps.clock, budget)
    return TurnContext(
        calls=calls,
        turn_limits=budget,
        tracer=tracer,
        now=now,
        conversation=conversation,
        search=deps.store.newest_search_for_conversation(conversation.id),
        quote=deps.store.current_quote_for_conversation(conversation.id),
    )


def _describe_action(request: TurnRequest) -> str:
    """A UI action (slot tap, confirm, decline) written as text for the trace."""
    if request.pick_slot is not None:
        provider_id, start = request.pick_slot
        return f"[pick_slot {provider_id} {start.isoformat()}]"
    action = request.card_action
    if action is not None:
        version = f" v{action.version}" if action.version is not None else ""
        return f"[{action.kind} {action.quote_id}{version}]"
    command = (request.operation_data or {}).get("action")
    return f"[{command}]" if command else ""


def _user_messages(conversation: Conversation) -> int:
    return sum(1 for message in conversation.messages if message.role == "user")


def _within_input_limit(text: str, turn: TurnContext) -> str:
    if len(text) <= MAX_INPUT_CHARS:
        return text
    turn.say(f"Your message was long, so I only read the first {MAX_INPUT_CHARS} characters.", NOTICE_CARD)
    return text[:MAX_INPUT_CHARS]


def _save(
    deps: Deps, request: TurnRequest, turn: TurnContext, conversation: Conversation, text: str, now: datetime
) -> bool:
    """Conversation, buffered changes and queued jobs in one transaction. Returns True if a job was written."""
    added: list[ChatMessage] = []
    if request.source == "user" and text:
        added.append(ChatMessage(role="user", text=text, at=now))
    updated = conversation.model_copy(
        update={
            "messages": (*conversation.messages, *added, *turn.messages),
            "title": conversation.title or (text[:80] if text else None),
            "updated_at": now,
        }
    )
    with deps.store.tx():
        deps.store.save_conversation(updated, expected_version=conversation.version)
        for change in turn.writes:
            change(deps.store)
        jobs = flush_outbox(deps.store, turn.outbox, now)
        for job in jobs:
            turn.events.append(("job_status", {"job_id": job.id, "kind": job.kind, "from": "none", "to": job.status}))
    return any(job.kind != "memory_update" for job in jobs)
