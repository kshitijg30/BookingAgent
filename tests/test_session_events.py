"""The conversation event stream.

TestClient buffers a whole response, so it cannot read a stream that never ends. The stream test runs
the app under uvicorn on a free local port and reads it with httpx.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from booking_agent import api as api_module
from booking_agent.api import create_app
from booking_agent.app_state import AppState, notify_conversation_updated
from tests.http_support import FakeAgent, make_settings, seed_conversation, user_message


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, FastAPI, FakeAgent]]:
    monkeypatch.setattr(api_module, "HEARTBEAT_S", 0.2)
    agent = FakeAgent()
    app = create_app(make_settings(tmp_path), agent=agent, run_workers=False)  # type: ignore[arg-type]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", app, agent
    server.should_exit = True
    thread.join(timeout=10)


def _read_until(lines: Iterator[str], wanted: str) -> list[str]:
    seen: list[str] = []
    for line in lines:
        seen.append(line)
        if line.startswith(wanted):
            return seen
    raise AssertionError(f"stream ended before {wanted!r}: {seen}")


def test_stream_delivers_a_published_event(live_server: tuple[str, FastAPI, FakeAgent]) -> None:
    base_url, app, agent = live_server
    state: AppState = app.state.booking
    headers = {"X-User-Id": "u1"}
    seed_conversation(agent, "c1", [user_message("hello")])
    with httpx.Client(base_url=base_url, timeout=10) as client:
        with client.stream("GET", "/v1/conversations/c1/events", headers=headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            lines = response.iter_lines()
            assert next(lines) == "retry: 3000"
            _read_until(lines, ": ping")  # a heartbeat arrives once the stream is idle
            notify_conversation_updated(state, "c1", "trace-9", "queued")
            notify_conversation_updated(state, "other-conversation", "trace-0", "queued")
            frame = _read_until(lines, "data:")
        assert frame[-2] == "event: conversation.updated"
        assert json.loads(frame[-1].removeprefix("data: ")) == {
            "type": "conversation.updated",
            "conversation_id": "c1",
            "trace_id": "trace-9",
            "exit": "queued",
        }
    deadline = time.monotonic() + 5
    while state.updates.subscriber_count("c1") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert state.updates.subscriber_count("c1") == 0


def test_stream_rejects_another_users_conversation(tmp_path: Path) -> None:
    agent = FakeAgent()
    seed_conversation(agent, "c1", [user_message("hello")])
    app = create_app(make_settings(tmp_path), agent=agent, run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        other = client.get("/v1/conversations/c1/events", headers={"X-User-Id": "u2"})
        assert other.status_code == 404
        assert other.json() == {"detail": "conversation not found"}


def test_stream_needs_the_access_code(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, access_code="sesame"), agent=FakeAgent(), run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/v1/conversations/new/events", headers={"X-User-Id": "u1"}).status_code == 401
