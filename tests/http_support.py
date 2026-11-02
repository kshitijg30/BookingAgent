"""Test support for the HTTP layer: a fake agent with the documented interface and seeded rows.

The HTTP routes only use ``Agent.handle`` and the repository, so these tests run a fake agent over a
``MemoryRepository``. The job worker and the real core are not involved.
"""

from __future__ import annotations

import sys
import types
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from booking_agent.clock import FixedClock
from booking_agent.config import DEFAULT_CATALOG, Settings
from booking_agent.domain.entities import (
    Booking,
    ChatMessage,
    Conversation,
    Criteria,
    ProviderOffer,
    Quote,
    Search,
    Slot,
)
from booking_agent.limits import Limits
from booking_agent.memory_repository import MemoryRepository
from booking_agent.prompts import load_prompts
from booking_agent.provider.scenario import Scenario

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
USER = "u1"
OTHER_USER = "u2"


def ensure_card_action_shape() -> None:
    """Use the core's ``CardAction`` when it matches the contract, else register a stand-in module.

    The contract is ``CardAction(kind, quote_id, version)`` in ``booking_agent.flow.context``. While the
    core is being rewritten that module may not import, or may still be the old shape.
    """
    try:
        from booking_agent.flow.context import CardAction

        CardAction(kind="confirm", quote_id="q", version=1)
    except (ImportError, TypeError):
        from dataclasses import dataclass as make_dataclass

        stand_in = types.ModuleType("booking_agent.flow.context")

        @make_dataclass(frozen=True)
        class CardAction:  # type: ignore[no-redef]
            kind: str
            quote_id: str
            version: int | None = None

        stand_in.CardAction = CardAction  # type: ignore[attr-defined]
        sys.modules["booking_agent.flow.context"] = stand_in


class ProviderBookingRow(BaseModel):
    booking_id: str
    status: str


@dataclass
class FakeTurnResult:
    """What ``TurnResult.to_dict`` returns."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.payload


class FakeTelemetry:
    def summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    def load(self, trace_id: str) -> list[dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        return None


class FakeProviderClient:
    def __init__(self) -> None:
        self.rows = [ProviderBookingRow(booking_id="pb-1", status="upcoming")]

    def list_bookings(self, user_id: str) -> Any:
        return types.SimpleNamespace(ok=True, value=self.rows)


class FakeDemoProvider:
    """The demo-control surface of the mock provider."""

    def __init__(self) -> None:
        self.scenario = Scenario(seed=1)
        self.forced_book: deque[Any] = deque()
        self.forced_status_timeouts = 0
        self.cancelled: list[str] = []

    def provider_cancel(self, booking_id: str) -> None:
        self.cancelled.append(booking_id)


@dataclass
class FakeAgent:
    """Implements the agent interface the HTTP layer uses."""

    store: MemoryRepository = field(default_factory=MemoryRepository)
    clock: FixedClock = field(default_factory=lambda: FixedClock(NOW))
    model: Any = None
    limits: Limits = field(default_factory=Limits)
    telemetry: FakeTelemetry = field(default_factory=FakeTelemetry)
    provider_client: FakeProviderClient = field(default_factory=FakeProviderClient)
    default_prompts: Any = field(default_factory=load_prompts)
    deps: Any = field(default_factory=lambda: types.SimpleNamespace(catalog=DEFAULT_CATALOG))
    runner: Any = field(default_factory=lambda: types.SimpleNamespace(on_jobs_queued=None))
    handled: list[dict[str, Any]] = field(default_factory=list)
    outcome: Any = None  # an exception to raise, or the dict to return
    applied: list[tuple[Any, Limits]] = field(default_factory=list)
    scan_result: int = 0

    def handle(self, **request: Any) -> FakeTurnResult:
        self.handled.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        payload = self.outcome or {
            "text": "ok",
            "exit": {"kind": "answered"},
            "trace_id": "t1",
            "answered_by": "fake",
            "messages": [],
            "card": None,
        }
        return FakeTurnResult(payload)

    def apply_settings(self, *, prompts: Any, limits: Limits) -> None:
        self.applied.append((prompts, limits))

    def scan_provider_cancellations(self) -> int:
        return self.scan_result


def make_settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(data_dir=tmp_path, trace_dir=tmp_path / "traces", _env_file=None, **extra)


def at(hour: int, day: int = 3) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


def seed_conversation(
    agent: FakeAgent, conversation_id: str, messages: list[ChatMessage], user_id: str = USER
) -> Conversation:
    conversation = Conversation(
        id=conversation_id,
        user_id=user_id,
        title="Plumber",
        messages=tuple(messages),
        created_at=NOW,
        updated_at=NOW,
    )
    return agent.store.save_conversation(conversation, expected_version=0)


def agent_message(text: str, card: dict[str, Any] | None = None) -> ChatMessage:
    return ChatMessage(role="agent", text=text, card=card, at=NOW)


def user_message(text: str) -> ChatMessage:
    return ChatMessage(role="user", text=text, at=NOW)


def seed_search(agent: FakeAgent, search_id: str, conversation_id: str) -> Search:
    offers = (
        ProviderOffer(provider_id="p-cheap", name="Cheap Co", rating=4.0, price=900, slots=(Slot(start=at(10), end=at(11)),)),
        ProviderOffer(provider_id="p-best", name="Best Co", rating=4.8, price=1200, slots=(Slot(start=at(12), end=at(13)),)),
        ProviderOffer(provider_id="p-same", name="Same Rating Co", rating=4.0, price=800, slots=()),
    )  # fmt: skip
    search = Search(
        id=search_id,
        conversation_id=conversation_id,
        criteria=Criteria(service="plumber"),
        results=offers,
        created_at=NOW,
    )
    agent.store.add_search(search)
    return search


def seed_quote(agent: FakeAgent, quote_id: str, conversation_id: str, **changes: Any) -> Quote:
    fields: dict[str, Any] = {
        "id": quote_id,
        "conversation_id": conversation_id,
        "user_id": USER,
        "action": "book",
        "provider_id": "p-best",
        "provider_name": "Best Co",
        "service": "plumber",
        "area": "Koramangala",
        "start": at(12),
        "end": at(13),
        "price": 1200,
        "created_at": NOW,
        "updated_at": NOW,
    } | changes
    quote = Quote(**fields)
    agent.store.add_quote(quote)
    return quote


def seed_booking(agent: FakeAgent, booking_id: str, quote_id: str, conversation_id: str, **changes: Any) -> Booking:
    fields: dict[str, Any] = {
        "id": booking_id,
        "user_id": USER,
        "conversation_id": conversation_id,
        "quote_id": quote_id,
        "provider_id": "p-best",
        "provider_name": "Best Co",
        "service": "plumber",
        "area": "Koramangala",
        "start": at(12),
        "end": at(13),
        "price": 1200,
        "created_at": NOW,
        "updated_at": NOW,
    } | changes
    booking = Booking(**fields)
    agent.store.add_booking(booking)
    return booking
