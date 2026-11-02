"""What the HTTP routes share: the agent, the mock provider and settings, stored on ``app.state``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request

from booking_agent.config import Settings
from booking_agent.live_updates import SessionUpdates

if TYPE_CHECKING:
    from booking_agent.admin import DemoControls
    from booking_agent.agent import Agent
    from booking_agent.provider.client import ProviderAPI


@dataclass
class AppState:
    settings: Settings
    agent: Agent
    provider: ProviderAPI | None = None
    demo: DemoControls | None = None
    authenticate: Callable[[Request], str] | None = None
    owns_store: bool = False
    owns_provider: bool = False
    run_workers: bool = True
    updates: SessionUpdates = field(default_factory=SessionUpdates)


def notify_conversation_updated(state: AppState, conversation_id: str, trace_id: str | None, exit_kind: str) -> None:
    """Tell open event streams that a new agent message was saved for this conversation."""
    state.updates.publish(
        conversation_id,
        {"type": "conversation.updated", "conversation_id": conversation_id, "trace_id": trace_id, "exit": exit_kind},
    )


def get_app_state(request: Request) -> AppState:
    state: AppState = request.app.state.booking
    return state


App = Annotated[AppState, Depends(get_app_state)]


def authenticated_user(request: Request) -> str:
    state = get_app_state(request)
    if state.authenticate is not None:
        user_id = state.authenticate(request)
    elif state.settings.demo_mode:
        user_id = request.headers.get("x-user-id", "")
    else:
        raise HTTPException(401, "An authenticated identity resolver is required")
    if not isinstance(user_id, str) or not user_id or len(user_id) > 64:
        raise HTTPException(401, "Invalid authenticated identity")
    return user_id


UserId = Annotated[str, Depends(authenticated_user)]
