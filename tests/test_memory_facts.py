"""The memory model's output parser: set/remove operations on personal-memory keys.

``parse`` is the one place that knows the call shape (``parse_memory_operations(raw)``), so a signature change is fixed here.
"""

from __future__ import annotations

import json
from typing import Any

from booking_agent.domain.memory_facts import parse_memory_operations


def parse(raw: str) -> list[tuple[str, str, Any, bool | None]]:
    operations = parse_memory_operations(raw)
    return [(op.op, op.key, getattr(op, "value", None), op.stated if op.op == "set" else None) for op in operations]


def reply(*operations: dict[str, Any]) -> str:
    return json.dumps({"operations": list(operations)})


def test_a_set_and_a_remove_are_parsed_in_order() -> None:
    raw = reply(
        {"op": "set", "key": "earliest_time", "value": "10:00", "stated": True}, {"op": "remove", "key": "area"}
    )
    assert parse(raw) == [("set", "earliest_time", "10:00", True), ("remove", "area", None, None)]


def test_a_fenced_json_block_is_accepted() -> None:
    raw = "```json\n" + reply({"op": "remove", "key": "area"}) + "\n```\n"
    assert parse(raw) == [("remove", "area", None, None)]


def test_inferred_is_the_default_unless_stated_is_true() -> None:
    raw = reply(
        {"op": "set", "key": "earliest_time", "value": "10:00"},
        {"op": "set", "key": "max_budget", "value": 1500, "stated": "yes"},
    )
    assert [item[3] for item in parse(raw)] == [False, False]


def test_invalid_output_gives_no_operations() -> None:
    for raw in ("", "not json", "[]", '{"operations": "none"}', '{"operations": [1, "x", null]}'):
        assert parse(raw) == []


def test_unknown_ops_and_missing_keys_are_dropped() -> None:
    raw = reply(
        {"op": "wipe", "key": "area"},
        {"op": "set", "value": "x"},
        {"op": "remove"},
        {"op": "set", "key": "", "value": "x"},
    )
    assert parse(raw) == []


def test_removing_a_key_that_is_not_in_memory_is_kept() -> None:
    # "Forget X" must also clear X from the chat summary, even when the key is already gone from memory.
    assert parse(reply({"op": "remove", "key": "never_stored"})) == [("remove", "never_stored", None, None)]


def test_values_are_capped_and_the_number_of_keys_is_limited() -> None:
    long_value = "x" * 5000
    assert parse(reply({"op": "set", "key": "note", "value": long_value, "stated": True})) == []  # over 500 chars
    many = reply(*[{"op": "set", "key": f"k{i}", "value": i, "stated": True} for i in range(100)])
    assert len(parse(many)) < 100  # at most MAX_KEYS * 2 operations are read
