"""Hard caps on model calls, tool calls and wall-clock time, plus a per-session rate limit."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass

from booking_agent.clock import Clock
from booking_agent.domain.exits import LimitKind


@dataclass(frozen=True)
class Limits:
    model_calls_per_turn: int = 3  # includes one repair retry and one escalation
    tool_calls_per_turn: int = 12
    wall_clock_s: float = 25.0
    verify_reserve_s: float = 10.0  # extra time only for verifying a write already in flight
    turns_per_session: int = 40
    bookings_per_user_per_day: int = 5
    messages_per_minute: int = 20


class LimitHit(Exception):
    def __init__(self, kind: LimitKind) -> None:
        super().__init__(kind)
        self.kind = kind


class TurnBudget:
    def __init__(self, clock: Clock, limits: Limits) -> None:
        self.clock = clock
        self.limits = limits
        self.started = clock.monotonic()
        self.tool_calls = 0
        self.model_calls = 0

    def elapsed(self) -> float:
        return self.clock.monotonic() - self.started

    def charge_tool(self, *, verifying: bool = False) -> None:
        if not verifying:
            if self.tool_calls >= self.limits.tool_calls_per_turn:
                raise LimitHit("tool_calls")
            if self.elapsed() >= self.limits.wall_clock_s:
                raise LimitHit("wall_clock")
        elif self.elapsed() >= self.limits.wall_clock_s + self.limits.verify_reserve_s:
            raise LimitHit("wall_clock")
        self.tool_calls += 1

    def charge_model_call(self) -> None:
        if self.model_calls >= self.limits.model_calls_per_turn:
            raise LimitHit("model_calls")
        if self.elapsed() >= self.limits.wall_clock_s:
            raise LimitHit("wall_clock")
        self.model_calls += 1


class RateLimiter:
    def __init__(self, clock: Clock, per_minute: int) -> None:
        self.clock = clock
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock.monotonic()
        with self._lock:
            recent = self._hits[key]
            while recent and now - recent[0] > 60:
                recent.popleft()
            if len(recent) >= self.per_minute:
                return False
            recent.append(now)
            return True
