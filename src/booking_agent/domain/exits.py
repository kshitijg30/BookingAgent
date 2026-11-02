"""Every run ends in exactly one ExitReason, and each has a message."""

from __future__ import annotations

from typing import Literal, assert_never

from booking_agent.domain.entities import Frozen

ExitKind = Literal[
    "completed",
    "needs_input",
    "blocked",
    "limit",
    "llm_unavailable",
    "tool_failed",
    "unknown_outcome",
    "queued",
    "internal_error",
    "rate_limited",
    "quarantined",
]
LimitKind = Literal["turns", "tool_calls", "wall_clock", "asks", "model_calls", "session_turns"]


class ExitReason(Frozen):
    kind: ExitKind
    detail: str | None = None
    limit: LimitKind | None = None


def exit_message(reason: ExitReason) -> str | None:
    """Message for exits that need one beyond the reply blocks already rendered."""
    match reason.kind:
        case "completed" | "needs_input" | "blocked" | "queued":
            return None
        case "limit":
            what = (reason.limit or "turn").replace("_", " ")
            return (
                f"I stopped because the {what} limit for this request was reached. "
                "Nothing was booked or changed in this step. Tell me what you'd like to do next."
            )
        case "llm_unavailable":
            return "I couldn't understand that just now. Nothing was changed. Please try rephrasing."
        case "tool_failed":
            return (
                f"The provider service ({reason.detail}) kept failing, so I stopped. "
                "Nothing was booked or changed. Please try again in a bit."
            )
        case "unknown_outcome":
            return (
                f"I sent the {reason.detail or 'request'} but couldn't confirm the result yet. "
                "I'll check again the next time you message. Please don't retry it yourself."
            )
        case "internal_error":
            return "Something went wrong on my side. Nothing was confirmed. Please try again."
        case "rate_limited":
            return "You're sending messages faster than I can handle. Please wait a moment."
        case "quarantined":
            return "This conversation hit an error I can't recover from. Please start a new session."
        case _:
            assert_never(reason.kind)
