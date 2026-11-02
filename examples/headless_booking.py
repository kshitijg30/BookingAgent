"""Run after `pip install .`; no web framework, model SDK or database dependency is needed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from booking_agent import (
    BookingEngine,
    BookingRequest,
    ConfirmQuote,
    EngineConfig,
    ProviderCapabilities,
    ProviderError,
)


class ExampleProvider:
    """An illustrative in-process backend; real adapters translate their own vendor APIs."""

    capabilities = ProviderCapabilities(negative_status_authoritative=True, read_back_authoritative=True)

    def __init__(self) -> None:
        self.slot = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        self.bookings: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.user = ""

    def search_providers(self, service: str, area: str, date_range: tuple[date, date]) -> list[dict[str, Any]]:
        if not date_range[0] <= self.slot.date() <= date_range[1]:
            return []
        return [
            {
                "id": "provider-1",
                "name": "Example provider",
                "price": 10000,
                "rating": 4.5,
                "technician": None,
                "free_slots": [{"start": self.slot.isoformat(), "end": (self.slot + timedelta(hours=1)).isoformat()}],
            }
        ]

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        return {"id": provider_id, "name": "Example provider", "rating": 4.5, "reviews": []}

    def list_bookings(self, user_id: str) -> list[dict[str, Any]]:
        return [dict(booking) for booking in self.bookings.values() if booking["user_id"] == user_id]

    def hold_slot(self, provider_id: str, slot_start: str, *, user_id: str, area: str) -> dict[str, Any]:
        self.user = user_id
        return {
            "hold_id": "hold-1",
            "provider_id": provider_id,
            "slot": {"start": self.slot.isoformat(), "end": (self.slot + timedelta(hours=1)).isoformat()},
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "quoted_price": 10000,
        }

    def book(self, hold_id: str, idempotency_key: str) -> dict[str, Any]:
        booking_id = self.keys.setdefault(idempotency_key, "booking-1")
        self.bookings.setdefault(
            booking_id,
            {
                "booking_id": booking_id,
                "user_id": self.user,
                "provider_id": "provider-1",
                "provider_name": "Example provider",
                "service": "plumber",
                "area": "Koramangala",
                "slot_start": self.slot.isoformat(),
                "slot_end": (self.slot + timedelta(hours=1)).isoformat(),
                "price": 10000,
                "status": "upcoming",
            },
        )
        return {"booking_id": booking_id}

    def get_booking_status(self, idempotency_key: str) -> dict[str, Any]:
        booking_id = self.keys.get(idempotency_key)
        return {"status": "booked" if booking_id else "not_found", "booking_id": booking_id}

    def cancel(self, booking_id: str, idempotency_key: str) -> dict[str, Any]:
        if booking_id not in self.bookings:
            raise ProviderError("not_found")
        self.bookings[booking_id]["status"] = "cancelled"
        return {"status": "cancelled"}

    def reschedule(self, booking_id: str, new_slot: str, idempotency_key: str) -> dict[str, Any]:
        booking = self.bookings[booking_id]
        start = datetime.fromisoformat(new_slot)
        booking.update(slot_start=new_slot, slot_end=(start + timedelta(hours=1)).isoformat())
        return {"status": "rescheduled"}


def main() -> None:
    provider = ExampleProvider()
    with BookingEngine(provider=provider, config=EngineConfig(timezone="UTC")) as engine:
        request = BookingRequest(service="plumber", area="Koramangala")
        quote = engine.propose(user_id="customer-1", conversation_id="conversation-1", request=request).quotes[0]
        result = engine.confirm(
            user_id="customer-1",
            conversation_id="conversation-1",
            command=ConfirmQuote(quote_id=quote.id, version=quote.version),
        )
        if result.exit.kind != "queued":  # the provider has not been called yet
            raise SystemExit(f"confirm was not queued: {result.exit}")
        engine.run_jobs()  # a real host runs engine.job_worker().run_forever(stop_event) in a thread instead
        booking = engine.list_bookings(user_id="customer-1")[0]
        print("Confirmed:", booking.booking_id)


if __name__ == "__main__":
    main()
