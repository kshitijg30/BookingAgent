"""The conversation: the model reads a short context, talks to the user and calls tools.

Context per turn is small on purpose, in this order: the system prompt, PERSONAL_MEMORY (confirmed keys as JSON),
the conversation summary (if any), a state line built by code, and the last 8 turns. Provider review text is
never sent to the model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from booking_agent.domain.entities import IST, Criteria
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.memory import confirmed_memory, key_to_ask, mark_asked, window_start
from booking_agent.domain.memory_facts import split_forgotten
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.memory import change_memory
from booking_agent.flow.search import failed_slots
from booking_agent.flow.tools import run_tool, tool_definitions
from booking_agent.limits import LimitHit
from booking_agent.model import ChatError, ChatModel, tool_messages
from booking_agent.render import describe_criteria, format_rupees, question

MAX_MODEL_STEPS = 5
MAX_HISTORY_CHARS = 400  # per message
MAX_NAME_CHARS = 60

# Phrases where the model says a write already happened.
_CLAIM = re.compile(
    r"\b(i(?:'ve| have)? (?:booked|cancelled|canceled|moved|rescheduled|confirmed)|"
    r"(?:is|has been|now) (?:booked|confirmed|cancelled|canceled|rescheduled))\b",
    re.IGNORECASE,
)


# Words that show a reply is asking for a field. Only the fields still missing may be asked for.
_ASKS = {
    "service": ("what service", "which service", "what do you need", "what kind of"),
    "area": ("which area", "what area", "your area", "where are you", "your location"),
    "dates": ("which day", "what day", "which date", "what date", "when would", "when do you", "what time"),
    "budget": ("budget", "how much", "price range"),
}


def _reasks_known(reply: str, missing: list[str]) -> bool:
    """True if a reply that asks a question mentions a field that is already known."""
    text = reply.lower()
    return "?" in text and any(word in text for name, words in _ASKS.items() if name not in missing for word in words)


def _request_lines(criteria: Criteria) -> list[str]:
    known = []
    if criteria.service:
        known.append(f"service = {criteria.service}")
    if criteria.area:
        known.append(f"area = {criteria.area}")
    if criteria.any_date:
        known.append("dates = any day")
    elif criteria.date_from:
        known.append(
            "dates = " + describe_criteria(criteria.model_copy(update={"days": (), "times": (), "max_price": None}))
        )
    if criteria.days:
        known.append("days = " + ",".join(criteria.days))
    if criteria.times:
        known.append("times = " + ", ".join(f"{r.from_time:%H:%M}-{r.to_time:%H:%M}" for r in criteria.times))
    if criteria.max_price is not None:
        known.append(f"max price = {format_rupees(criteria.max_price)}")
    elif criteria.no_budget:
        known.append("budget = no limit")
    if criteria.provider_id:
        known.append(f"provider = {criteria.provider_id}")
    return known


def _screen_lines(deps: Deps, turn: TurnContext) -> list[str]:
    """Where the conversation is, derived from the newest search and the current quote."""
    quote, search = turn.quote, turn.search
    if quote is not None:
        when = quote.start.astimezone(IST)
        name = quote.provider_name[:MAX_NAME_CHARS]
        base = f"a {quote.action} quote for {name} at {when:%a %d %b %H:%M}, {format_rupees(quote.price)}"
        if quote.status == "confirmed":
            return [f"On screen: {base}, confirmed by the user, waiting for the provider."]
        return [f"On screen: {base}, waiting for the user to confirm."]
    if search is not None and search.results:
        return [f"On screen: offers from {len(search.results)} providers; the user picks a slot."]
    return []


def summary(deps: Deps, turn: TurnContext) -> str:
    """The state line: what code knows, as plain text for the model."""
    now = turn.now.astimezone(IST)
    lines = [f"Now: {now:%A %d %B %Y, %H:%M} (Asia/Kolkata)."]
    profile = deps.store.get_profile(turn.user_id)
    if profile.addresses:
        lines.append("Addresses: " + "; ".join(f"{a.label}: {a.address}" for a in profile.addresses))
    _, forgotten = split_forgotten(turn.conversation.summary)
    if forgotten:
        lines.append(f"Forgotten in this chat: {', '.join(forgotten)}. Do not use it; ask the user again.")
    memory = profile.memory
    ask = key_to_ask(memory)
    if ask is not None:
        entry = memory[ask]
        lines.append(
            f"Ask the user once whether this is right: {ask} = {json.dumps(entry['value'])}. Do not use it yet. "
            "Record the answer with confirm_memory."
        )
        user_id, now = turn.user_id, turn.now
        turn.write(lambda store: change_memory(store, user_id, lambda m: mark_asked(m, ask), now))
    criteria = turn.search.criteria if turn.search is not None else Criteria()
    lines.append("Known: " + ("; ".join(_request_lines(criteria)) or "nothing yet"))
    missing = criteria.missing()
    if missing:
        lines.append(
            "Missing: " + ", ".join(missing) + ". Ask for just these, once. Never ask again for what is Known."
        )
    skipped = sorted(failed_slots(deps.store, turn.user_id, turn.conversation_id), key=lambda key: key[1])
    if skipped:
        lines.append(
            "Slots that failed and are skipped: "
            + ", ".join(f"{pid} {start:%a %d %b %H:%M}" for pid, start in skipped[:6])
        )
    return "\n".join([*lines, *_screen_lines(deps, turn)])


def history(turn: TurnContext) -> list[dict[str, Any]]:
    """The last 8 turns of the conversation, trimmed, in the chat format the model expects."""
    messages: list[dict[str, Any]] = []
    stored = turn.conversation.messages
    for message in stored[window_start(stored) :]:
        text = message.text[:MAX_HISTORY_CHARS]
        if text:
            messages.append({"role": "user" if message.role == "user" else "assistant", "content": text})
    return messages


def converse(deps: Deps, model: ChatModel, turn: TurnContext, text: str) -> str | None:
    """Run the model with tools until it replies. Returns the model's reply text."""
    turn.text = text
    memory = confirmed_memory(deps.store.get_profile(turn.user_id).memory)
    conversation_summary, _ = split_forgotten(turn.conversation.summary)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": deps.prompts.system},
        {"role": "system", "content": "PERSONAL_MEMORY: " + json.dumps(memory, separators=(",", ":"))},
        *(
            [{"role": "system", "content": f"Conversation summary: {conversation_summary}"}]
            * bool(conversation_summary)
        ),
        {"role": "system", "content": summary(deps, turn)},
        *history(turn),
        {"role": "user", "content": text},
    ]
    for _ in range(MAX_MODEL_STEPS):
        turn.turn_limits.charge_model_call()
        with turn.tracer.span("model chat") as span:
            try:
                response = model.respond(messages, tool_definitions(deps.prompts, deps.catalog))
            except ChatError as error:
                turn.tracer.set(span, ok=False, error=str(error))
                turn.end(ExitReason(kind="llm_unavailable"))
                return None
            turn.tracer.set(
                span,
                **{
                    "ok": True,
                    "gen_ai.request.model": response.model,
                    "gen_ai.usage.input_tokens": response.input_tokens,
                    "gen_ai.usage.output_tokens": response.output_tokens,
                    "cost_usd": response.cost_usd,
                    "tool_calls": [call.name for call in response.tool_calls],
                },
            )
        if not response.tool_calls:
            return _without_false_claims(response.text or "", turn, deps)
        messages.append(tool_messages(response.tool_calls))
        for call in response.tool_calls:
            with turn.tracer.span(
                f"agent tool {call.name}", **{"tool.name": call.name, "tool.args": call.arguments}
            ) as span:
                result = run_tool(deps, turn, call.name, call.arguments)
                turn.tracer.set(span, result=result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})
    raise LimitHit("model_calls")


def _without_false_claims(reply: str, turn: TurnContext, deps: Deps) -> str:
    """The model must not claim a write happened; only code reports confirmed results."""
    if _CLAIM.search(reply):
        turn.tracer.record("reply_claim_removed", now=turn.now, reply=reply[:200])
        key = "claim_with_card" if turn.quote_message is not None else "claim_without_card"
        return deps.prompts.replies.get(key, "I haven't changed anything yet.")
    missing = (turn.search.criteria if turn.search is not None else Criteria()).missing()
    if _reasks_known(reply, missing):
        turn.tracer.record("reply_reask_replaced", now=turn.now, reply=reply[:200], missing=missing)
        if missing:
            return question(missing, deps.catalog)[0]
        return "Please pick a slot from the offers below, or check the quote card and confirm."
    return reply
