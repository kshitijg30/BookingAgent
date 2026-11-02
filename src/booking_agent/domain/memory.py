"""Personal memory: a JSON object kept in ``user_profiles.memory``, one entry per key.

    {"area": {"value": "Koramangala", "source": "stated", "confirmed": true},
     "earliest_time": {"value": "10:00", "source": "inferred", "confirmed": false, "asked": true}}

Pure functions only. Every function returns a new dict and never changes its input.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

MAX_KEYS = 30
MAX_KEY_CHARS = 40
MAX_VALUE_CHARS = 500
HISTORY_TURNS = 8
_KEY = re.compile(r"[a-z][a-z0-9_]*")

Memory = dict[str, Any]


@dataclass(frozen=True)
class MemoryOperation:
    """One validated change. ``value`` and ``stated`` are used by ``set`` only."""

    op: Literal["set", "remove"]
    key: str
    value: Any = None
    stated: bool = False


def valid_key(key: object) -> bool:
    return isinstance(key, str) and len(key) <= MAX_KEY_CHARS and _KEY.fullmatch(key) is not None


def valid_value(value: object) -> bool:
    """A JSON value of at most 500 characters. ``None`` is not a value (use remove)."""
    if value is None:
        return False
    try:
        return len(json.dumps(value, ensure_ascii=False)) <= MAX_VALUE_CHARS
    except (TypeError, ValueError):
        return False


def set_key(memory: Memory, key: str, value: Any, *, stated: bool) -> Memory:
    """Stated: source stated, confirmed. Inferred: source inferred, not confirmed, not asked yet; a key that is
    already confirmed is not overwritten by an inference. A new key beyond the cap is dropped."""
    current = memory.get(key)
    if current is None and len(memory) >= MAX_KEYS:
        return memory
    if not stated and current is not None and current.get("confirmed") is True:
        return memory
    entry = {"value": copy.deepcopy(value), "source": "stated", "confirmed": True}
    if not stated:
        entry = {"value": copy.deepcopy(value), "source": "inferred", "confirmed": False, "asked": False}
    return {**memory, key: entry}


def remove_key(memory: Memory, key: str) -> Memory:
    return {k: v for k, v in memory.items() if k != key}


def confirm_key(memory: Memory, key: str) -> Memory:
    """The user said yes: confirmed, source kept."""
    current = memory.get(key)
    if current is None:
        return memory
    return {**memory, key: {**current, "confirmed": True}}


def mark_asked(memory: Memory, key: str) -> Memory:
    current = memory.get(key)
    return memory if current is None else {**memory, key: {**current, "asked": True}}


def apply_operations(memory: Memory, operations: Sequence[MemoryOperation]) -> Memory:
    for operation in operations:
        if operation.op == "remove":
            memory = remove_key(memory, operation.key)
        else:
            memory = set_key(memory, operation.key, operation.value, stated=operation.stated)
    return memory


def confirmed_memory(memory: Memory) -> Memory:
    """What the chat model sees: confirmed keys only (value and source)."""
    return {
        key: {"value": entry["value"], "source": entry["source"]}
        for key, entry in memory.items()
        if entry.get("confirmed") is True
    }


def key_to_ask(memory: Memory) -> str | None:
    """One unconfirmed key that has not been asked about yet."""
    return next((key for key, entry in memory.items() if not entry.get("confirmed") and not entry.get("asked")), None)


def public_memory(memory: Memory) -> Memory:
    """What the API returns: value, source and confirmed per key."""
    return {
        key: {"value": entry["value"], "source": entry["source"], "confirmed": bool(entry.get("confirmed"))}
        for key, entry in memory.items()
    }


def window_start(messages: Sequence[Any], turns: int = HISTORY_TURNS) -> int:
    """Index of the first message of the last ``turns`` turns (a turn starts at a user message)."""
    starts = [i for i, message in enumerate(messages) if message.role == "user"]
    return starts[-turns] if len(starts) > turns else 0
