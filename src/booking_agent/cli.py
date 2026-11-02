"""Command line: chat in the terminal or run the API server.

Typed text never confirms a booking. When a quote is waiting, the terminal says so; the Confirm button
is in the web UI.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booking_agent.agent import Agent
    from booking_agent.provider.client import ProviderAPI


def _chat(args: argparse.Namespace) -> int:
    from booking_agent.config import Settings, build

    settings = Settings()
    agent, provider = build(settings)
    conversation_id = args.conversation or f"cli-{uuid.uuid4().hex[:6]}"
    print(f"BookingAgent (model: {settings.llm_model}). Conversation {conversation_id}, user {args.user}.")
    print("Commands: /scan (check for provider cancellations), /provider-cancel <booking_id>, /quit\n")
    shown = _message_count(agent, conversation_id)
    while True:
        shown = _print_new_agent_messages(agent, conversation_id, shown)
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in ("/quit", "/exit"):
            return 0
        if not text or _run_command(text, agent, provider):
            continue
        _send(agent, args, conversation_id, text)
        shown = _message_count(agent, conversation_id)


def _message_count(agent: Agent, conversation_id: str) -> int:
    conversation = agent.store.get_conversation(conversation_id)
    return len(conversation.messages) if conversation is not None else 0


def _print_new_agent_messages(agent: Agent, conversation_id: str, shown: int) -> int:
    """Print agent messages saved by the background worker since the last prompt. Returns the new count."""
    conversation = agent.store.get_conversation(conversation_id)
    if conversation is None:
        return shown
    for message in conversation.messages[shown:]:
        if message.role == "agent":
            print(f"agent> {message.text}\n")
    return len(conversation.messages)


def _send(agent: Agent, args: argparse.Namespace, conversation_id: str, text: str) -> None:
    """Send one message to the agent and print the reply."""
    from booking_agent.errors import AccessDenied, EnqueueFailed

    try:
        result = agent.handle(conversation_id=conversation_id, user_id=args.user, text=text)
    except AccessDenied:
        print("agent> This conversation belongs to a different user.\n")
        return
    except EnqueueFailed:
        print("agent> Couldn't take your request, please try again.\n")
        return
    print(f"agent> {result.text}\n")
    card_type = (result.to_dict().get("card") or {}).get("type")
    if card_type == "quote":
        print("       (a quote is waiting; confirm it with the button in the web UI, typed text cannot confirm)\n")
    if args.show_trace:
        print(f"       [trace: {result.trace_id}, exit: {result.exit.kind}, answered by: {result.answered_by}]\n")


def _run_command(text: str, agent: Agent, provider: ProviderAPI) -> bool:
    """Run a slash command. False means the text is a message for the agent."""
    if text == "/scan":
        print(f"agent> queued {agent.scan_provider_cancellations()} replacement search(es)\n")
        return True
    if text.startswith("/provider-cancel "):
        command = getattr(provider, "provider_cancel", None)
        if command is None:
            print("agent> Provider simulation is unavailable for this integration.")
            return True
        command(text.split(maxsplit=1)[1])
        print("agent> (simulated) the provider cancelled that booking\n")
        return True
    return False


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("booking_agent.api:app_factory", factory=True, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="booking-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat", help="chat with the agent in the terminal")
    chat.add_argument("--user", default="u1")
    chat.add_argument("--conversation", default=None)
    chat.add_argument("--show-trace", action="store_true")
    chat.set_defaults(fn=_chat)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(fn=_serve)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
