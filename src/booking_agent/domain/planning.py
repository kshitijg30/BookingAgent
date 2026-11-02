"""Pure search rules: which dates to search, which slots fit, which providers to offer and in what order."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date, datetime, time, timedelta
from typing import Literal

from booking_agent.domain.approval import MIN_LEAD
from booking_agent.domain.contract import ProviderSummary
from booking_agent.domain.entities import Criteria, ProviderOffer, Slot, TimeRange, Weekday
from booking_agent.domain.models import IST

EmptyReason = Literal["no_providers", "over_budget", "no_slots_in_window", "preferred_provider_unavailable"]

WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PART_RANGES: dict[str, tuple[time, time]] = {
    "morning": (time(6), time(12)),
    "afternoon": (time(12), time(17)),
    "evening": (time(17), time(22)),
}
MAX_DAYS_AHEAD = 30  # "any day" searches today..today+30; later dates are clamped
RELAX_EXTRA_DAYS = 7
RELAX_PRICE_FACTOR = 1.25

SlotKey = tuple[str, datetime]


def part_range(name: str) -> TimeRange | None:
    """The time range for "morning", "afternoon" or "evening"."""
    bounds = PART_RANGES.get(name.strip().lower())
    return TimeRange(from_time=bounds[0], to_time=bounds[1]) if bounds else None


def search_window(criteria: Criteria, today: date) -> tuple[date, date] | None:
    """The inclusive dates to search, or None if the dates are over or too far ahead to search."""
    last_allowed = today + timedelta(days=MAX_DAYS_AHEAD)
    if criteria.any_date or criteria.date_from is None:
        return today, last_allowed
    start = max(criteria.date_from, today)
    end = min(criteria.date_to or criteria.date_from, last_allowed)
    return (start, end) if start <= end else None


def slot_fits(start: datetime, criteria: Criteria, window: tuple[date, date], now: datetime) -> bool:
    """Does a slot starting at ``start`` meet the dates, days, times and minimum lead time?"""
    local = start.astimezone(IST)
    if not window[0] <= local.date() <= window[1]:
        return False
    if criteria.days and WEEKDAYS[local.weekday()] not in criteria.days:
        return False
    clock_time = local.time()
    if criteria.times and not any(r.from_time <= clock_time < r.to_time for r in criteria.times):
        return False
    return start - now >= MIN_LEAD


def _drop_reason(provider: ProviderSummary, criteria: Criteria) -> str | None:
    if provider.id in criteria.exclude_provider_ids:
        return "excluded"
    if provider.name.casefold() in {name.casefold() for name in criteria.exclude_provider_names}:
        return "excluded"
    if criteria.provider_id is not None and criteria.provider_id.casefold() not in (
        provider.id.casefold(),
        provider.name.casefold(),
    ):
        return "preferred"
    if criteria.max_price is not None and provider.price >= criteria.max_price:
        return "over_budget"
    return None


def _empty_reason(criteria: Criteria, drops: dict[str, int]) -> EmptyReason:
    if criteria.provider_id is not None:
        return "preferred_provider_unavailable"
    if drops.get("over_budget") and not drops.get("no_slot"):
        return "over_budget"
    if drops.get("no_slot"):
        return "no_slots_in_window"
    return "no_providers"


def build_offers(
    providers: Sequence[ProviderSummary],
    criteria: Criteria,
    window: tuple[date, date],
    now: datetime,
    excluded: Collection[SlotKey] = (),
) -> tuple[tuple[ProviderOffer, ...], EmptyReason | None]:
    """Providers with their qualifying slots, ranked by rating desc, price asc, id. Also why none, if none."""
    unique = {provider.id: provider for provider in reversed(providers)}.values()  # first result wins
    drops: dict[str, int] = {}
    ranked: list[tuple[tuple[float, int, str], ProviderOffer]] = []
    for provider in unique:
        reason = _drop_reason(provider, criteria)
        slots = () if reason else _slots_of(provider, criteria, window, now, excluded)
        if reason or not slots:
            key = reason or "no_slot"
            drops[key] = drops.get(key, 0) + 1
            continue
        offer = ProviderOffer(
            provider_id=provider.id, name=provider.name, rating=provider.rating, price=provider.price, slots=slots
        )
        ranked.append(((-(provider.rating or 0.0), provider.price, provider.id), offer))
    ranked.sort(key=lambda entry: entry[0])
    offers = tuple(offer for _, offer in ranked)
    return offers, (None if offers else _empty_reason(criteria, drops))


def _slots_of(
    provider: ProviderSummary,
    criteria: Criteria,
    window: tuple[date, date],
    now: datetime,
    excluded: Collection[SlotKey],
) -> tuple[Slot, ...]:
    fitting = (
        Slot(start=slot.start, end=slot.end)
        for slot in sorted(provider.free_slots, key=lambda slot: slot.start)
        if slot_fits(slot.start, criteria, window, now) and (provider.id, slot.start) not in excluded
    )
    return tuple(fitting)


def relaxed_variants(criteria: Criteria) -> list[tuple[str, Criteria]]:
    """The criteria with one constraint loosened at a time, in the order to try them."""
    variants: list[tuple[str, Criteria]] = []
    if criteria.times:
        variants.append(("times", criteria.model_copy(update={"times": ()})))
    if not criteria.any_date and criteria.date_from is not None:
        end = (criteria.date_to or criteria.date_from) + timedelta(days=RELAX_EXTRA_DAYS)
        variants.append(("dates", criteria.model_copy(update={"date_to": end})))
    if criteria.days:
        variants.append(("days", criteria.model_copy(update={"days": ()})))
    if criteria.max_price is not None and not criteria.no_budget:
        wider = int(criteria.max_price * RELAX_PRICE_FACTOR)
        variants.append(("budget", criteria.model_copy(update={"max_price": wider})))
    return variants
