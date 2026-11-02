"""Time as a port. Domain code receives ``now``; it never reads the system clock.

Also ``KeyedLocks``: per-key reentrant coordination, with an optional durable repository boundary."""

from __future__ import annotations

import threading
import time as _time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from typing import Protocol

from booking_agent.domain.entities import IST


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)

    def monotonic(self) -> float:
        return _time.monotonic()


class FixedClock:
    """Deterministic clock for tests. ``sleep`` advances time instead of waiting."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)

    def monotonic(self) -> float:
        return self._mono

    def advance(self, *, minutes: float = 0, seconds: float = 0) -> None:
        delta = timedelta(minutes=minutes, seconds=seconds)
        self._now += delta
        self._mono += delta.total_seconds()


class ZonedClock:
    """Normalize calendar decisions without changing the supplied clock's monotonic time."""

    def __init__(self, source: Clock, timezone: str) -> None:
        from zoneinfo import ZoneInfo

        self.source = source
        self.timezone = ZoneInfo(timezone)

    def now(self) -> datetime:
        return self.source.now().astimezone(self.timezone)

    def monotonic(self) -> float:
        return self.source.monotonic()

    def sleep(self, seconds: float) -> None:
        self.source.sleep(seconds)


class KeyedLocks:
    def __init__(self, backend: Callable[[str], AbstractContextManager[object]] | None = None) -> None:
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._guard = threading.Lock()
        self._backend = backend

    @contextmanager
    def get(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks[key]
        with lock:
            if self._backend is None:
                yield
            else:
                with self._backend(key):
                    yield
