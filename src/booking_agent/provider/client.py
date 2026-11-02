"""Client adapter: calls the provider, validates every response, and maps failures to Outcome.

- Timeouts become ``unknown`` (the call may have taken effect).
- Known provider errors become ``err`` with their code.
- A malformed response to a write becomes ``unknown`` (never a guessed success); to a read, ``err``.
- Provider free text is wrapped in ``Untrusted`` before anything else can touch it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import TypeVar

from pydantic import ValidationError

from booking_agent.domain.contract import (
    BookingStatusResult,
    Hold,
    Outcome,
    ProviderDetails,
    ProviderSummary,
    SlotModel,
)
from booking_agent.domain.models import IST, ProviderBooking
from booking_agent.domain.untrusted import Untrusted
from booking_agent.errors import ProviderError, ProviderTimeout
from booking_agent.provider.contract import ProviderAPI as ProviderAPI
from booking_agent.provider.contract import ProviderCapabilities

T = TypeVar("T")


def _local(moment: datetime) -> str:
    """Slots go to the provider in the business timezone; stored quotes carry UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST).isoformat()


def _call(fn: Callable[[], T], *, write: bool) -> Outcome[T]:
    try:
        return Outcome("ok", fn())
    except ProviderTimeout:
        return Outcome("unknown", error="timeout")
    except ProviderError as error:
        return Outcome("err", error=error.code)
    except (ValidationError, KeyError, TypeError, ValueError):
        return Outcome("unknown", error="bad_response") if write else Outcome("err", error="bad_response")
    except Exception:
        return Outcome("unknown", error="transport") if write else Outcome("err", error="transport")


class ProviderClient:
    def __init__(self, api: ProviderAPI) -> None:
        self.api = api

    def route_key(self, provider_id: str, key: str) -> str:
        route = getattr(self.api, "route_key", None)
        return str(route(provider_id, key)) if route is not None else key

    def capabilities_for(self) -> ProviderCapabilities:
        capabilities = getattr(self.api, "capabilities", None)
        return capabilities if isinstance(capabilities, ProviderCapabilities) else ProviderCapabilities()

    def search_providers(self, service: str, area: str, start: date, end: date) -> Outcome[list[ProviderSummary]]:
        def run() -> list[ProviderSummary]:
            seen: set[str] = set()
            providers = []
            for raw in self.api.search_providers(service, area, (start, end)):
                provider = ProviderSummary(
                    id=raw["id"],
                    name=raw["name"],
                    price=raw["price"],
                    rating=raw.get("rating"),
                    technician=raw.get("technician"),
                    attributes=tuple(sorted((str(k), str(v)) for k, v in (raw.get("attributes") or {}).items())),
                    free_slots=tuple(
                        SlotModel(start=datetime.fromisoformat(s["start"]), end=datetime.fromisoformat(s["end"]))
                        for s in raw["free_slots"]
                    ),
                )
                if provider.id not in seen:
                    seen.add(provider.id)
                    providers.append(provider)
            return providers

        return _call(run, write=False)

    def get_provider(self, provider_id: str) -> Outcome[ProviderDetails]:
        def run() -> ProviderDetails:
            raw = self.api.get_provider(provider_id)
            return ProviderDetails(
                id=raw["id"],
                name=Untrusted(raw["name"]),
                rating=raw.get("rating"),
                reviews=tuple(Untrusted(r) for r in raw.get("reviews", [])),
            )

        return _call(run, write=False)

    def list_bookings(self, user_id: str) -> Outcome[list[ProviderBooking]]:
        def run() -> list[ProviderBooking]:
            return [
                ProviderBooking(
                    booking_id=b["booking_id"],
                    user_id=b["user_id"],
                    provider_id=b["provider_id"],
                    provider_name=b["provider_name"],
                    service=b["service"],
                    area=b["area"],
                    slot_start=datetime.fromisoformat(b["slot_start"]),
                    slot_end=datetime.fromisoformat(b["slot_end"]),
                    price=b["price"],
                    status=b["status"],
                )
                for b in self.api.list_bookings(user_id)
                if b["user_id"] == user_id
            ]

        return _call(run, write=False)

    def hold_slot(self, provider_id: str, slot_start: datetime, *, user_id: str, area: str) -> Outcome[Hold]:
        def run() -> Hold:
            raw = self.api.hold_slot(provider_id, _local(slot_start), user_id=user_id, area=area)
            return Hold(
                hold_id=raw["hold_id"],
                provider_id=raw["provider_id"],
                slot=SlotModel(
                    start=datetime.fromisoformat(raw["slot"]["start"]), end=datetime.fromisoformat(raw["slot"]["end"])
                ),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
                quoted_price=raw["quoted_price"],
            )

        return _call(run, write=False)

    def book(self, hold_id: str, key: str) -> Outcome[str]:
        def run() -> str:
            booking_id = self.api.book(hold_id, key)["booking_id"]
            if not isinstance(booking_id, str) or not booking_id:
                raise ValueError("missing booking_id")
            return booking_id

        return _call(run, write=True)

    def get_booking_status(self, key: str) -> Outcome[BookingStatusResult]:
        def run() -> BookingStatusResult:
            raw = self.api.get_booking_status(key)
            return BookingStatusResult(status=raw["status"], booking_id=raw.get("booking_id"))

        return _call(run, write=False)

    def cancel(self, booking_id: str, key: str) -> Outcome[None]:
        def run() -> None:
            if self.api.cancel(booking_id, key).get("status") != "cancelled":
                raise ValueError("unexpected cancel response")

        return _call(run, write=True)

    def reschedule(self, booking_id: str, new_start: datetime, key: str) -> Outcome[None]:
        def run() -> None:
            self.api.reschedule(booking_id, _local(new_start), key)

        return _call(run, write=True)
