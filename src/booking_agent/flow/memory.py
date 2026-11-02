"""Changing a user's memory and using it: shared by the model's tools, the memory_update job and the API.

Memory lives in ``user_profiles.memory``. Callers hold the per-user lock; the change is one profile write.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, time
from typing import Any

from booking_agent.config import ServiceCatalog
from booking_agent.domain.entities import Criteria, TimeRange
from booking_agent.domain.memory import Memory
from booking_agent.domain.memory_facts import join_forgotten, split_forgotten
from booking_agent.domain.planning import part_range
from booking_agent.ports import Repository

EVENING_END = time(22)


def change_memory(store: Repository, user_id: str, change: Callable[[Memory], Memory], now: datetime) -> Memory:
    """Read the profile, apply ``change`` to its memory and save it if anything changed."""
    profile = store.get_profile(user_id)
    updated = change(profile.memory)
    if updated != profile.memory:
        store.save_profile(profile.model_copy(update={"memory": updated}), now=now)
    return updated


def _confirmed_value(memory: Memory, key: str) -> Any:
    entry = memory.get(key)
    return entry["value"] if entry is not None and entry.get("confirmed") is True else None


def _preferred_times(memory: Memory) -> tuple[TimeRange, ...]:
    earliest = _confirmed_value(memory, "earliest_time")
    if isinstance(earliest, str):
        try:
            start = time.fromisoformat(earliest)
        except ValueError:
            start = None
        if start is not None and start < EVENING_END:
            return (TimeRange(from_time=start, to_time=EVENING_END),)
    part = _confirmed_value(memory, "preferred_time")
    named = part_range(part) if isinstance(part, str) else None
    return (named,) if named else ()


def avoided_providers(memory: Memory) -> list[str]:
    """Provider names the user never wants: the avoid_providers key, or the older shape
    preferred_providers = {"disliked": [...]} that the memory model once produced."""
    names = _confirmed_value(memory, "avoid_providers")
    if not isinstance(names, list):
        legacy = _confirmed_value(memory, "preferred_providers")
        names = legacy.get("disliked") if isinstance(legacy, dict) else None
    return [name for name in names if isinstance(name, str) and name.strip()] if isinstance(names, list) else []


def prefill_criteria(criteria: Criteria, memory: Memory, catalog: ServiceCatalog, *, fill_times: bool) -> Criteria:
    """Fill missing area, budget, times and preferred provider from CONFIRMED memory keys.

    Avoided providers are always added, whatever the model passed.
    """
    update: dict[str, Any] = {}
    area = _confirmed_value(memory, "area")
    if criteria.area is None and isinstance(area, str) and area in catalog.areas:
        update["area"] = area
    budget = _confirmed_value(memory, "max_budget")
    if criteria.max_price is None and not criteria.no_budget and isinstance(budget, int | float) and budget > 0:
        update["max_price"] = int(budget)
    if fill_times and not criteria.times:
        times = _preferred_times(memory)
        if times:
            update["times"] = times
    providers = _confirmed_value(memory, "preferred_providers")
    if criteria.provider_id is None and criteria.service and isinstance(providers, dict):
        preferred = providers.get(criteria.service)
        if isinstance(preferred, dict):
            preferred = preferred.get("provider")
        if isinstance(preferred, str) and preferred:
            update["provider_id"] = preferred  # an id or a name; planning matches either
    avoided = avoided_providers(memory)
    if avoided:
        update["exclude_provider_names"] = tuple(dict.fromkeys([*criteria.exclude_provider_names, *avoided]))
    return criteria.model_copy(update=update) if update else criteria


def forget_in_chat(store: Repository, conversation_id: str, keys: list[str]) -> str | None:
    """List ``keys`` as forgotten in the conversation's summary (its trailing ``Forgotten:`` line).

    Returns the new summary, or None if there is no such conversation. The summary text is rewritten later by
    the memory_update job.
    """
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return None
    text, forgotten = split_forgotten(conversation.summary)
    summary = join_forgotten(text, [*forgotten, *keys])
    if summary != conversation.summary:
        store.save_conversation(
            conversation.model_copy(update={"summary": summary}), expected_version=conversation.version
        )
    return summary


def drop_forgotten(criteria: Criteria, forgotten: list[str], text: str) -> Criteria:
    """Remove a criteria field whose memory key was forgotten in this chat, unless ``text`` (the newest user
    message) contains the value again. area -> area, max_budget -> max_price, earliest_time -> times,
    preferred_providers -> provider_id, avoid_providers -> exclude_provider_names."""
    lowered, plain = text.casefold(), text.replace(",", "")
    update: dict[str, Any] = {}
    if "area" in forgotten and criteria.area and criteria.area.casefold() not in lowered:
        update["area"] = None
    if "max_budget" in forgotten and criteria.max_price is not None and str(criteria.max_price) not in plain:
        update |= {"max_price": None, "no_budget": False}
    if "earliest_time" in forgotten and criteria.times:
        start = criteria.times[0].from_time
        if not re.search(rf"\b0?{start.hour}(:{start.minute:02d})?\b", lowered):
            update["times"] = ()
    if ("avoid_providers" in forgotten or "preferred_providers" in forgotten) and criteria.exclude_provider_names:
        update["exclude_provider_names"] = ()
    if "preferred_providers" in forgotten and criteria.provider_id and criteria.provider_id.casefold() not in lowered:
        update["provider_id"] = None
    return criteria.model_copy(update=update) if update else criteria
