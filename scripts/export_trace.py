"""Write docs/traces/timeout-after-success.jsonl: one run where book() times out but the booking exists.

The run: user message -> search -> hold -> quote shown -> confirm tapped -> job queued -> the worker's book call
times out -> get_booking_status with the same key -> booked -> booking row -> confirmation message.
Everything is offline: a scripted chat model, a seeded mock provider, a fixed clock and SQLite in a temp dir.
Each line of the output is one event, model call or tool call from tracing.timeline, in time order.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from booking_agent.agent import Agent
from booking_agent.clock import FixedClock
from booking_agent.domain.models import IST
from booking_agent.flow.context import CardAction
from booking_agent.jobs.registry import JOB_HANDLERS
from booking_agent.jobs.worker import JobWorker
from booking_agent.llm import ScriptedChatModel, call, say
from booking_agent.provider.mock import MockProvider
from booking_agent.provider.scenario import Scenario
from booking_agent.store import Store
from booking_agent.tracing import Telemetry, read_jsonl_dir, timeline

OUT = Path(__file__).resolve().parents[1] / "docs" / "traces" / "timeout-after-success.jsonl"
CONVERSATION = "demo"
USER = "u1"


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    trace_dir = tmp / "traces"
    clock = FixedClock(datetime(2026, 11, 2, 9, 0, tzinfo=IST))  # a Monday morning
    provider = MockProvider(tmp / "provider.sqlite3", clock, Scenario(seed=7, book_outcomes=("timeout_booked",)))
    store = Store(tmp / "agent.sqlite3")
    model = ScriptedChatModel(
        [
            call(
                "find_and_propose_booking",
                service="plumber",
                area="Koramangala",
                date_from="2026-11-02",
                date_to="2026-11-08",
                part_of_day="morning",
                max_price_rupees=1500,
            ),
            say("Iyer Plumbing can come in the morning. Confirm on the card if that works."),
        ]
    )
    agent = Agent(
        store=store,
        provider=provider,
        model=model,
        clock=clock,
        telemetry=Telemetry(trace_dir=trace_dir),
    )
    first = agent.handle(
        conversation_id=CONVERSATION,
        user_id=USER,
        text="Find a plumber for my kitchen tap this week, mornings only, under ₹1,500 in Koramangala",
    )
    if first.exit.kind != "needs_input" or first.exit.detail != "choose_slot":
        raise SystemExit(f"expected an offers card, got {first.exit}")
    search = store.newest_search_for_conversation(CONVERSATION)
    if search is None or not search.results:
        raise SystemExit("no offers were saved")
    offer = search.results[0]
    clock.advance(seconds=20)  # the user looks at the offers card, then taps the first slot
    agent.handle(
        conversation_id=CONVERSATION,
        user_id=USER,
        pick_slot=(offer.provider_id, offer.slots[0].start),
    )
    quote = store.current_quote_for_conversation(CONVERSATION)
    if quote is None:
        raise SystemExit("no quote was shown")
    clock.advance(minutes=1)
    queued = agent.handle(
        conversation_id=CONVERSATION,
        user_id=USER,
        card_action=CardAction(kind="confirm", quote_id=quote.id, version=quote.version),
    )
    if queued.exit.kind != "queued":
        raise SystemExit(f"confirm was not queued: {queued.exit}")
    ran = JobWorker(agent.deps, agent.runner, JOB_HANDLERS).run_due()
    booked = [b for b in provider.all_bookings() if b["user_id"] == USER and not b["booking_id"].startswith("bk-seed")]
    if ran < 1 or len(booked) != 1:
        raise SystemExit(f"expected one booking after the worker ran, got {len(booked)} ({ran} jobs)")
    lines = timeline(read_jsonl_dir(trace_dir))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    print(f"wrote {OUT}: {len(lines)} lines, {len({line['trace_id'] for line in lines})} traces")
    store.close()
    provider.close()


if __name__ == "__main__":
    main()
