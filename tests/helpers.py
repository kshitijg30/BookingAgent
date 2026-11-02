"""Helpers shared by the scenario tests: scripted tool calls, and reading replies and traces."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from booking_agent.agent import TurnResult
from booking_agent.domain.entities import Booking, ProviderOffer, Quote
from booking_agent.model import ChatResponse, call, say
from booking_agent.provider.scenario import Scenario
from tests.conftest import World, quiet_history

THIS_WEEK = {"date_from": "2026-11-02", "date_to": "2026-11-08"}  # START is Monday 2 Nov 2026, 09:00 IST
BOOK_MSG = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500 in Koramangala"


def scenario_with(**overrides: Any) -> Scenario:
    return Scenario(seed=7, seed_bookings=quiet_history(), **overrides)


def find(**overrides: Any) -> ChatResponse:
    """The model calling find_and_propose_booking. Pass ``name=None`` to leave an argument out."""
    args = {"service": "plumber", "area": "Koramangala", **THIS_WEEK, "times": ["morning"], "max_price_rupees": 1500}
    return call("find_and_propose_booking", **{k: v for k, v in (args | overrides).items() if v is not None})


def ask_booking(world: World, msg: str = BOOK_MSG, **overrides: Any) -> TurnResult:
    """The user asks; the scripted model searches. Leaves an offers card on screen."""
    world.model.then(find(**overrides), say("Here are some options. Tap a slot."))
    return world.say(msg)


def offers(world: World) -> tuple[ProviderOffer, ...]:
    search = world.search()
    assert search is not None, "no search yet"
    return search.results


def first_slot(world: World, index: int = 0, conversation: str | None = None) -> tuple[str, datetime]:
    """(provider_id, start) of the first slot of the index-th offered provider."""
    search = world.search(conversation)
    assert search is not None, "no search yet"
    offer = search.results[index]
    return offer.provider_id, offer.slots[0].start


def pick_first(world: World, index: int = 0) -> TurnResult:
    provider_id, start = first_slot(world, index)
    return world.pick(provider_id, start)


def quote_on_screen(world: World) -> Quote:
    quote = world.current_quote()
    assert quote is not None, "no quote on screen"
    return quote


def book_to_confirmation(world: World, index: int = 0, **overrides: Any) -> Quote:
    """Ask, tap the first slot of the index-th provider, and return the quote that is now on screen."""
    ask_booking(world, **overrides)
    pick_first(world, index)
    return quote_on_screen(world)


def cards(reply: TurnResult) -> list[str]:
    return [str(m.card.get("type")) for m in reply.messages if m.card]


def card(reply: TurnResult, kind: str) -> dict[str, Any]:
    for message in reply.messages:
        if message.card and message.card.get("type") == kind:
            return message.card
    raise AssertionError(f"no {kind} card in {cards(reply)}")


def tools(world: World, name: str) -> list[dict[str, Any]]:
    return world.tool_calls(name)


def book_done(world: World, index: int = 0, **overrides: Any) -> Booking:
    """Run a whole booking (ask, pick, confirm, worker) and return our booking row."""
    book_to_confirmation(world, index, **overrides)
    world.confirm()
    world.run_jobs()
    (booking,) = world.bookings()
    return booking


def set_memory(
    world: World, key: str, value: Any, *, source: str = "stated", confirmed: bool = True, asked: bool = False
) -> None:
    """Put a key into the user's personal memory (user_profiles.memory), as an earlier conversation would have."""
    profile = world.store.get_profile(world.user)
    entry: dict[str, Any] = {"value": value, "source": source, "confirmed": confirmed}
    if asked:
        entry["asked"] = True
    world.store.save_profile(
        profile.model_copy(update={"memory": {**profile.memory, key: entry}}), now=world.clock.now()
    )


def memory(world: World, user: str | None = None) -> dict[str, Any]:
    """The user's personal memory: {key: {value, source, confirmed, asked?}}."""
    return dict(world.store.get_profile(user or world.user).memory)


def context_sent(world: World) -> str:
    """Everything the chat model got on its latest call except the typed history (all system messages)."""
    return "\n".join(str(m["content"]) for m in world.model.calls[-1] if m["role"] == "system")
