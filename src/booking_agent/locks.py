"""Per-key reentrant coordination, with an optional durable repository boundary."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager


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
