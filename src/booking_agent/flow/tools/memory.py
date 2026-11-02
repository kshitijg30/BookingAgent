"""Memory tools: remember what the user stated, forget a key, and record the answer to a confirmation question."""

from __future__ import annotations

from typing import Any

from booking_agent.domain.memory import confirm_key, remove_key, set_key, valid_key, valid_value
from booking_agent.domain.memory_facts import join_forgotten, split_forgotten
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.memory import change_memory, forget_in_chat
from booking_agent.flow.tools.base import Tool, ToolOutput, fixed_parameters
from booking_agent.ports import Repository


def _remember(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key, value = args.get("key"), args.get("value")
    if not valid_key(key) or not valid_value(value):
        return {"error": "invalid key or value"}
    user_id, now = turn.user_id, turn.now
    turn.write(lambda store: change_memory(store, user_id, lambda m: set_key(m, str(key), value, stated=True), now))
    return {"key": key, "stated": True, "confirmed": True}


def _forget(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key = str(args.get("key"))
    user_id, now = turn.user_id, turn.now
    conversation_id = turn.conversation_id

    def write(store: Repository) -> None:
        change_memory(store, user_id, lambda m: remove_key(m, key), now)
        forget_in_chat(store, conversation_id, [key])

    turn.write(write)
    text, forgotten = split_forgotten(turn.conversation.summary)
    summary = join_forgotten(text, [*forgotten, key])
    turn.conversation = turn.conversation.model_copy(update={"summary": summary})
    return {"removed": key}


def _confirm_memory(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key, answer = str(args["key"]), args["answer"]
    if key not in deps.store.get_profile(turn.user_id).memory:
        return {"error": "no such memory key"}
    user_id, now = turn.user_id, turn.now
    change = confirm_key if answer == "yes" else remove_key
    turn.write(lambda store: change_memory(store, user_id, lambda m: change(m, key), now))
    return {"key": key, "recorded": answer}


_VALUE = {"type": ["string", "number", "boolean", "object", "array"]}
REMEMBER = Tool("remember", fixed_parameters({"key": {"type": "string"}, "value": _VALUE}, ["key", "value"]), _remember)
FORGET = Tool("forget", fixed_parameters({"key": {"type": "string"}}, ["key"]), _forget)
CONFIRM_MEMORY = Tool(
    "confirm_memory",
    fixed_parameters(
        {"key": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no"]}}, ["key", "answer"]
    ),
    _confirm_memory,
)
MEMORY_TOOLS = (REMEMBER, FORGET, CONFIRM_MEMORY)
