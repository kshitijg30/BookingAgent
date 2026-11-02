"""Neutral provider capability declarations. Uncertainty is the safe default."""

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    """Trust flags a provider declares about its answers.

    negative_status_authoritative: a ``not_found`` from ``get_booking_status`` proves the write did not happen.
    read_back_authoritative: a ``list_bookings`` read that lacks the booking proves a change was not applied.

    False (the default) is the safe choice. Unknown writes then stay unknown, and a ``not_found`` never
    triggers another write under a new key.
    """

    negative_status_authoritative: bool = False
    read_back_authoritative: bool = False


class AvailabilityProvider(Protocol):
    def search_providers(self, service: str, area: str, date_range: tuple[date, date]) -> list[dict[str, Any]]: ...
    def get_provider(self, provider_id: str) -> dict[str, Any]: ...


class BookingProvider(Protocol):
    def list_bookings(self, user_id: str) -> list[dict[str, Any]]: ...
    def hold_slot(self, provider_id: str, slot_start: str, *, user_id: str, area: str) -> dict[str, Any]: ...
    def book(self, hold_id: str, idempotency_key: str) -> dict[str, Any]: ...
    def get_booking_status(self, idempotency_key: str) -> dict[str, Any]: ...


class ChangeProvider(Protocol):
    def cancel(self, booking_id: str, idempotency_key: str) -> dict[str, Any]: ...
    def reschedule(self, booking_id: str, new_slot: str, idempotency_key: str) -> dict[str, Any]: ...


class ProviderAPI(AvailabilityProvider, BookingProvider, ChangeProvider, Protocol):
    """Full booking backend, composed from independently reusable capability contracts."""
