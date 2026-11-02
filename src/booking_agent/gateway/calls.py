"""Provider calls that are charged to the turn budget and traced, plus the four read calls."""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from booking_agent.clock import Clock
from booking_agent.domain.contract import Hold, Outcome, ProviderDetails, ProviderSummary
from booking_agent.domain.entities import ProviderBooking
from booking_agent.limits import TurnBudget
from booking_agent.provider.client import ProviderClient
from booking_agent.tracing import Tracer

T = TypeVar("T")

FIRST_BACKOFF_S = 0.25  # pause before the second try; doubles after that


class ProviderCalls:
    """Runs one provider call at a time: charge the budget, call, trace the result."""

    def __init__(
        self, provider_client: ProviderClient, tracer: Tracer, clock: Clock, turn_limits: TurnBudget | None
    ) -> None:
        self.provider_client = provider_client
        self.tracer = tracer
        self.clock = clock
        self.turn_limits = turn_limits  # None outside a user turn (the background scan)

    def run(
        self,
        tool: str,
        args: dict[str, Any],
        call: Callable[[], Outcome[T]],
        *,
        verifying: bool = False,
        summarize: Callable[[T], Any] | None = None,
    ) -> Outcome[T]:
        """Charge the turn budget, call the provider, and trace the call with a short summary of its result."""
        if self.turn_limits is not None:
            self.turn_limits.charge_tool(verifying=verifying)
        with self.tracer.span(f"tool {tool}", **{"tool.name": tool, "tool.args": args}) as span:
            started = _time.perf_counter()
            outcome = call()
            traced_result: Any = None
            if outcome.kind == "ok" and outcome.value is not None:
                traced_result = summarize(outcome.value) if summarize else outcome.value
            self.tracer.set(
                span,
                outcome=outcome.kind,
                error=outcome.error,
                result=traced_result,
                latency_ms=round((_time.perf_counter() - started) * 1000, 2),
            )
        return outcome

    def pause_before(self, attempt: int) -> None:
        """No pause before the first try, then 0.25 s, 0.5 s, 1 s, ..."""
        if attempt:
            self.clock.sleep(FIRST_BACKOFF_S * 2 ** (attempt - 1))

    # ---------- reads ----------
    def search(self, service: str, area: str, start: Any, end: Any) -> Outcome[list[ProviderSummary]]:
        return self.run(
            "search_providers",
            {"service": service, "area": area, "date_range": [start, end]},
            lambda: self.provider_client.search_providers(service, area, start, end),
            summarize=lambda providers: [
                {"id": p.id, "price": p.price, "rating": p.rating, "free_slots": len(p.free_slots)} for p in providers
            ],
        )

    def get_provider(self, provider_id: str) -> Outcome[ProviderDetails]:
        return self.run(
            "get_provider",
            {"provider_id": provider_id},
            lambda: self.provider_client.get_provider(provider_id),
            summarize=lambda details: {"id": details.id, "reviews": list(details.reviews)},
        )

    def list_bookings(self, user_id: str, *, verifying: bool = False) -> Outcome[list[ProviderBooking]]:
        return self.run(
            "list_bookings",
            {"user_id": user_id},
            lambda: self.provider_client.list_bookings(user_id),
            verifying=verifying,
            summarize=lambda bookings: [
                {"id": b.booking_id, "status": b.status, "slot": b.slot_start.isoformat()} for b in bookings
            ],
        )

    def hold(self, provider_id: str, start: datetime, *, user_id: str, area: str) -> Outcome[Hold]:
        return self.run(
            "hold_slot",
            {"provider_id": provider_id, "slot": start},
            lambda: self.provider_client.hold_slot(provider_id, start, user_id=user_id, area=area),
        )
