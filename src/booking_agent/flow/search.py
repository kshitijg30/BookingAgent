"""Running a provider search for some criteria and storing it as a Search snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from booking_agent.domain.entities import Criteria, ProviderOffer, Search
from booking_agent.domain.models import IST
from booking_agent.domain.planning import EmptyReason, SlotKey, build_offers, search_window
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.gateway.calls import ProviderCalls
from booking_agent.ports import Repository


@dataclass(frozen=True)
class SearchOutcome:
    """Offers for some criteria. ``provider_failed`` means the provider could not be reached."""

    offers: tuple[ProviderOffer, ...]
    reason: EmptyReason | None
    provider_failed: bool = False


def failed_slots(store: Repository, user_id: str, conversation_id: str) -> set[SlotKey]:
    """Slots of this conversation that failed or expired. They are never offered again."""
    keys: set[SlotKey] = set()
    for status in ("failed", "expired"):
        for quote in store.list_quotes_for_user(user_id, status=status, limit=200):
            if quote.conversation_id == conversation_id and quote.action != "cancel":
                keys.add((quote.provider_id, quote.start))
    return keys


def search_offers(calls: ProviderCalls, now: datetime, criteria: Criteria, excluded: set[SlotKey]) -> SearchOutcome:
    """Ask the provider and rank what fits the criteria."""
    today = now.astimezone(IST).date()
    window = search_window(criteria, today)
    if window is None or criteria.service is None or criteria.area is None:
        return SearchOutcome((), "no_slots_in_window")
    answer = calls.search(criteria.service, criteria.area, window[0], window[1])
    if not answer.ok or answer.value is None:
        return SearchOutcome((), None, provider_failed=True)
    offers, reason = build_offers(answer.value, criteria, window, now, excluded)
    return SearchOutcome(offers, reason)


def run_new_search(deps: Deps, turn: TurnContext, criteria: Criteria) -> SearchOutcome:
    """Search and store the result as the conversation's newest search."""
    excluded = failed_slots(deps.store, turn.user_id, turn.conversation_id)
    outcome = search_offers(turn.calls, turn.now, criteria, excluded)
    store_search(deps, turn, criteria, outcome.offers)
    return outcome


def store_search(deps: Deps, turn: TurnContext, criteria: Criteria, offers: tuple[ProviderOffer, ...]) -> Search:
    search = Search(
        id=f"s-{deps.new_id()}",
        conversation_id=turn.conversation_id,
        criteria=criteria,
        results=offers,
        created_at=turn.now,
    )
    turn.set_search(search)
    return search
