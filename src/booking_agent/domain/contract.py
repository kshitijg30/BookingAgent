"""Data shapes of the provider tool contract (fixed by the brief). Pure data, no behaviour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from booking_agent.domain.entities import Frozen, ProviderBooking
from booking_agent.domain.untrusted import Untrusted

T = TypeVar("T")


class SlotModel(Frozen):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> SlotModel:
        if self.end <= self.start:
            raise ValueError("slot end must follow start")
        return self


class ProviderSummary(Frozen):
    id: str
    name: str
    price: int = Field(ge=0, strict=True)
    rating: float | None
    technician: str | None
    attributes: tuple[tuple[str, str], ...] = ()
    free_slots: tuple[SlotModel, ...] = ()


class ProviderDetails(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    id: str
    name: Untrusted
    rating: float | None
    reviews: tuple[Untrusted, ...]


class Hold(Frozen):
    hold_id: str
    provider_id: str
    slot: SlotModel
    expires_at: AwareDatetime
    quoted_price: int = Field(ge=0, strict=True)


class BookingStatusResult(Frozen):
    status: Literal["booked", "not_found", "failed"]
    booking_id: str | None = None


ToolName = Literal[
    "search_providers",
    "get_provider",
    "list_bookings",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
]


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """Result of one tool call. 'unknown' means the call may or may not have taken effect."""

    kind: Literal["ok", "err", "unknown"]
    value: T | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


__all__ = [
    "BookingStatusResult",
    "Hold",
    "Outcome",
    "ProviderBooking",
    "ProviderDetails",
    "ProviderSummary",
    "SlotModel",
    "ToolName",
]
