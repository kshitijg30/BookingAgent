"""Provider cancellations: a booking the provider cancelled gets a replacement search. Nothing is ever booked.

``recover`` runs at the start of every user turn for that user. ``scan_user`` is the background version used by
``Agent.scan_provider_cancellations``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from booking_agent.domain.entities import Booking, ChatMessage, Criteria, Search
from booking_agent.domain.models import IST
from booking_agent.flow.context import Deps, TurnContext, Write
from booking_agent.flow.search import search_offers
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.render import fmt_slot, offers_card, safe_name, service_label


@dataclass(frozen=True)
class Cancellation:
    """A booking the provider cancelled, with the replacement search and the message for the user."""

    booking: Booking
    search: Search
    text: str
    card: dict[str, Any] | None
    writes: tuple[Write, ...]


def _replacement_criteria(deps: Deps, booking: Booking, now: datetime) -> Criteria:
    quote = deps.store.get_quote(booking.quote_id)
    original = deps.store.get_search(quote.search_id) if quote is not None and quote.search_id else None
    base = original.criteria if original is not None else Criteria(service=booking.service, area=booking.area)
    update: dict[str, Any] = {
        "provider_id": None,
        "replaces_booking_id": booking.id,
        "reschedule_booking_id": None,
        "relaxed": [],
        "exclude_provider_ids": tuple(sorted({*base.exclude_provider_ids, booking.provider_id})),
    }
    last_day = base.date_to or base.date_from
    if base.date_from is None or (last_day is not None and last_day < now.astimezone(IST).date()):
        update |= {"date_from": None, "date_to": None, "any_date": True}
    if base.max_price is None:
        update["no_budget"] = True
    return base.model_copy(update=update)


def find_cancellations(deps: Deps, calls: ProviderCalls, user_id: str, now: datetime) -> list[Cancellation]:
    """Bookings of this user that the provider has cancelled, each with a replacement search (not booked)."""
    booked = deps.store.list_bookings_for_user(user_id, status="booked")
    if not booked:
        return []
    answer = calls.list_bookings(user_id)
    if not answer.ok or answer.value is None:
        return []
    status_by_id = {item.booking_id: item.status for item in answer.value}
    found: list[Cancellation] = []
    for booking in booked:
        if status_by_id.get(booking.provider_booking_id or "") == "cancelled_by_provider":
            found.append(_replacement(deps, calls, booking, now))
    return found


def _replacement(deps: Deps, calls: ProviderCalls, booking: Booking, now: datetime) -> Cancellation:
    criteria = _replacement_criteria(deps, booking, now)
    offers = search_offers(calls, now, criteria, set()).offers
    search = Search(
        id=f"s-{deps.new_id()}",
        conversation_id=booking.conversation_id,
        criteria=criteria,
        results=offers,
        created_at=now,
    )
    cancelled = booking.model_copy(update={"status": "cancelled_by_provider", "updated_at": now})
    text = (
        f"Heads up: {safe_name(booking.provider_name)} cancelled your {service_label(booking.service)} booking for "
        f"{fmt_slot(booking.start, booking.end)}. "
    )
    text += (
        "Here are other options. Nothing will be booked until you confirm."
        if offers
        else "I couldn't find a replacement yet. Tell me what you'd like to change."
    )
    calls.tracer.record("provider_cancellation_detected", now=now, booking_id=booking.id)
    calls.tracer.record(
        "booking_updated", now=now, booking_id=booking.id, **{"from": booking.status, "to": "cancelled_by_provider"}
    )
    writes: tuple[Write, ...] = (lambda store: store.save_booking(cancelled), lambda store: store.add_search(search))
    return Cancellation(booking, search, text, offers_card(search.id) if offers else None, writes)


def recover(deps: Deps, turn: TurnContext) -> None:
    """Handle provider cancellations for this user before the turn's own operation runs."""
    for item in find_cancellations(deps, turn.calls, turn.user_id, turn.now):
        if item.booking.conversation_id == turn.conversation_id:
            for change in item.writes:
                turn.write(change)
            turn.search = item.search
            turn.say(item.text, item.card)
        else:
            notify_conversation(deps, item, turn.now)


def notify_conversation(deps: Deps, item: Cancellation, now: datetime) -> None:
    """Append the notice to the conversation that made the booking, in its own transaction."""
    with deps.conversation_locks.get(item.booking.conversation_id), deps.store.tx():
        conversation = deps.store.get_conversation(item.booking.conversation_id)
        for change in item.writes:
            change(deps.store)
        if conversation is not None:
            message = ChatMessage(role="agent", text=item.text, card=item.card, at=now)
            updated = conversation.model_copy(update={"messages": (*conversation.messages, message), "updated_at": now})
            deps.store.save_conversation(updated, expected_version=conversation.version)
