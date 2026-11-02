"""The user HTTP API and the admin API, over a fake agent and an in-memory repository."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from booking_agent.api import create_app
from booking_agent.errors import AccessDenied, EnqueueFailed
from tests.http_support import (
    OTHER_USER,
    USER,
    FakeAgent,
    FakeDemoProvider,
    agent_message,
    at,
    ensure_card_action_shape,
    make_settings,
    seed_booking,
    seed_conversation,
    seed_quote,
    seed_search,
    user_message,
)

ME = {"X-User-Id": USER}
THEM = {"X-User-Id": OTHER_USER}
ADMIN = {"X-Admin-Code": "adm"}


@pytest.fixture(autouse=True)
def _card_action_shape() -> None:
    ensure_card_action_shape()


@pytest.fixture
def agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture
def client(agent: FakeAgent, tmp_path: Path) -> Any:
    app = create_app(make_settings(tmp_path), agent=agent, run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- chat routing


def test_message_goes_to_the_agent_as_text(client: TestClient, agent: FakeAgent) -> None:
    response = client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", "message": "need a plumber"})
    assert response.status_code == 200
    assert response.json()["exit"] == {"kind": "answered"}
    assert agent.handled == [
        {"conversation_id": "c1", "user_id": USER, "text": "need a plumber", "pick_slot": None, "card_action": None}
    ]


def test_pick_slot_goes_to_the_agent_as_a_pair(client: TestClient, agent: FakeAgent) -> None:
    body = {"conversation_id": "c1", "pick_slot": {"provider_id": "p1", "slot_start": "2026-11-03T10:00:00+05:30"}}
    assert client.post("/v1/chat", headers=ME, json=body).status_code == 200
    (request,) = agent.handled
    provider_id, slot_start = request["pick_slot"]
    assert provider_id == "p1" and slot_start.isoformat() == "2026-11-03T10:00:00+05:30"
    assert request["text"] == "" and request["card_action"] is None


def test_confirm_and_decline_become_card_actions(client: TestClient, agent: FakeAgent) -> None:
    confirm = {"type": "confirm", "quote_id": "q1", "version": 2}
    decline = {"type": "decline", "quote_id": "q1"}
    assert client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", "action": confirm}).status_code == 200
    assert client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", "action": decline}).status_code == 200
    first, second = (request["card_action"] for request in agent.handled)
    assert (first.kind, first.quote_id, first.version) == ("confirm", "q1", 2)
    assert (second.kind, second.quote_id, second.version) == ("decline", "q1", None)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"message": ""},
        {"message": "hi", "pick_slot": {"provider_id": "p1", "slot_start": "2026-11-03T10:00:00+05:30"}},
        {"message": "hi", "action": {"type": "decline", "quote_id": "q1"}},
        {
            "pick_slot": {"provider_id": "p1", "slot_start": "2026-11-03T10:00:00+05:30"},
            "action": {"type": "decline", "quote_id": "q1"},
        },
        {"action": {"type": "confirm", "quote_id": "q1"}},
        {"action": {"type": "approve", "quote_id": "q1", "version": 1}},
    ],
)
def test_chat_needs_exactly_one_valid_input(client: TestClient, agent: FakeAgent, extra: dict[str, Any]) -> None:
    response = client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", **extra})
    assert response.status_code == 422
    assert agent.handled == []


def test_chat_needs_a_conversation_id_not_a_session_id(client: TestClient) -> None:
    assert client.post("/v1/chat", headers=ME, json={"session_id": "c1", "message": "hi"}).status_code == 422


def test_other_users_conversation_is_a_404(client: TestClient, agent: FakeAgent) -> None:
    agent.outcome = AccessDenied("not yours")
    response = client.post("/v1/chat", headers=THEM, json={"conversation_id": "c1", "message": "hi"})
    assert response.status_code == 404
    assert response.json() == {"detail": "conversation not found"}


def test_failed_confirm_is_a_503_and_logs_an_alert_line(
    client: TestClient, agent: FakeAgent, caplog: pytest.LogCaptureFixture
) -> None:
    agent.outcome = EnqueueFailed("q-77")
    action = {"type": "confirm", "quote_id": "q-77", "version": 1}
    with caplog.at_level(logging.ERROR, logger="booking_agent.api"):
        response = client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", "action": action})
    assert response.status_code == 503
    assert response.json() == {"detail": "Couldn't take your request, please try again."}
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == f"enqueue_failed quote_id=q-77 user_id={USER} conversation_id=c1"


def test_chat_returns_the_turn_result_unchanged(client: TestClient, agent: FakeAgent) -> None:
    agent.outcome = {
        "text": "Here you go",
        "exit": {"kind": "needs_input"},
        "trace_id": "t9",
        "answered_by": "model",
        "messages": [{"role": "agent", "text": "Here you go"}],
        "card": {"type": "offers", "search_id": "s1"},
    }
    assert client.post("/v1/chat", headers=ME, json={"conversation_id": "c1", "message": "hi"}).json() == agent.outcome


def test_chat_needs_an_identity(client: TestClient) -> None:
    assert client.post("/v1/chat", json={"conversation_id": "c1", "message": "hi"}).status_code == 401


# ---------------------------------------------------------------- reading conversations


def test_list_conversations_is_scoped_to_the_user(client: TestClient, agent: FakeAgent) -> None:
    seed_conversation(agent, "mine", [user_message("hi")])
    seed_conversation(agent, "theirs", [user_message("hi")], user_id=OTHER_USER)
    body = client.get("/v1/conversations", headers=ME).json()
    assert [row["id"] for row in body["conversations"]] == ["mine"]
    assert sorted(body["conversations"][0]) == ["id", "title", "updated_at"]


def test_conversation_cards_are_resolved_from_current_rows(client: TestClient, agent: FakeAgent) -> None:
    seed_search(agent, "s1", "c1")
    seed_quote(agent, "q1", "c1", status="confirmed", version=3, price=125000)
    seed_quote(agent, "q2", "c1", status="done", action="cancel")
    seed_booking(agent, "b1", "q2", "c1", status="cancelled")
    seed_conversation(
        agent,
        "c1",
        [
            user_message("plumber please"),
            agent_message("Options", {"type": "offers", "search_id": "s1"}),
            agent_message(
                "Your quote", {"type": "quote", "quote_id": "q1", "version": 1, "reviews": ["Quick and tidy"]}
            ),
            agent_message("Done", {"type": "confirmation", "booking_id": "b1"}),
            agent_message("Which day?", {"type": "question", "choices": ["Mon", "Tue"]}),
            agent_message("Heads up", {"type": "notice"}),
            agent_message("plain"),
        ],
    )
    messages = client.get("/v1/conversations/c1", headers=ME).json()["messages"]
    assert [m["role"] for m in messages] == ["user", *["agent"] * 6]
    assert {key: messages[0][key] for key in ("role", "text", "card")} == {
        "role": "user",
        "text": "plumber please",
        "card": None,
    }
    assert messages[0]["at"].startswith("2026-11-02T09:00:00")

    offers = messages[1]["card"]
    assert [p["provider_id"] for p in offers["providers"]] == ["p-best", "p-same", "p-cheap"]  # rating desc, price asc
    assert sorted(offers["providers"][0]) == ["name", "price", "provider_id", "rating", "slots"]
    assert offers["providers"][0]["slots"][0]["start"].startswith("2026-11-03T12:00:00")

    quote = messages[2]["card"]
    assert quote["quote_id"] == "q1" and quote["reviews"] == ["Quick and tidy"]
    assert (quote["status"], quote["version"], quote["price"], quote["action"]) == ("confirmed", 3, 125000, "book")
    assert (quote["provider_name"], quote["service"]) == ("Best Co", "plumber")
    assert quote["start"].startswith("2026-11-03T12:00:00") and quote["end"].startswith("2026-11-03T13:00:00")

    confirmation = messages[3]["card"]
    assert confirmation["booking_id"] == "b1"
    assert (confirmation["status"], confirmation["action"], confirmation["price"]) == ("cancelled", "cancel", 120000)
    assert (confirmation["service"], confirmation["provider_name"]) == ("plumber", "Best Co")
    assert "start" in confirmation and "end" in confirmation

    assert messages[4]["card"] == {"type": "question", "choices": ["Mon", "Tue"]}
    assert messages[5]["card"] == {"type": "notice"}
    assert messages[6]["card"] is None


def test_card_with_a_missing_row_is_returned_as_stored(client: TestClient, agent: FakeAgent) -> None:
    seed_conversation(agent, "c1", [agent_message("x", {"type": "quote", "quote_id": "gone", "version": 1})])
    card = client.get("/v1/conversations/c1", headers=ME).json()["messages"][0]["card"]
    assert card == {"type": "quote", "quote_id": "gone", "version": 1}


def test_other_users_or_missing_conversation_is_a_404(client: TestClient, agent: FakeAgent) -> None:
    seed_conversation(agent, "c1", [user_message("hi")])
    assert client.get("/v1/conversations/c1", headers=THEM).status_code == 404
    assert client.get("/v1/conversations/nope", headers=ME).status_code == 404
    assert client.get("/v1/conversations/c1", headers=THEM).json() == {"detail": "conversation not found"}


# ---------------------------------------------------------------- bookings and quotes


def test_bookings_are_ours_newest_first_and_scoped(client: TestClient, agent: FakeAgent) -> None:
    seed_quote(agent, "q1", "c1")
    seed_quote(agent, "q2", "c1")
    seed_booking(agent, "old", "q1", "c1", created_at=at(8, day=1), start=at(10, day=5))
    seed_booking(agent, "new", "q2", "c1", created_at=at(8, day=2), start=at(10, day=4), provider_id="p2")
    seed_booking(agent, "foreign", "q2", "c1", user_id=OTHER_USER, provider_id="p3")
    rows = client.get("/v1/bookings", headers=ME).json()["bookings"]
    assert [row["id"] for row in rows] == ["new", "old"]
    for name in ("service", "provider_name", "start", "status"):
        assert name in rows[0]


def test_quotes_can_be_filtered_by_status(client: TestClient, agent: FakeAgent) -> None:
    seed_quote(agent, "q1", "c1", status="declined")
    seed_quote(agent, "q2", "c1", status="shown", updated_at=at(10))
    seed_quote(agent, "q3", "c2", user_id=OTHER_USER)
    assert {q["id"] for q in client.get("/v1/quotes", headers=ME).json()["quotes"]} == {"q1", "q2"}
    shown = client.get("/v1/quotes?status=shown", headers=ME).json()["quotes"]
    assert [q["id"] for q in shown] == ["q2"]
    assert client.get("/v1/quotes?status=bogus", headers=ME).status_code == 422


# ---------------------------------------------------------------- profile and memory


def test_profile_round_trip_is_per_user(client: TestClient) -> None:
    assert client.get("/v1/profile", headers=ME).json() == {"name": None, "phones": [], "addresses": []}
    body = {
        "name": "Asha",
        "phones": ["+91 98450 00000"],
        "addresses": [{"label": "Home", "address": "12 1st Main, Koramangala"}],
    }
    assert client.put("/v1/profile", headers=ME, json=body).json() == body
    assert client.get("/v1/profile", headers=ME).json() == body
    assert client.get("/v1/profile", headers=THEM).json() == {"name": None, "phones": [], "addresses": []}


def test_profile_rejects_an_address_without_text(client: TestClient) -> None:
    body = {"name": "A", "phones": [], "addresses": [{"label": "Home", "address": " "}]}
    assert client.put("/v1/profile", headers=ME, json=body).status_code == 422


def test_memory_view_correct_and_forget_are_scoped_to_the_user(client: TestClient) -> None:
    assert client.get("/v1/memory", headers=ME).json() == {"memory": {}}
    saved = client.put("/v1/memory/area", headers=ME, json={"value": "Koramangala"})
    assert saved.status_code == 200
    entry = {"value": "Koramangala", "source": "stated", "confirmed": True}
    assert client.get("/v1/memory", headers=ME).json() == {"memory": {"area": entry}}
    assert client.get("/v1/memory", headers=THEM).json() == {"memory": {}}

    assert client.delete("/v1/memory/area", headers=THEM).status_code == 404
    corrected = client.put("/v1/memory/area", headers=ME, json={"value": "HSR Layout"})
    assert corrected.status_code == 200
    assert client.get("/v1/memory", headers=ME).json()["memory"]["area"]["value"] == "HSR Layout"
    assert client.delete("/v1/memory/area", headers=ME).status_code == 200
    assert client.get("/v1/memory", headers=ME).json() == {"memory": {}}
    assert client.delete("/v1/memory/area", headers=ME).status_code == 404


def test_correcting_an_inferred_key_confirms_it_and_makes_it_stated(client: TestClient, agent: FakeAgent) -> None:
    profile = agent.store.get_profile(USER)
    inferred = {"earliest_time": {"value": "10:00", "source": "inferred", "confirmed": False}}
    agent.store.save_profile(profile.model_copy(update={"memory": inferred}), now=at(8))
    assert client.get("/v1/memory", headers=ME).json()["memory"]["earliest_time"]["confirmed"] is False
    client.put("/v1/memory/earliest_time", headers=ME, json={"value": "11:00"})
    entry = client.get("/v1/memory", headers=ME).json()["memory"]["earliest_time"]
    assert entry["value"] == "11:00" and entry["confirmed"] is True and entry["source"] == "stated"  # a user edit


def test_removed_routes_are_gone(client: TestClient) -> None:
    assert client.get("/v1/sessions", headers=ME).status_code == 404
    assert (
        client.post("/v1/memory", headers=ME, json={"text": "a fact row"}).status_code == 405
    )  # no fact rows any more


# ---------------------------------------------------------------- catalog, health, scan


def test_catalog_and_health(client: TestClient) -> None:
    assert "plumber" in client.get("/v1/catalog", headers=ME).json()["services"]
    assert client.get("/healthz").json()["status"] == "ok"


def test_scan_runs_in_demo_mode(client: TestClient, agent: FakeAgent) -> None:
    agent.scan_result = 2
    assert client.post("/v1/scan", headers=ME).json() == {"queued": 2}


# ---------------------------------------------------------------- admin


@pytest.fixture
def admin_client(agent: FakeAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("BA_ADMIN_CODE", "adm")
    demo = FakeDemoProvider()
    agent.demo_provider = demo  # type: ignore[attr-defined]
    app = create_app(make_settings(tmp_path), agent=agent, provider=demo, run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client


def test_admin_needs_the_code(admin_client: TestClient) -> None:
    for path in ("/admin/api/overview", "/admin/api/bookings", "/admin/api/jobs", "/admin/api/users/u1"):
        assert admin_client.get(path).status_code == 401
        assert admin_client.get(path, headers={"X-Admin-Code": "wrong"}).status_code == 401


def test_admin_is_disabled_without_a_code(agent: FakeAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BA_ADMIN_CODE", raising=False)
    app = create_app(make_settings(tmp_path), agent=agent, run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        assert test_client.get("/admin/api/overview", headers=ADMIN).status_code == 404


def test_admin_overview_has_the_new_count_shape(admin_client: TestClient, agent: FakeAgent) -> None:
    seed_conversation(agent, "c1", [user_message("hi")])
    seed_search(agent, "s1", "c1")
    seed_quote(agent, "q1", "c1", status="confirmed")
    seed_quote(agent, "q2", "c1", status="shown")
    seed_booking(agent, "b1", "q1", "c1")
    agent.store.enqueue_job(kind="book", quote_id="q1", payload={}, now=at(9))
    body = admin_client.get("/admin/api/overview", headers=ADMIN).json()
    counts = {key: value for key, value in body["counts"].items() if key != "memory"}
    assert counts == {
        "conversations": 1,
        "searches": 1,
        "quotes": {"confirmed": 1, "shown": 1},
        "bookings": {"booked": 1},
        "jobs": {"pending": 1},
    }
    assert body["demo"] == {"forced_book": [], "forced_status_timeouts": 0}
    assert body["activity"] == []


def test_admin_bookings_and_quotes_replace_approvals(admin_client: TestClient, agent: FakeAgent) -> None:
    seed_quote(agent, "q1", "c1")
    seed_booking(agent, "b1", "q1", "c1")
    body = admin_client.get("/admin/api/bookings", headers=ADMIN).json()
    assert sorted(body) == ["bookings", "quotes"]
    assert body["bookings"][0]["id"] == "b1" and body["quotes"][0]["id"] == "q1"


def test_admin_jobs_lists_recent_jobs(admin_client: TestClient, agent: FakeAgent) -> None:
    job = agent.store.enqueue_job(kind="book", quote_id="q1", payload={"quote_id": "q1"}, now=at(9))
    body = admin_client.get("/admin/api/jobs?limit=5", headers=ADMIN).json()
    (row,) = body["jobs"]
    assert row["id"] == job.id and row["kind"] == "book" and row["status"] == "pending"
    assert sorted(row) == sorted(
        [
            "id", "kind", "status", "attempts", "max_attempts", "run_at", "locked_until",
            "last_error", "created_at", "updated_at", "payload",
        ]
    )  # fmt: skip
    assert admin_client.get("/admin/api/jobs?limit=0", headers=ADMIN).status_code == 422


def test_admin_user_lookup_shows_everything_for_the_user(admin_client: TestClient, agent: FakeAgent) -> None:
    from booking_agent.domain.entities import Address, UserProfile

    agent.store.save_profile(
        UserProfile(
            user_id=USER,
            name="Asha",
            phones=("1",),
            addresses=(Address(label="Home", address="Koramangala"),),
            memory={"area": {"value": "Koramangala", "source": "stated", "confirmed": True}},
        ),
        now=at(9),
    )
    seed_conversation(agent, "c1", [user_message("hi")])
    seed_quote(agent, "q1", "c1")
    seed_booking(agent, "b1", "q1", "c1")
    body = admin_client.get("/admin/api/users/u1", headers=ADMIN).json()
    assert sorted(body) == ["bookings", "conversations", "memory", "profile", "provider_bookings"]
    assert body["profile"]["name"] == "Asha" and body["profile"]["addresses"] == [
        {"label": "Home", "address": "Koramangala"}
    ]
    assert body["memory"]["area"]["value"] == "Koramangala"
    assert [c["id"] for c in body["conversations"]] == ["c1"]
    assert [b["id"] for b in body["bookings"]] == ["b1"]
    assert body["provider_bookings"] == [{"booking_id": "pb-1", "status": "upcoming"}]


def test_admin_settings_apply_and_validate(admin_client: TestClient, agent: FakeAgent) -> None:
    current = admin_client.get("/admin/api/settings", headers=ADMIN).json()["settings"]
    assert "asks_per_field" not in current
    assert (
        admin_client.put("/admin/api/settings", headers=ADMIN, json=current | {"book_success": 0.9}).status_code == 422
    )
    saved = admin_client.put("/admin/api/settings", headers=ADMIN, json=current | {"tool_calls_per_turn": 7})
    assert saved.status_code == 200
    assert agent.applied[-1][1].tool_calls_per_turn == 7
    assert agent.store.get_setting("runtime")["tool_calls_per_turn"] == 7  # type: ignore[index]


def test_admin_demo_controls(admin_client: TestClient, agent: FakeAgent) -> None:
    demo: FakeDemoProvider = agent.demo_provider  # type: ignore[attr-defined]
    admin_client.post("/admin/api/demo/next-book", headers=ADMIN, json={"outcome": "timeout_booked", "count": 2})
    assert list(demo.forced_book) == ["timeout_booked", "timeout_booked"]
    admin_client.post("/admin/api/demo/status-timeouts", headers=ADMIN, json={"count": 3})
    assert demo.forced_status_timeouts == 3
    agent.scan_result = 1
    cancelled = admin_client.post("/admin/api/demo/provider-cancel", headers=ADMIN, json={"booking_id": "bk-1"})
    assert cancelled.json() == {"cancelled_by_provider": "bk-1", "replacements_queued": 1}
    assert demo.cancelled == ["bk-1"]
    admin_client.post("/admin/api/demo/clear", headers=ADMIN)
    assert not demo.forced_book and demo.forced_status_timeouts == 0
