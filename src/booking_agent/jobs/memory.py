"""The memory_update job.

1. Ask the memory model what changed in the current turn (the user's latest message and the agent messages after
   it) and apply the operations to ``user_profiles.memory``. Older messages are not sent, so a key the user
   removed is not learned again from them.
2. If the conversation has more than 8 user turns beyond ``summarized_count``, fold the messages that left the
   8-turn window into ``conversations.summary`` and advance ``summarized_count``.
3. A removed key (forget) is also taken out of the summary (the model rewrites it) and listed on a trailing
   ``Forgotten: a, b`` line of the summary, which the state line shows. A key set again leaves that list.

The model calls run first. The writes are one transaction under the per-user lock, like a user turn.
"""

from __future__ import annotations

from booking_agent.domain.entities import ChatMessage, Conversation
from booking_agent.domain.memory import apply_operations, window_start
from booking_agent.domain.memory_facts import (
    build_memory_messages,
    build_summary_messages,
    join_forgotten,
    parse_memory_operations,
    parse_summary,
    split_forgotten,
)
from booking_agent.flow.memory import change_memory
from booking_agent.jobs.common import JobContext
from booking_agent.rows import Job

MAX_FOLDED_CHARS = 400  # per message sent to the summary prompt


def _pairs(messages: tuple[ChatMessage, ...] | list[ChatMessage], limit: int | None = None) -> list[tuple[str, str]]:
    return [(m.role, m.text[:limit]) for m in messages if m.text]


def handle_memory_update(context: JobContext, job: Job) -> bool:
    deps = context.deps
    model = deps.memory_model
    if model is None:
        return False
    user_id, conversation_id = job.payload["user_id"], job.payload["conversation_id"]
    conversation = deps.store.get_conversation(conversation_id)
    messages = conversation.messages if conversation else ()
    last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=None)
    turn = _pairs(messages[last_user:]) if last_user is not None else []
    response = model.respond(build_memory_messages(deps.store.get_profile(user_id).memory, turn), [])
    operations = parse_memory_operations(response.text or "")

    removed = [op.key for op in operations if op.op == "remove"]
    restored = {op.key for op in operations if op.op == "set"}
    folded: tuple[int, int, str, list[str], list[str]] | None = None  # old count, new count, text, removed, restored
    if conversation is not None:
        start = window_start(messages)
        new_count = max(start, conversation.summarized_count)
        if new_count > conversation.summarized_count or removed or restored:
            old_text, _ = split_forgotten(conversation.summary)
            text = old_text
            if new_count > conversation.summarized_count or removed:
                leaving = _pairs(messages[conversation.summarized_count : new_count], MAX_FOLDED_CHARS)
                reply = model.respond(build_summary_messages(old_text, leaving, removed), [])
                text = parse_summary(reply.text or "", old_text)
            folded = (conversation.summarized_count, new_count, text, removed, sorted(restored))

    now = deps.clock.now()
    with deps.user_locks.get(user_id), deps.store.tx():
        change_memory(deps.store, user_id, lambda memory: apply_operations(memory, operations), now)
        if folded is not None:
            _save_summary(context, conversation_id, *folded)
    return False


def _save_summary(
    context: JobContext,
    conversation_id: str,
    old_count: int,
    new_count: int,
    text: str,
    removed: list[str],
    restored: list[str],
) -> None:
    store = context.deps.store
    current: Conversation | None = store.get_conversation(conversation_id)
    if current is None or current.summarized_count != old_count:
        return
    _, forgotten = split_forgotten(current.summary)
    forgotten = [key for key in forgotten if key not in restored] + removed
    summary = join_forgotten(text, forgotten)
    if summary == current.summary and new_count == old_count:
        return
    updated = current.model_copy(update={"summary": summary, "summarized_count": new_count})
    store.save_conversation(updated, expected_version=current.version)
