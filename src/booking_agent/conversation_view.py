"""The conversation as the browser reads it: saved messages with every card filled in from current rows.

A card stored in a conversation holds ids only. When the UI loads the thread, each card is resolved here
so that it shows the current status of its quote or booking and the offers of its search.
"""

from __future__ import annotations

from typing import Any, Protocol

from booking_agent.domain.entities import ChatMessage, Conversation, ProviderOffer
from booking_agent.ports import BookingStore, QuoteStore, SearchStore


class CardSource(SearchStore, QuoteStore, BookingStore, Protocol):
    """The three stores a card can point into."""


def conversation_view(conversation: Conversation, source: CardSource) -> dict[str, Any]:
    """``{messages: [{role, text, card, at}]}`` with each card resolved."""
    return {"messages": [message_view(message, source) for message in conversation.messages]}


def message_view(message: ChatMessage, source: CardSource) -> dict[str, Any]:
    card = resolve_card(message.card, source) if message.card else None
    return {"role": message.role, "text": message.text, "card": card, "at": message.at.isoformat()}


def resolve_card(card: dict[str, Any], source: CardSource) -> dict[str, Any]:
    """Add current details to a stored card. Unknown card types and missing rows are returned as stored."""
    resolvers = {"offers": _offers, "quote": _quote, "confirmation": _confirmation}
    resolver = resolvers.get(str(card.get("type")))
    return resolver(dict(card), source) if resolver is not None else dict(card)


def _rank(offer: ProviderOffer) -> tuple[float, int]:
    """Best first: higher rating, then lower price."""
    return (-(offer.rating or 0.0), offer.price)


def _offers(card: dict[str, Any], source: CardSource) -> dict[str, Any]:
    search = source.get_search(str(card.get("search_id")))
    offers = sorted(search.results, key=_rank) if search is not None else []
    card["providers"] = [offer.model_dump(mode="json") for offer in offers]
    return card


def _quote(card: dict[str, Any], source: CardSource) -> dict[str, Any]:
    quote = source.get_quote(str(card.get("quote_id")))
    if quote is None:
        return card
    current = quote.model_dump(mode="json")
    for name in ("action", "provider_name", "service", "start", "end", "price", "status", "version"):
        card[name] = current[name]
    return card


def _confirmation(card: dict[str, Any], source: CardSource) -> dict[str, Any]:
    booking = source.get_booking(str(card.get("booking_id")))
    if booking is None:
        return card
    current = booking.model_dump(mode="json")
    for name in ("service", "provider_name", "start", "end", "price", "status"):
        card[name] = current[name]
    quote = source.get_quote(booking.quote_id)
    if quote is not None:
        card["action"] = quote.action
    return card
