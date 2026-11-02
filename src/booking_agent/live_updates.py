"""In-process fan-out of conversation updates to open event streams.

This bus works within one process. The app runs as one replica, so a worker thread and the HTTP
routes share the same ``SessionUpdates`` object. With more than one replica an event published in one
process would never reach a stream open in another; that would need Postgres LISTEN/NOTIFY or Redis
pub/sub in place of this class.

Events are not stored. A browser that was disconnected when an event was published does not get it
later, so clients should reload the conversation after reconnecting.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from types import TracebackType
from typing import Any, Self

SUBSCRIBER_QUEUE_SIZE = 100


class Subscription:
    """One listener on one conversation. Use as a context manager; leaving it unsubscribes."""

    def __init__(self, bus: SessionUpdates, conversation_id: str, size: int) -> None:
        self._bus = bus
        self._conversation_id = conversation_id
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=size)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop listening. Safe to call more than once."""
        self._bus.unsubscribe(self._conversation_id, self)

    def push(self, event: dict[str, Any]) -> None:
        """Queue an event. When the queue is full the oldest event is dropped to make room."""
        while True:
            try:
                self._events.put_nowait(event)
                return
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self._events.get_nowait()

    def next_event(self, timeout_s: float) -> dict[str, Any] | None:
        """The next event, or None if none arrived within ``timeout_s`` seconds."""
        try:
            return self._events.get(timeout=timeout_s)
        except queue.Empty:
            return None


class SessionUpdates:
    """Thread-safe publish and subscribe, keyed by conversation id."""

    def __init__(self, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Subscription]] = {}

    def subscribe(self, conversation_id: str) -> Subscription:
        """Start listening to a conversation. Returns a context manager; the subscription is live at once."""
        subscription = Subscription(self, conversation_id, self._queue_size)
        with self._lock:
            self._subscribers.setdefault(conversation_id, []).append(subscription)
        return subscription

    def unsubscribe(self, conversation_id: str, subscription: Subscription) -> None:
        with self._lock:
            remaining = [item for item in self._subscribers.get(conversation_id, []) if item is not subscription]
            if remaining:
                self._subscribers[conversation_id] = remaining
            else:
                self._subscribers.pop(conversation_id, None)

    def subscriber_count(self, conversation_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(conversation_id, []))

    def publish(self, conversation_id: str, event: dict[str, Any]) -> None:
        """Deliver an event to every current subscriber of the conversation. Drops it if there are none."""
        with self._lock:
            listeners = list(self._subscribers.get(conversation_id, []))
        for listener in listeners:
            listener.push(event)
