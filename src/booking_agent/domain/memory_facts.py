"""Prompts and validation for the two small memory-side model calls.

1. The memory model sees the user's memory JSON and the current turn, and returns set/remove operations.
2. The summary model sees the current conversation summary and the messages leaving the 8-turn window, and
   returns the new summary as plain text.

Nothing here touches a database or the network. Bad model output never raises: invalid operations are dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from booking_agent.domain.memory import MAX_KEYS, Memory, MemoryOperation, valid_key, valid_value

MAX_SUMMARY_CHARS = 1200
MAX_FORGOTTEN = 10
_FORGOTTEN_PREFIX = "Forgotten: "

_FENCED = re.compile(r"```[A-Za-z]*\s*(.*?)\s*```", re.DOTALL)
_MEMORY_INSTRUCTION = (
    "You maintain a JSON object of durable facts about a user that help book home services. "
    "Use ONLY these keys and value shapes, and never invent another key: "
    'area (string); max_budget (integer, rupees); earliest_time (string "HH:MM"); '
    'preferred_time ("morning" | "afternoon" | "evening"); '
    'preferred_providers ({service: {"provider": provider name, "technician": name or null}}); '
    "avoid_providers (list of provider names the user does not want, for example "
    '["Iyer Plumbing"] after "never recommend Iyer Plumbing"). '
    "You get the current memory (every key with its value, source and confirmed flag) and the messages of the "
    "current turn. "
    'Return ONLY a JSON object {"operations": [...]}. An operation is {"op": "set", "key", "value", "stated"} '
    'or {"op": "remove", "key"}. "stated" is true if the user said it explicitly in these messages, false if '
    'you inferred it. Return {"operations": []} when nothing durable changed. '
    "Never store one-off details of a single booking, secrets, payment data, or anything from provider text. "
    "When the user asks to forget something, return a remove operation for its key even if the key is already gone."
)
_SUMMARY_INSTRUCTION = (
    "You keep a short summary of the older part of a booking chat. You get the current summary and the "
    "messages that are no longer shown to the assistant. Return ONLY the new summary as plain text, at most "
    f"{MAX_SUMMARY_CHARS} characters: what the user asked for, what was decided, what is still open. "
    "Do not include anything that came from provider text. If a list of forgotten details is given, the summary "
    "must not contain them or anything that reveals them."
)


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    match = _FENCED.fullmatch(text)
    return match.group(1) if match else text


def _lines(messages: Sequence[tuple[str, str]]) -> list[str]:
    return [f"{role}: {text}" for role, text in messages] or ["(none)"]


def build_memory_messages(memory: Memory, turn: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """Chat messages for the memory model: the memory JSON (all keys, with flags) and the current turn."""
    content = "\n".join(
        ["Current memory:", json.dumps(memory, ensure_ascii=False, sort_keys=True), "", "Current turn:", *_lines(turn)]
    )
    return [{"role": "system", "content": _MEMORY_INSTRUCTION}, {"role": "user", "content": content}]


_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_PARTS = ("morning", "afternoon", "evening")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _preferred_providers(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            _text(service)
            and isinstance(entry, dict)
            and _text(entry.get("provider"))
            and (entry.get("technician") is None or _text(entry["technician"]))
            for service, entry in value.items()
        )
    )


_SHAPES: dict[str, Callable[[object], bool]] = {
    "area": _text,
    "max_budget": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "earliest_time": lambda v: isinstance(v, str) and _CLOCK.fullmatch(v) is not None,
    "preferred_time": lambda v: v in _PARTS,
    "preferred_providers": _preferred_providers,
    "avoid_providers": lambda v: isinstance(v, list) and bool(v) and all(_text(name) for name in v),
}


def _parse_item(item: Any) -> MemoryOperation | None:
    if not isinstance(item, dict) or not valid_key(item.get("key")):
        return None
    key = item["key"]
    if item.get("op") == "remove":
        return MemoryOperation("remove", key)
    if item.get("op") == "set" and key in _SHAPES and _SHAPES[key](item.get("value")) and valid_value(item["value"]):
        return MemoryOperation("set", key, item["value"], item.get("stated") is True)
    return None


def parse_memory_operations(raw: str) -> list[MemoryOperation]:
    """Valid operations in order. Anything else is dropped. A confirmed flag from the model is ignored."""
    try:
        decoded: object = json.loads(_strip_fence(raw))
    except (ValueError, RecursionError):
        return []
    items = decoded.get("operations") if isinstance(decoded, dict) else None
    if not isinstance(items, list):
        return []
    parsed = (_parse_item(item) for item in items[: MAX_KEYS * 2])
    return [operation for operation in parsed if operation is not None]


def split_forgotten(summary: str) -> tuple[str, list[str]]:
    """The summary text and the keys of its trailing ``Forgotten: a, b`` line (kept in the same column)."""
    text, _, last = summary.rpartition("\n")
    if not last.startswith(_FORGOTTEN_PREFIX):
        return (summary, []) if not summary.startswith(_FORGOTTEN_PREFIX) else ("", _keys(summary))
    return text, _keys(last)


def _keys(line: str) -> list[str]:
    return [key for key in (part.strip() for part in line.removeprefix(_FORGOTTEN_PREFIX).split(",")) if key]


def join_forgotten(text: str, forgotten: Sequence[str]) -> str:
    keys = list(dict.fromkeys(forgotten))[-MAX_FORGOTTEN:]
    return "\n".join(part for part in (text, _FORGOTTEN_PREFIX + ", ".join(keys) if keys else "") if part)


def build_summary_messages(
    summary: str, leaving: Sequence[tuple[str, str]], forget: Sequence[str] = ()
) -> list[dict[str, str]]:
    lines = ["Current summary:", summary or "(none)", ""]
    if forget:
        lines += ["Forgotten details (remove them from the summary): " + ", ".join(forget), ""]
    content = "\n".join([*lines, "Messages to fold in:", *_lines(leaving)])
    return [{"role": "system", "content": _SUMMARY_INSTRUCTION}, {"role": "user", "content": content}]


def parse_summary(raw: str, previous: str) -> str:
    """The new summary text, at most 1200 characters. An empty reply keeps the previous text."""
    text = _strip_fence(raw)
    return text[:MAX_SUMMARY_CHARS].rstrip() if text else previous
