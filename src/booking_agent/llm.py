"""Compatibility imports. SDK integrations are loaded only when explicitly requested."""

from typing import TYPE_CHECKING, Any

from booking_agent.model import (
    ChatError,
    ChatModel,
    ChatResponse,
    ScriptedChatModel,
    Step,
    ToolCall,
    call,
    say,
    tool_messages,
)

__all__ = [
    "ChatError",
    "ChatModel",
    "ChatResponse",
    "OpenAIChatModel",
    "ScriptedChatModel",
    "Step",
    "ToolCall",
    "call",
    "say",
    "tool_messages",
]


if TYPE_CHECKING:
    from booking_agent.adapters.openai import OpenAIChatModel


def __getattr__(name: str) -> Any:
    if name == "OpenAIChatModel":
        from booking_agent.adapters.openai import OpenAIChatModel

        return OpenAIChatModel
    raise AttributeError(name)
