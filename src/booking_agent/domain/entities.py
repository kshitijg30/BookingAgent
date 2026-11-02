"""The data model: one frozen class per table, with the same fields.

A booking moves through these records in order:

    Search (what the user asked for and the offers that came back)
      -> Quote (one exact action shown with a Confirm button; the approval record)
        -> Job (the background write, see rows.Job)
          -> Booking (created only after the provider verifies the write)

Lists inside a row (messages, results, phones, addresses, times) are stored as JSON columns.
"""

from __future__ import annotations

from datetime import date, time
from typing import Any, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from booking_agent.domain.models import Frozen

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MessageRole = Literal["user", "agent"]
QuoteAction = Literal["book", "cancel", "reschedule"]
QuoteStatus = Literal["shown", "confirmed", "done", "failed", "declined", "expired"]
BookingStatus = Literal["booked", "cancelled", "cancelled_by_provider"]
JobKind = Literal["book", "cancel", "reschedule", "check", "memory_update", "research"]
JobStatus = Literal["pending", "running", "done", "failed", "dead"]

# ---------------------------------------------------------------- conversation


class ChatMessage(Frozen):
    """One message. ``card`` carries ids only; the UI reads the current status of what it points to."""

    role: MessageRole
    text: str
    card: dict[str, Any] | None = None
    at: AwareDatetime


class Conversation(Frozen):
    """The chat. Turn count is the number of user messages. ``version`` is the optimistic lock."""

    id: str
    user_id: str
    title: str | None = None
    messages: tuple[ChatMessage, ...] = ()
    summary: str = ""  # plain text of the older messages; ``summarized_count`` messages are folded into it
    summarized_count: int = 0
    version: int = 0
    created_at: AwareDatetime
    updated_at: AwareDatetime


# ---------------------------------------------------------------- search


class TimeRange(Frozen):
    """A time-of-day range each day. JSON keys are ``from`` and ``to``."""

    model_config = ConfigDict(populate_by_name=True)

    from_time: time = Field(alias="from")
    to_time: time = Field(alias="to")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.to_time <= self.from_time:
            raise ValueError("time range must end after it starts")
        return self


class Criteria(Frozen):
    """What the user asked for. ``None`` means not given yet.

    ``any_date`` and ``no_budget`` record "any day" and "no limit" so the agent never asks again.
    Empty ``days`` means every day, empty ``times`` means any time. ``max_price`` is paise, a strict
    upper bound.
    """

    service: str | None = None
    area: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    any_date: bool = False
    days: tuple[Weekday, ...] = ()
    times: tuple[TimeRange, ...] = ()
    max_price: int | None = None
    no_budget: bool = False
    provider_id: str | None = None
    replaces_booking_id: str | None = None
    reschedule_booking_id: str | None = None  # set by propose_reschedule: picks on this search move that booking
    exclude_provider_ids: tuple[str, ...] = ()  # providers never offered (the one that cancelled)
    relaxed: list[str] = Field(default_factory=list)  # constraints the research job loosened, in order

    def missing(self) -> list[str]:
        """The details to ask for before searching, in the order to ask them."""
        missing = [name for name in ("service", "area") if getattr(self, name) is None]
        if self.date_from is None and not self.any_date:
            missing.append("dates")
        if self.max_price is None and not self.no_budget:
            missing.append("budget")
        return missing


class Slot(Frozen):
    start: AwareDatetime
    end: AwareDatetime


class ProviderOffer(Frozen):
    """One provider from a search and its free slots. Name is provider text (untrusted)."""

    provider_id: str
    name: str
    rating: float | None
    price: int  # paise, as listed by search
    slots: tuple[Slot, ...]


class Search(Frozen):
    """A snapshot: the criteria and the offers returned for them. Never reused for other criteria."""

    id: str
    conversation_id: str
    criteria: Criteria
    results: tuple[ProviderOffer, ...] = ()
    created_at: AwareDatetime


# ---------------------------------------------------------------- quote and booking


class Quote(Frozen):
    """One exact action shown on a card with Confirm. It is the approval record.

    Confirm must send ``id`` and ``version``. A re-hold with a new price or time makes ``version`` + 1
    and sets ``status`` back to ``shown``.
    """

    id: str
    conversation_id: str
    user_id: str
    search_id: str | None = None
    action: QuoteAction
    booking_id: str | None = None
    provider_id: str
    provider_name: str
    service: str
    area: str
    start: AwareDatetime
    end: AwareDatetime
    price: int  # paise, as quoted by hold_slot
    hold_id: str | None = None
    hold_expires_at: AwareDatetime | None = None
    version: int = 1
    status: QuoteStatus = "shown"
    confirmed_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Booking(Frozen):
    """A booking this agent made. Created only after the provider verifies it."""

    id: str
    user_id: str
    conversation_id: str
    quote_id: str
    provider_id: str
    provider_name: str
    service: str
    area: str
    start: AwareDatetime
    end: AwareDatetime
    price: int
    status: BookingStatus = "booked"
    provider_booking_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


# ---------------------------------------------------------------- user


class Address(Frozen):
    label: str
    address: str  # free text; the model reads the area from it


class UserProfile(Frozen):
    user_id: str
    name: str | None = None
    phones: tuple[str, ...] = ()
    addresses: tuple[Address, ...] = ()
    # key -> {value, source: stated | inferred, confirmed, asked?}; see domain/memory.py
    memory: dict[str, Any] = Field(default_factory=dict)
    updated_at: AwareDatetime | None = None
