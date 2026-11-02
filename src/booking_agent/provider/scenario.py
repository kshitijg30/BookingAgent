"""Configurable mock behaviour so every failure in the contract can be reproduced on demand."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"]


@dataclass(frozen=True)
class SeedBooking:
    """A booking that exists before the session starts (history for "same as last time", reschedules)."""

    user_id: str
    service: str
    area: str
    provider_index: int
    days_from_today: int
    hour: int
    status: Literal["upcoming", "completed", "cancelled"]


@dataclass(frozen=True)
class Scenario:
    seed: int = 7
    # Forced outcomes for successive book calls; after the list is used up, the random mix applies.
    book_outcomes: tuple[BookOutcome, ...] = ()
    # The brief's mix: 70% success, 15% slot_taken, 15% timeout of which half actually booked.
    book_mix: tuple[float, float, float] = (0.70, 0.15, 0.075)
    status_timeouts: int = 0  # first N get_booking_status calls time out
    quote_drift_rupees: int = 0  # rupees added to the quoted price at hold time
    quote_drift_holds: int = 1_000_000  # apply drift to the first N holds only
    hold_unavailable_first: int = 0  # first N hold_slot calls fail with slot_unavailable
    hold_ttl: timedelta = timedelta(minutes=10)
    reschedule_conflict_hours: tuple[int, ...] = ()  # slot start hours that return slot_conflict
    cancel_first_call_503: bool = True
    search_timeouts: int = 0
    inject_all_reviews: bool = False  # put the injection review on every provider
    seed_bookings: tuple[SeedBooking, ...] = field(default_factory=tuple)


def default_history(user_id: str) -> tuple[SeedBooking, ...]:
    """Past electrician jobs (mornings), and an upcoming AC servicing booking."""
    return (
        SeedBooking(user_id, "electrician", "Koramangala", 1, -40, 10, "completed"),
        SeedBooking(user_id, "electrician", "Koramangala", 1, -21, 10, "completed"),
        SeedBooking(user_id, "plumber", "Koramangala", 2, -12, 8, "completed"),
        SeedBooking(user_id, "ac_service", "Koramangala", 0, 3, 14, "upcoming"),
    )
