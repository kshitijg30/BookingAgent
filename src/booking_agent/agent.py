"""The chat agent: one user message in, one reply out.

``Agent.handle`` builds a ``TurnRequest`` and hands it to a ``TurnRunner`` (turn_runner.py), which owns locking,
recovery, error handling and saving. The agent supplies the operation that answers the message:

1. A slot tapped on an offers card (``pick_slot``) is held in code (flow/quotes.py).
2. A confirm or decline tapped on a quote card (``card_action``) is applied in code (flow/card_actions.py).
3. Any typed message goes to the model (flow/conversation.py), which calls tools (flow/tools/). Typed text never
   confirms anything.

The agent also runs the scan for provider cancellations.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from booking_agent.catalog import DEFAULT_CATALOG, ServiceCatalog
from booking_agent.clock import Clock, KeyedLocks
from booking_agent.flow.context import CardAction, Deps, TurnContext, TurnRequest, TurnResult, apply_card_action
from booking_agent.flow.conversation import converse
from booking_agent.flow.quotes import PickResult, pick_slot
from booking_agent.flow.search import find_cancellations, notify_conversation
from booking_agent.flow.tools.booking import pick_result
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.limits import Limits
from booking_agent.model import ChatModel
from booking_agent.ports import Repository as Store
from booking_agent.prompts import Prompts, load_prompts
from booking_agent.provider.client import ProviderAPI, ProviderClient
from booking_agent.render import NOTICE_CARD, blocked_text
from booking_agent.tracing import Telemetry, Tracer
from booking_agent.turn_runner import TurnRunner, build_deps

__all__ = ["Agent", "TurnResult"]


class Agent:
    """The chat entry point used by the HTTP API and the CLI."""

    def __init__(
        self,
        *,
        store: Store,
        provider: ProviderAPI,
        model: ChatModel | None = None,
        clock: Clock,
        limits: Limits | None = None,
        trace_dir: Path | str | None = None,
        new_id: Callable[[], str] | None = None,
        telemetry: Telemetry | None = None,
        prompts: Prompts | None = None,
        catalog: ServiceCatalog = DEFAULT_CATALOG,
        memory_model: ChatModel | None = None,
    ) -> None:
        self.store = store
        self.provider_client = ProviderClient(provider)
        self.model = model
        self.clock = clock
        self.trace_dir = trace_dir
        self.telemetry = telemetry or Telemetry(trace_dir=trace_dir)
        self.default_prompts = prompts or load_prompts()
        self._scan_lock = threading.Lock()
        # (conversation_id, trace_id, exit_kind): called after the scan saved a notice, so open chats can refresh
        self.on_conversation_updated: Callable[[str, str | None, str], None] | None = None
        deps = build_deps(
            store=store,
            provider_client=self.provider_client,
            clock=clock,
            limits=limits or Limits(),
            telemetry=self.telemetry,
            prompts=self.default_prompts,
            catalog=catalog,
            new_id=new_id,
            memory_model=memory_model or model,
        )
        self.runner = TurnRunner(deps)

    @property
    def deps(self) -> Deps:
        """The services and settings the next turn will use."""
        return self.runner.deps

    @property
    def prompts(self) -> Prompts:
        return self.runner.deps.prompts

    @property
    def limits(self) -> Limits:
        return self.runner.deps.limits

    @property
    def conversation_locks(self) -> KeyedLocks:
        return self.runner.deps.conversation_locks

    @property
    def user_locks(self) -> KeyedLocks:
        return self.runner.deps.user_locks

    def apply_settings(self, *, prompts: Prompts, limits: Limits) -> None:
        """Switch to new prompts and limits (from the admin page). Turns already running keep the old ones."""
        self.runner.apply_settings(prompts=prompts, limits=limits)

    def handle(
        self,
        *,
        conversation_id: str,
        user_id: str,
        text: str = "",
        pick_slot: tuple[str, datetime] | None = None,
        card_action: CardAction | None = None,
    ) -> TurnResult:
        """One user turn.

        ``pick_slot`` is a slot tapped on an offers card: (provider_id, slot_start). ``card_action`` is a confirm or
        decline tapped on a quote card. Without either, ``text`` goes to the model.
        Raises AccessDenied for another user's conversation and EnqueueFailed when a confirm could not be queued.
        """
        request = TurnRequest(
            conversation_id=conversation_id,
            user_id=user_id,
            text=text,
            pick_slot=pick_slot,
            card_action=card_action,
        )
        return self.runner.run(request, self._respond)

    def _respond(self, deps: Deps, turn: TurnContext, request: TurnRequest) -> None:
        """UI actions are handled in code; typed text goes to the model."""
        if request.pick_slot is not None:
            turn.answered_by = "ui"
            self._pick(deps, turn, request.pick_slot)
        elif request.card_action is not None:
            turn.answered_by = "code"
            apply_card_action(deps, turn, request.card_action)
        else:
            self._ask_model(deps, turn, request.text)

    @staticmethod
    def _pick(deps: Deps, turn: TurnContext, picked: tuple[str, datetime]) -> None:
        result: PickResult = pick_slot(deps, turn, picked[0], picked[1])
        if result.status == "not_offered":
            turn.say("That isn't one I can offer. Please pick a slot from the current options.", NOTICE_CARD)
        elif result.status in ("busy", "over_budget", "provider_error"):
            turn.say(
                blocked_text("no_slots_in_window")
                if result.status == "over_budget"
                else str(pick_result(result)["details"][0]),
                NOTICE_CARD,
            )

    def _ask_model(self, deps: Deps, turn: TurnContext, text: str) -> None:
        if self.model is None:
            raise ValueError("A chat model is required for conversation; use BookingEngine for typed operations")
        turn.answered_by = self.model.model_name
        reply = converse(deps, self.model, turn, text)
        if reply:
            turn.reply = reply

    def scan_provider_cancellations(self) -> int:
        """Background check over all users with booked bookings. Adds a message and a replacement search."""
        found = 0
        deps = self.deps
        with self._scan_lock:
            for user_id in sorted({booking.user_id for booking in self.store.booked_all()}):
                with deps.user_locks.get(user_id):
                    tracer = Tracer(self.telemetry)
                    now = self.clock.now()
                    tracer.start(session_id="watcher", user_id=user_id, now=now, name="provider_cancellation_scan")
                    calls = ProviderCalls(self.provider_client, tracer, self.clock, None)
                    for item in find_cancellations(deps, calls, user_id, now):
                        notify_conversation(deps, item, now)
                        found += 1
                        if self.on_conversation_updated is not None:
                            self.on_conversation_updated(item.booking.conversation_id, None, "provider_cancelled")
                    tracer.finish()
        return found
