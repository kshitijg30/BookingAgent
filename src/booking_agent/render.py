"""Reply texts and card shapes. Every price, slot, status and booking id a user reads comes from code here.

A card carries ids only (the HTTP layer adds the current status), except the quote card's ``reviews``, which
are display strings escaped for the page and never sent to the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from booking_agent.catalog import ServiceCatalog
from booking_agent.domain.entities import Criteria, Quote
from booking_agent.domain.models import IST
from booking_agent.domain.money import fmt_rupees
from booking_agent.domain.untrusted import Untrusted

NOTICE_CARD: dict[str, Any] = {"type": "notice"}
HIDDEN_REVIEW = "Review hidden: it contained instructions aimed at the assistant."
MAX_REVIEWS_SHOWN = 3

FIELD_PROMPTS = {
    "service": "what you need done (plumber, electrician, AC servicing, carpenter or cleaning)",
    "area": "your area (for example Koramangala or HSR Layout)",
    "dates": "when (for example 'this week, mornings' or 'Saturday')",
    "budget": "your budget (for example 'under ₹1,500', or 'no limit')",
}
FIELD_LABELS = {"service": "Service", "area": "Area", "dates": "When", "budget": "Budget"}
FIXED_CHOICES: dict[str, list[str]] = {
    "dates": ["Today", "Tomorrow", "This week", "This weekend", "Next week"],
    "budget": ["Under ₹1,000", "Under ₹1,500", "Under ₹2,500", "No limit"],
}
SERVICE_NAMES = {
    "plumber": "plumbing",
    "electrician": "electrical",
    "ac_service": "AC servicing",
    "carpenter": "carpentry",
    "cleaning": "cleaning",
}
BLOCK_MESSAGES = {
    "unsupported_service": "I can only book plumbers, electricians, AC servicing, carpenters and cleaning.",
    "unsupported_area": "I don't cover that area yet. Try one of the areas shown.",
    "no_providers": "I couldn't find any providers for that service in your area.",
    "over_budget": "Every available provider is at or above your budget. Raise the budget or change the dates?",
    "no_slots_in_window": "No provider has a free slot that fits those times. Try other dates or times?",
    "preferred_provider_unavailable": (
        "That provider has no free slot that fits. Want me to look at other providers, or try different times?"
    ),
    "no_previous_booking": "I couldn't find an earlier booking to take the provider from.",
}


def fmt_slot(start: datetime, end: datetime) -> str:
    local_start, local_end = start.astimezone(IST), end.astimezone(IST)
    return f"{local_start.strftime('%a %d %b')}, {local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"


def day_time(start: datetime) -> str:
    local = start.astimezone(IST)
    return f"{local.strftime('%a %d %b')} {local.strftime('%H:%M')}"


def safe_name(raw: str) -> str:
    """A provider name (provider text) escaped for display."""
    return Untrusted(raw).escaped_for_display()


def service_label(name: str | None) -> str:
    return SERVICE_NAMES.get(name or "", name or "service")


def field_choices(field_name: str, catalog: ServiceCatalog) -> list[str] | None:
    """Tap-to-answer choices for a field. Service and area come from the catalog."""
    if field_name == "service":
        return [catalog.labels.get(service, service) for service in catalog.services]
    if field_name == "area":
        return list(catalog.areas)
    return FIXED_CHOICES.get(field_name)


def question(fields: Sequence[str], catalog: ServiceCatalog) -> tuple[str, dict[str, Any]]:
    """Text asking for the missing details, and a question card with choices for the fields that have them."""
    parts = [FIELD_PROMPTS.get(name, name) for name in fields]
    body = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    groups = []
    for name in fields:
        choices = field_choices(name, catalog)
        if choices is not None:
            groups.append({"label": FIELD_LABELS.get(name, name), "choices": choices})
    return f"To find the right person, tell me {body}.", {"type": "question", "choices": groups}


def offers_card(search_id: str) -> dict[str, Any]:
    return {"type": "offers", "search_id": search_id}


def offers_text(count: int) -> str:
    return f"I found {count} provider{'s' if count != 1 else ''}. Pick a slot below."


def quote_card(quote: Quote, reviews: Sequence[str]) -> dict[str, Any]:
    return {"type": "quote", "quote_id": quote.id, "version": quote.version, "reviews": list(reviews)}


def confirmation_card(booking_id: str) -> dict[str, Any]:
    return {"type": "confirmation", "booking_id": booking_id}


def review_lines(reviews: Sequence[Untrusted]) -> list[str]:
    """Provider reviews as display strings: escaped, and hidden if they address the assistant."""
    shown = [HIDDEN_REVIEW if review.looks_like_injection() else review.escaped_for_display() for review in reviews]
    return shown[:MAX_REVIEWS_SHOWN]


def price_text(quote: Quote) -> str:
    price = fmt_rupees(quote.price)
    if quote.action == "reschedule":
        return price + " (current booking price, assumed unchanged)"
    if quote.action == "cancel":
        return price + " (no cancellation fee information from the provider)"
    return price


def quote_text(quote: Quote, *, note: str | None = None) -> str:
    """The text of the quote card: exactly what the user confirms."""
    verb = {"book": "Book", "cancel": "Cancel", "reschedule": "Move"}[quote.action]
    slot_label = "New slot" if quote.action == "reschedule" else "Slot"
    lines = [note] if note else []
    lines += [
        f"Please confirm. {verb} this {service_label(quote.service)} visit?",
        f"  Provider: {safe_name(quote.provider_name)}",
        f"  Area: {quote.area}",
        f"  {slot_label}: {fmt_slot(quote.start, quote.end)}",
        f"  Price: {price_text(quote)}",
    ]
    if quote.booking_id and quote.action != "book":
        lines.append(f"  Booking: {quote.booking_id}")
    lines.append("Tap Confirm to go ahead, or pick another option.")
    return "\n".join(lines)


def confirmation_text(quote: Quote, booking_id: str) -> str:
    """The message for a write the provider has verifiably done."""
    provider, when = safe_name(quote.provider_name), fmt_slot(quote.start, quote.end)
    if quote.action == "book":
        return (
            f"Booked. {provider} will come for {service_label(quote.service)} on {when} "
            f"for {fmt_rupees(quote.price)}. Booking reference {booking_id}."
        )
    if quote.action == "cancel":
        return f"Cancelled booking {booking_id} ({provider}, {when})."
    return f"Moved booking {booking_id} to {when} with {provider}."


def blocked_text(reason: str, extra: str | None = None) -> str:
    message = BLOCK_MESSAGES.get(reason, "I can't continue with that request.")
    return f"{message} {extra}".strip()


def describe_criteria(criteria: Criteria) -> str:
    """The constraints as a short phrase, for messages such as "Nothing matches ..."."""
    parts: list[str] = []
    if criteria.date_from is not None and not criteria.any_date:
        start = criteria.date_from.strftime("%a %d %b")
        end = (criteria.date_to or criteria.date_from).strftime("%a %d %b")
        parts.append(start if start == end else f"{start} to {end}")
    if criteria.days:
        parts.append("on " + ", ".join(criteria.days))
    if criteria.times:
        parts.append(", ".join(f"{r.from_time:%H:%M}-{r.to_time:%H:%M}" for r in criteria.times))
    if criteria.max_price is not None:
        parts.append(f"under {fmt_rupees(criteria.max_price)}")
    return ", ".join(parts) or "your request"
