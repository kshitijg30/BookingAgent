"""The in-process session update bus."""

from __future__ import annotations

import threading
import time

from booking_agent.live_updates import SessionUpdates


def test_delivers_only_to_subscribers_of_that_session() -> None:
    bus = SessionUpdates()
    with bus.subscribe("a") as first, bus.subscribe("a") as second, bus.subscribe("b") as other:
        bus.publish("a", {"type": "conversation.updated", "n": 1})
        assert first.next_event(0.1) == {"type": "conversation.updated", "n": 1}
        assert second.next_event(0.1) == {"type": "conversation.updated", "n": 1}
        assert other.next_event(0.05) is None


def test_publish_without_subscribers_is_dropped() -> None:
    bus = SessionUpdates()
    bus.publish("nobody", {"type": "x"})
    with bus.subscribe("nobody") as late:
        assert late.next_event(0.05) is None


def test_leaving_the_context_unsubscribes() -> None:
    bus = SessionUpdates()
    with bus.subscribe("a") as subscription:
        assert bus.subscriber_count("a") == 1
    assert bus.subscriber_count("a") == 0
    bus.publish("a", {"type": "x"})
    assert subscription.next_event(0.05) is None


def test_overflow_drops_the_oldest_event() -> None:
    bus = SessionUpdates(queue_size=3)
    with bus.subscribe("a") as subscription:
        for number in range(5):
            bus.publish("a", {"n": number})
        received = [subscription.next_event(0.05) for _ in range(4)]
    assert received == [{"n": 2}, {"n": 3}, {"n": 4}, None]


def test_next_event_times_out_with_none() -> None:
    bus = SessionUpdates()
    with bus.subscribe("a") as subscription:
        started = time.monotonic()
        assert subscription.next_event(0.05) is None
        assert time.monotonic() - started < 2


def test_publish_from_another_thread_wakes_a_waiting_subscriber() -> None:
    bus = SessionUpdates()
    with bus.subscribe("a") as subscription:
        threading.Timer(0.05, bus.publish, args=("a", {"n": 1})).start()
        assert subscription.next_event(5) == {"n": 1}
