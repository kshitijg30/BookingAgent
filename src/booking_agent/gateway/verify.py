"""Verifying a write against the provider, which is the source of truth.

Only a verified answer settles a write. An answer that is missing or not authoritative stays "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from booking_agent.domain.contract import BookingStatusResult, Outcome
from booking_agent.domain.entities import ProviderBooking
from booking_agent.gateway.calls import ProviderCalls

STATUS_CHECKS = 4  # get_booking_status calls before the outcome counts as unknown
NOT_FOUND_ANSWERS_NEEDED = 2  # "not_found" twice in a row before we believe nothing was booked
READ_BACK_TRIES = 3  # list_bookings calls when reading a booking back after cancel or reschedule
CANCEL_TRIES = 3  # cancel calls, retried only on a transient 503 and always with the same key


@dataclass(frozen=True)
class BookStatus:
    """What the provider says happened to one book call, after asking a few times."""

    status: Literal["booked", "not_booked", "failed", "unknown"]
    booking_id: str | None = None


@dataclass(frozen=True)
class ReadBack:
    """One booking as the provider has it now. ``reachable=False``: the provider did not answer."""

    reachable: bool
    booking: ProviderBooking | None = None


def check_book_status(calls: ProviderCalls, key: str) -> BookStatus:
    """Ask get_booking_status a few times. Only two "not_found" answers from an authoritative provider count."""
    not_found_answers = 0
    for attempt in range(STATUS_CHECKS):
        calls.pause_before(attempt)
        answer: Outcome[BookingStatusResult] = calls.run(
            "get_booking_status",
            {"idem_key": key},
            lambda: calls.provider_client.get_booking_status(key),
            verifying=True,
        )
        if answer.kind != "ok" or answer.value is None:
            continue
        if answer.value.status == "booked" and answer.value.booking_id:
            return BookStatus("booked", answer.value.booking_id)
        if answer.value.status == "failed":
            return BookStatus("failed")
        if (
            answer.value.status != "not_found"
            or not calls.provider_client.capabilities_for().negative_status_authoritative
        ):
            continue
        not_found_answers += 1
        if not_found_answers >= NOT_FOUND_ANSWERS_NEEDED:
            return BookStatus("not_booked")
    return BookStatus("unknown")


def read_back(calls: ProviderCalls, user_id: str, booking_id: str) -> ReadBack:
    """Look the booking up in list_bookings (cancel and reschedule have no status endpoint)."""
    for attempt in range(READ_BACK_TRIES):
        calls.pause_before(attempt)
        answer = calls.list_bookings(user_id, verifying=True)
        if answer.kind == "ok" and answer.value is not None:
            return ReadBack(True, next((b for b in answer.value if b.booking_id == booking_id), None))
    return ReadBack(reachable=False)


def cancel_with_retry(calls: ProviderCalls, booking_id: str, key: str) -> Outcome[None]:
    """Cancel, retrying only a transient 503 and always with the same key."""
    last: Outcome[None] = Outcome("unknown")
    for attempt in range(CANCEL_TRIES):
        calls.pause_before(attempt)
        last = calls.run(
            "cancel",
            {"booking_id": booking_id, "idem_key": key},
            lambda: calls.provider_client.cancel(booking_id, key),
            verifying=True,
        )
        if last.kind != "err" or last.error != "transient_503":
            break
    return last
