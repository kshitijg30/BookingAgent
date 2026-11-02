"""Supported typed booking API. No HTTP server or chat model is required.

``BookingEngine`` runs each command as one turn through ``TurnRunner`` (turn_runner.py), the same pipeline the
chat agent uses. A command needs a ``conversation_id``; the quote it returns is confirmed with its id and version.
"""

from __future__ import annotations

from datetime import date, time
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from booking_agent.clock import Clock, SystemClock, ZonedClock
from booking_agent.config import ServiceCatalog
from booking_agent.domain.entities import Criteria, Frozen, ProviderBooking, ProviderOffer, Quote, TimeRange
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.planning import MAX_DAYS_AHEAD, WEEKDAYS, part_range
from booking_agent.errors import ProviderError
from booking_agent.flow.context import Deps, TurnContext, TurnRequest, TurnResult, confirm_quote, decline_quote
from booking_agent.flow.quotes import pick_slot
from booking_agent.flow.search import run_new_search, search_offers
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.jobs import JOB_HANDLERS
from booking_agent.jobs.worker import JobWorker
from booking_agent.limits import Limits
from booking_agent.memory_repository import MemoryRepository
from booking_agent.model import ChatModel
from booking_agent.ports import Repository
from booking_agent.prompts import load_prompts
from booking_agent.provider.client import ProviderAPI, ProviderClient
from booking_agent.render import NOTICE_CARD, blocked_text
from booking_agent.tracing import Telemetry, Tracer
from booking_agent.turn_runner import Operation, TurnRunner, build_deps


class EngineConfig(ServiceCatalog):
    """Business timezone/catalogue. Provider amounts use the configured currency's minor unit."""

    currency: str = Field(default="INR", min_length=3, max_length=3)

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        ZoneInfo(self.timezone)
        return self


class BookingRequest(Frozen):
    service: str = Field(min_length=1)
    area: str = Field(min_length=1)
    date_from: date | None = None
    date_to: date | None = None
    part_of_day: str = "any"
    budget_rupees: int | None = Field(default=None, gt=0)
    earliest_time: time | None = None
    exclude_weekdays: tuple[int, ...] = ()

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.date_to is not None and self.date_from is None:
            raise ValueError("date_to requires date_from")
        if self.date_from is not None and self.date_to is not None and self.date_to < self.date_from:
            raise ValueError("date_to precedes date_from")
        if self.part_of_day not in ("any", "morning", "afternoon", "evening"):
            raise ValueError("invalid part_of_day")
        if any(day not in range(7) for day in self.exclude_weekdays):
            raise ValueError("invalid weekday")
        return self

    def as_criteria(self) -> Criteria:
        times: tuple[TimeRange, ...] = ()
        part = part_range(self.part_of_day)
        if part is not None or self.earliest_time is not None:
            start = max(part.from_time if part else time(6), self.earliest_time or time(0))
            end = part.to_time if part else time(22)
            times = (TimeRange(from_time=start, to_time=end),)
        days = tuple(WEEKDAYS[i] for i in range(7) if i not in self.exclude_weekdays) if self.exclude_weekdays else ()
        return Criteria(
            service=self.service,
            area=self.area,
            date_from=self.date_from,
            date_to=self.date_to or self.date_from,
            any_date=self.date_from is None,
            days=days,
            times=times,
            max_price=self.budget_rupees,
            no_budget=self.budget_rupees is None,
        )


class ConfirmQuote(Frozen):
    quote_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class SelectSlot(Frozen):
    provider_id: str = Field(min_length=1)
    slot_start: AwareDatetime


class BookingResult(Frozen):
    exit: ExitReason
    quotes: tuple[Quote, ...] = ()
    booking_ids: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    currency: str = "INR"


class BookingEngine:
    """Compose supplied integrations. Injected resources are never closed by this engine.

    The default repository is in-memory; pass a durable Repository for restart recovery. user_id must come from
    the embedding application's authenticated principal.
    """

    def __init__(
        self,
        *,
        provider: ProviderAPI,
        repository: Repository | None = None,
        clock: Clock | None = None,
        config: EngineConfig | None = None,
        limits: Limits | None = None,
        telemetry: Telemetry | None = None,
        memory_model: ChatModel | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.repository: Repository = repository if repository is not None else MemoryRepository()
        self._owns_repository = repository is None
        self._owns_telemetry = telemetry is None
        self._closed = False
        self._clock = ZonedClock(clock or SystemClock(), self.config.timezone)
        self._provider_client = ProviderClient(provider)
        self._telemetry = telemetry or Telemetry()
        deps = build_deps(
            store=self.repository,
            provider_client=self._provider_client,
            clock=self._clock,
            limits=limits or Limits(),
            telemetry=self._telemetry,
            prompts=load_prompts(),
            catalog=self.config,
            memory_model=memory_model,
        )
        self._runner = TurnRunner(deps)

    # ---------------------------------------------------------------- read-only queries

    def search(self, *, request: BookingRequest) -> tuple[ProviderOffer, ...]:
        """Ranked availability. No conversation, hold or write is created."""
        self._require_open()
        now = self._clock.now()
        self._validate(request, now.date())
        calls = ProviderCalls(self._provider_client, Tracer(self._telemetry), self._clock, None)
        outcome = search_offers(calls, now, request.as_criteria(), set())
        if outcome.provider_failed:
            raise ProviderError("provider_unavailable")
        return outcome.offers

    def list_bookings(self, *, user_id: str) -> tuple[ProviderBooking, ...]:
        """The provider's own list of the user's bookings (the source of truth)."""
        self._require_open()
        if not user_id:
            raise ValueError("user_id must not be empty")
        answer = self._provider_client.list_bookings(user_id)
        if not answer.ok or answer.value is None:
            raise ProviderError(answer.error or "provider_unavailable")
        return tuple(answer.value)

    # ---------------------------------------------------------------- commands (one turn each)

    def propose(self, *, user_id: str, conversation_id: str, request: BookingRequest) -> BookingResult:
        """Search, hold the best slot and return it as a quote. Nothing is booked until ``confirm``."""
        self._validate(request, self._clock.now().date())

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            outcome = run_new_search(deps, turn, request.as_criteria())
            if not outcome.offers:
                turn.say(blocked_text(outcome.reason or "no_providers"), NOTICE_CARD)
                turn.end(ExitReason(kind="blocked", detail=outcome.reason))
                return
            best = outcome.offers[0]
            pick_slot(deps, turn, best.provider_id, best.slots[0].start)

        return self._run(user_id, conversation_id, operation, {"action": "propose"})

    def choose_slot(self, *, user_id: str, conversation_id: str, command: SelectSlot) -> BookingResult:
        """Quote a slot that the last search offered; an unoffered slot is refused."""

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            result = pick_slot(deps, turn, command.provider_id, command.slot_start)
            if result.status == "not_offered":
                turn.say("That isn't one I can offer.", NOTICE_CARD)

        return self._run(user_id, conversation_id, operation, {"action": "choose_slot"})

    def confirm(self, *, user_id: str, conversation_id: str, command: ConfirmQuote) -> BookingResult:
        """Confirm the quote on screen and queue the write. A replaced quote is refused as stale.

        The result has exit kind ``queued``. The provider is called later by ``run_jobs`` or a worker from
        ``job_worker``. Raises ``EnqueueFailed`` if the confirmation could not be saved with its job.
        """

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            confirm_quote(deps, turn, command.quote_id, command.version)

        return self._run(user_id, conversation_id, operation, {"action": "confirm"})

    def decline(self, *, user_id: str, conversation_id: str, quote_id: str) -> BookingResult:
        """Drop the quote on screen. Nothing is booked or changed."""

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            decline_quote(turn, quote_id)

        return self._run(user_id, conversation_id, operation, {"action": "decline"})

    def cancel(self, *, user_id: str, conversation_id: str, booking_id: str) -> BookingResult:
        """Quote cancelling one of our bookings. Nothing changes until ``confirm``."""

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            from booking_agent.flow.tools.booking import PROPOSE_CANCELLATION

            result = PROPOSE_CANCELLATION.handler(deps, turn, {"booking_id": booking_id})
            if "error" in result:
                turn.say(str(result["error"]), NOTICE_CARD)

        return self._run(user_id, conversation_id, operation, {"action": "cancel"})

    def reschedule(
        self, *, user_id: str, conversation_id: str, booking_id: str, request: BookingRequest
    ) -> BookingResult:
        """Quote moving a booking to the first slot that fits ``request``. Nothing changes until ``confirm``."""
        self._validate(request, self._clock.now().date())

        def operation(deps: Deps, turn: TurnContext, turn_request: TurnRequest) -> None:
            from booking_agent.flow.tools.booking import PROPOSE_RESCHEDULE

            start = request.date_from or turn.now.date()
            args = {
                "booking_id": booking_id,
                "date_from": start.isoformat(),
                "date_to": (request.date_to or start).isoformat(),
            }
            result = PROPOSE_RESCHEDULE.handler(deps, turn, args)
            if "error" in result:
                turn.say(str(result["error"]), NOTICE_CARD)

        return self._run(user_id, conversation_id, operation, {"action": "reschedule"})

    # ---------------------------------------------------------------- background jobs

    def job_worker(self) -> JobWorker:
        """A worker for the host to run in its own thread: ``worker.run_forever(stop_event)``."""
        self._require_open()
        return JobWorker(self._runner.deps, self._runner, JOB_HANDLERS)

    def run_jobs(self, max_jobs: int | None = None) -> int:
        """Run the jobs that are due and return how many ran."""
        return self.job_worker().run_due(max_jobs)

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        if not self._closed:
            if self._owns_repository:
                self.repository.close()
            if self._owns_telemetry:
                self._telemetry.shutdown()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ---------------------------------------------------------------- helpers

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("engine is closed")

    def _validate(self, request: BookingRequest, today: date) -> None:
        if request.service not in self.config.services:
            raise ValueError("unsupported service")
        if request.area not in self.config.areas:
            raise ValueError("unsupported area")
        last_day = request.date_to or request.date_from
        if last_day is not None and last_day < today:
            raise ValueError("request window is in the past")
        if last_day is not None and (last_day - today).days > MAX_DAYS_AHEAD:
            raise ValueError(f"request window must end within {MAX_DAYS_AHEAD} days")

    def _run(self, user_id: str, conversation_id: str, operation: Operation, data: dict[str, object]) -> BookingResult:
        """Run one command as a turn. Raises AccessDenied for another user's conversation."""
        self._require_open()
        if not user_id or not conversation_id:
            raise ValueError("user_id and conversation_id must not be empty")
        request = TurnRequest(conversation_id=conversation_id, user_id=user_id, operation_data=data)
        return self._result(self._runner.run(request, operation), conversation_id)

    def _result(self, result: TurnResult, conversation_id: str) -> BookingResult:
        current = self.repository.current_quote_for_conversation(conversation_id)
        cards = [message.card for message in result.messages if message.card]
        return BookingResult(
            exit=result.exit,
            quotes=(current,) if current is not None and current.status == "shown" else (),
            booking_ids=tuple(card["booking_id"] for card in cards if card.get("type") == "confirmation"),
            notices=tuple(message.text for message in result.messages),
            currency=self.config.currency,
        )


__all__ = ["BookingEngine", "BookingRequest", "BookingResult", "ConfirmQuote", "EngineConfig", "SelectSlot"]
