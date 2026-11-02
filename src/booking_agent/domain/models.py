"""Shared base class, the business time zone and the provider's own view of a booking."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

IST = ZoneInfo("Asia/Kolkata")
ProviderBookingStatus = Literal["upcoming", "completed", "cancelled", "cancelled_by_provider"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderBooking(Frozen):
    """A booking as the provider reports it. The provider is the source of truth for status and history."""

    booking_id: str
    user_id: str
    provider_id: str
    provider_name: str
    service: str
    area: str
    slot_start: AwareDatetime
    slot_end: AwareDatetime
    price: int
    status: ProviderBookingStatus


def at_ist(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=IST)
