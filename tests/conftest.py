"""Shared harness: a deterministic world (frozen clock, seeded mock with its own DB, scripted chat models).

Scenario tests marked ``@pytest.mark.scenario`` call ``record(actual=..., notes=...)``. At the end
of the run the plugin writes ``reports/eval_table.md`` from those results.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import patch

import pytest
from pluggy import Result

from booking_agent.agent import Agent, TurnResult
from booking_agent.clock import FixedClock
from booking_agent.domain.entities import Booking, Conversation, Quote, Search
from booking_agent.domain.models import IST
from booking_agent.flow.context import CardAction, TurnRequest
from booking_agent.jobs.registry import JOB_HANDLERS
from booking_agent.jobs.worker import JobWorker
from booking_agent.limits import Limits
from booking_agent.model import ScriptedChatModel
from booking_agent.provider.mock import MockProvider
from booking_agent.provider.scenario import Scenario, SeedBooking
from booking_agent.rows import Job
from booking_agent.store import Store
from booking_agent.tracing import read_jsonl_dir, timeline
from booking_agent.turn_runner import Operation

START = datetime(2026, 11, 2, 9, 0, tzinfo=IST)  # a Monday morning
REPORT = Path(__file__).resolve().parents[1] / "reports" / "eval_table.md"


def quiet_history(user: str = "u1") -> tuple[SeedBooking, ...]:
    """Provider history: two past electrician jobs and an upcoming AC servicing (for "same as last time")."""
    return (
        SeedBooking(user, "electrician", "Koramangala", 1, -40, 10, "completed"),
        SeedBooking(user, "electrician", "Koramangala", 1, -21, 16, "completed"),
        SeedBooking(user, "ac_service", "Koramangala", 0, 3, 14, "upcoming"),
    )


@dataclass
class World:
    """One test's universe: provider, agent store, agent, and the scripted models."""

    tmp: Path
    clock: FixedClock
    scenario: Scenario
    model: ScriptedChatModel
    memory_model: ScriptedChatModel
    limits: Limits
    provider: MockProvider = field(init=False)
    store: Store = field(init=False)
    agent: Agent = field(init=False)
    conversation: str = "c1"
    user: str = "u1"
    replies: list[TurnResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.provider = MockProvider(self.tmp / "provider.sqlite3", self.clock, self.scenario)
        self.store = Store(self.tmp / "agent.sqlite3")
        self.agent = self._agent()

    def _agent(self) -> Agent:
        return Agent(
            store=self.store,
            provider=self.provider,
            model=self.model,
            memory_model=self.memory_model,
            clock=self.clock,
            limits=self.limits,
            trace_dir=self.tmp / "traces",
        )

    # ------------------------------------------------------------ user actions
    def say(self, text: str, *, conversation: str | None = None, user: str | None = None) -> TurnResult:
        """Type a message."""
        return self._record(
            self.agent.handle(conversation_id=conversation or self.conversation, user_id=user or self.user, text=text)
        )

    def pick(
        self, provider_id: str, start: datetime, *, conversation: str | None = None, user: str | None = None
    ) -> TurnResult:
        """Tap a slot on an offers card."""
        return self._record(
            self.agent.handle(
                conversation_id=conversation or self.conversation,
                user_id=user or self.user,
                pick_slot=(provider_id, start),
            )
        )

    def confirm(
        self,
        quote_id: str | None = None,
        version: int | None = None,
        *,
        conversation: str | None = None,
        user: str | None = None,
    ) -> TurnResult:
        """Tap Confirm. By default on the current quote of the conversation, at its current version."""
        quote = self.current_quote(conversation)
        if quote_id is None:
            assert quote is not None, "no current quote to confirm"
            quote_id = quote.id
        if version is None:
            assert quote is not None, "no current quote to confirm"
            version = quote.version
        action = CardAction(kind="confirm", quote_id=quote_id, version=version)
        return self._record(
            self.agent.handle(
                conversation_id=conversation or self.conversation, user_id=user or self.user, card_action=action
            )
        )

    def decline(self, quote_id: str | None = None, *, conversation: str | None = None) -> TurnResult:
        """Tap "Not now". By default on the current quote."""
        if quote_id is None:
            quote = self.current_quote(conversation)
            assert quote is not None, "no current quote to decline"
            quote_id = quote.id
        action = CardAction(kind="decline", quote_id=quote_id)
        return self._record(
            self.agent.handle(conversation_id=conversation or self.conversation, user_id=self.user, card_action=action)
        )

    def _record(self, reply: TurnResult) -> TurnResult:
        self.replies.append(reply)
        self.clock.advance(seconds=1)  # rows are ordered by time; a fixed clock would tie them
        return reply

    def run_jobs(self, max_jobs: int | None = None) -> list[TurnResult]:
        """Run the due jobs and return the results of the worker turns they ran, oldest first.

        ``memory_update`` jobs run no turn, so they add no result. The results also go to ``replies`` so
        the end-of-test invariants see them.
        """
        runner = self.agent.runner
        original_run = runner.run
        results: list[TurnResult] = []

        def recording_run(request: TurnRequest, operation: Operation) -> TurnResult:
            result = original_run(request, operation)
            results.append(result)
            return result

        worker = JobWorker(self.agent.deps, runner, JOB_HANDLERS)
        with patch.object(runner, "run", side_effect=recording_run):
            worker.run_due(max_jobs)
        self.replies.extend(results)
        self.clock.advance(seconds=1)
        return results

    def restart(self) -> None:
        """Throw away every in-memory object; rebuild the agent and provider from their own databases."""
        self.store.close()
        self.provider.close()
        self.provider = MockProvider(self.tmp / "provider.sqlite3", self.clock, self.scenario)
        self.store = Store(self.tmp / "agent.sqlite3")
        self.agent = self._agent()

    # ------------------------------------------------------------ reading state
    def current_quote(self, conversation: str | None = None) -> Quote | None:
        return self.store.current_quote_for_conversation(conversation or self.conversation)

    def quotes(self, status: str | None = None, *, user: str | None = None) -> list[Quote]:
        """The user's quotes, oldest first."""
        rows = self.store.list_quotes_for_user(user or self.user, status=status, limit=500)  # type: ignore[arg-type]
        return sorted(rows, key=lambda quote: (quote.created_at, quote.id))

    def search(self, conversation: str | None = None) -> Search | None:
        return self.store.newest_search_for_conversation(conversation or self.conversation)

    def bookings(self, status: str | None = "booked", *, user: str | None = None) -> list[Booking]:
        """The bookings in our table (verified by the provider)."""
        return self.store.list_bookings_for_user(user or self.user, status=status)  # type: ignore[arg-type]

    def provider_bookings(self, status: str = "upcoming", *, user: str | None = None) -> list[dict[str, Any]]:
        """Bookings the provider holds for the user that this test created (seed history is left out)."""
        return [
            b
            for b in self.provider.all_bookings()
            if b["status"] == status
            and b["user_id"] == (user or self.user)
            and not b["booking_id"].startswith("bk-seed")
        ]

    def jobs(self, kind: str | None = None, status: str | None = None) -> list[Job]:
        """Every job, oldest first."""
        rows = sorted(self.store.recent_jobs(1000), key=lambda job: (job.created_at, job.id))
        return [job for job in rows if (kind is None or job.kind == kind) and (status is None or job.status == status)]

    def saved_conversation(self, conversation: str | None = None) -> Conversation:
        saved = self.store.get_conversation(conversation or self.conversation)
        assert saved is not None, "conversation was never saved"
        return saved

    def agent_texts(self, conversation: str | None = None) -> list[str]:
        return [m.text for m in self.saved_conversation(conversation).messages if m.role == "agent"]

    def trace_lines(self) -> list[dict[str, Any]]:
        return timeline(read_jsonl_dir(self.tmp / "traces"))

    def events(self, name: str) -> list[dict[str, Any]]:
        """Trace events of one type, in order."""
        return [line for line in self.trace_lines() if line["type"] == name]

    def tool_calls(self, name: str) -> list[dict[str, Any]]:
        """Provider tool calls recorded in traces (spans named ``tool <name>``)."""
        return [x for x in self.trace_lines() if x["type"] == "tool_call" and x["tool"] == name]

    # ------------------------------------------------------------ invariants
    def assert_invariants(self) -> None:
        """Checked after every scenario, whatever the test asserted.

        I1 a confirmation message points at a booking row, and the provider holds that booking.
        I2 no two booked rows for one user, provider and start.
        I3 every booked row has a quote that is done.
        I4 no job for a write exists unless its quote was confirmed.
        I5 the provider holds no duplicate active booking for one user, provider and start.
        """
        booking_rows = self.store.recent_bookings(1000)
        quote_rows = {row["id"]: row for row in self.store.recent_quotes(1000)}
        provider_ids = {b["booking_id"] for b in self.provider.all_bookings()}
        booked_ids = {row["id"] for row in booking_rows}
        provider_booking_ids = {row["provider_booking_id"] for row in booking_rows}

        for reply in self.replies:
            assert reply.exit is not None
            for message in reply.messages:
                if message.card and message.card.get("type") == "confirmation":
                    assert message.card["booking_id"] in booked_ids, "confirmation for a booking we have no row for"
        assert provider_booking_ids <= provider_ids, "a booking row points at a booking the provider does not have"

        booked = [row for row in booking_rows if row["status"] == "booked"]
        keys = [(row["user_id"], row["provider_id"], row["start"]) for row in booked]
        assert len(keys) == len(set(keys)), "two booked rows for one user, provider and start"
        for row in booking_rows:
            quote = quote_rows[row["quote_id"]]
            assert quote["status"] == "done", f"booking {row['id']} has a quote that is {quote['status']}"

        for job in self.store.recent_jobs(1000):
            if job.kind in ("book", "cancel", "reschedule", "check"):
                quote = quote_rows[job.quote_id or ""]
                assert quote["confirmed_at"] is not None, f"{job.kind} job {job.id} for a quote nobody confirmed"

        slots = [
            (b["user_id"], b["provider_id"], b["slot_start"])
            for b in self.provider.all_bookings()
            if b["status"] == "upcoming"
        ]
        assert len(slots) == len(set(slots)), "duplicate active booking at the provider"


class MakeWorld(Protocol):
    """The ``make_world`` fixture: builds a fresh world; invariants are checked when the test ends."""

    def __call__(
        self,
        scenario: Scenario | None = None,
        model: ScriptedChatModel | None = None,
        limits: Limits | None = None,
        start: datetime = START,
    ) -> World: ...


class Record(Protocol):
    """The ``record`` fixture: what a scenario test observed, for reports/eval_table.md."""

    def __call__(self, *, actual: str, notes: str = "") -> None: ...


@pytest.fixture
def make_world(tmp_path: Path) -> Iterator[MakeWorld]:
    worlds: list[World] = []

    def _make(
        scenario: Scenario | None = None,
        model: ScriptedChatModel | None = None,
        limits: Limits | None = None,
        start: datetime = START,
    ) -> World:
        tmp = tmp_path / f"w{len(worlds)}"
        tmp.mkdir(parents=True, exist_ok=True)
        world = World(
            tmp=tmp,
            clock=FixedClock(start),
            scenario=scenario or Scenario(seed=7, seed_bookings=quiet_history()),
            model=model or ScriptedChatModel(),
            memory_model=ScriptedChatModel(),
            limits=limits or Limits(),
        )
        worlds.append(world)
        return world

    yield _make
    for world in worlds:
        world.assert_invariants()


def memory_ops(*operations: dict[str, Any]) -> str:
    """The JSON text the memory model returns: set/remove operations on keys. The summary is a separate call."""
    return json.dumps({"operations": list(operations)})


def set_op(key: str, value: Any, *, stated: bool) -> dict[str, Any]:
    return {"op": "set", "key": key, "value": value, "stated": stated}


def remove_op(key: str) -> dict[str, Any]:
    return {"op": "remove", "key": key}


# ---------------------------------------------------------------- eval table plugin
_RESULTS: dict[str, dict[str, Any]] = {}


@pytest.fixture
def record(request: pytest.FixtureRequest) -> Record:
    def _record(*, actual: str, notes: str = "") -> None:
        _RESULTS.setdefault(request.node.nodeid, {}).update(actual=actual, notes=notes)

    return _record


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("scenario")
    if marker is None or report.when not in ("call", "teardown"):
        return
    entry = _RESULTS.setdefault(item.nodeid, {})
    entry.update(id=marker.kwargs["id"], title=marker.kwargs["title"], expected=marker.kwargs["expected"])
    if report.when == "call":
        entry["passed"] = report.passed
        if report.failed:
            entry["notes"] = (entry.get("notes", "") + " " + str(report.longrepr).splitlines()[-1][:160]).strip()
    elif report.failed:
        entry["passed"] = False
        entry["notes"] = (entry.get("notes", "") + " invariant check failed").strip()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    rows = sorted((result for result in _RESULTS.values() if "id" in result), key=lambda result: result["id"])
    if not rows:
        return
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Eval table",
        "",
        "Generated by `uv run pytest`. Do not edit by hand.",
        "",
        f"{sum(1 for row in rows if row.get('passed'))}/{len(rows)} scenarios passed.",
        "",
        "| ID | Scenario | Expected | Actual | Result | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        cells = [
            row["id"],
            row["title"],
            row["expected"],
            row.get("actual", "(not recorded)"),
            "pass" if row.get("passed") else "FAIL",
            row.get("notes", ""),
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "/").replace("\n", " ") for cell in cells) + " |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
