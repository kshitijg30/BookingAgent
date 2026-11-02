"""Chat model with tool calling: the OpenAI client for real use, a scripted model for tests.

The model talks to the user and calls tools. It never books, cancels or reschedules by itself:
those happen in code only after the user confirms the card on screen.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class ChatError(Exception):
    """The model could not be reached or returned something unusable."""


class ChatModel(Protocol):
    model_name: str

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse: ...


Step = ChatResponse | Callable[[list[dict[str, Any]]], ChatResponse] | ChatError


@dataclass
class ScriptedChatModel:
    """For tests: replies in order, or computed from the conversation so far."""

    steps: list[Step] = field(default_factory=list)
    model_name: str = "scripted"
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        self.calls.append(list(messages))
        if not self.steps:
            return ChatResponse(text="Okay.", model=self.model_name)
        step = self.steps.pop(0)
        if isinstance(step, ChatError):
            raise step
        return step(messages) if callable(step) else step

    def then(self, *steps: Step) -> ScriptedChatModel:
        self.steps.extend(steps)
        return self


def call(name: str, **arguments: Any) -> ChatResponse:
    """A scripted response that calls one tool."""
    return ChatResponse(
        text=None,
        tool_calls=(ToolCall(id=f"call_{name}_{len(arguments)}", name=name, arguments=arguments),),
        model="scripted",
    )


def say(text: str) -> ChatResponse:
    return ChatResponse(text=text, model="scripted")


def tool_messages(calls: Sequence[ToolCall]) -> dict[str, Any]:
    """The assistant message that carries tool calls, in OpenAI format."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ],
    }
