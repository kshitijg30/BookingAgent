"""The HTTP interface accepts supplied integrations and trusted identity resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking_agent.api import create_app
from tests.http_support import FakeAgent, ensure_card_action_shape, make_settings


@pytest.fixture(autouse=True)
def _card_action_shape() -> None:
    ensure_card_action_shape()


def test_http_uses_authenticated_principal_and_disables_demo(tmp_path: Path) -> None:
    agent = FakeAgent()
    app = create_app(
        make_settings(tmp_path, demo_mode=False),
        agent=agent,  # type: ignore[arg-type]
        authenticate=lambda request: "trusted-user",
        run_workers=False,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat", headers={"X-User-Id": "forged-user"}, json={"conversation_id": "external", "message": "hello"}
        )
        assert response.status_code == 200
        assert agent.handled[0]["user_id"] == "trusted-user"
        assert client.post("/v1/scan").status_code == 403


def test_production_http_requires_identity_resolver(tmp_path: Path) -> None:
    agent = FakeAgent()
    app = create_app(make_settings(tmp_path, demo_mode=False), agent=agent, run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat", headers={"X-User-Id": "forged-user"}, json={"conversation_id": "external", "message": "hello"}
        )
        assert response.status_code == 401
        assert agent.handled == []


def test_access_code_gates_the_user_api_but_not_health(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, access_code="sesame"), agent=FakeAgent(), run_workers=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        headers = {"X-User-Id": "u1"}
        assert client.get("/v1/conversations", headers=headers).status_code == 401
        assert client.get("/v1/conversations", headers=headers | {"X-Access-Code": "wrong"}).status_code == 401
        assert client.get("/v1/conversations", headers=headers | {"X-Access-Code": "sesame"}).status_code == 200
        assert client.get("/healthz").json()["access_code_required"] is True


def test_memory_model_setting_defaults_to_the_main_model() -> None:
    from booking_agent.config import Settings, choose_memory_model
    from booking_agent.llm import ScriptedChatModel

    main = ScriptedChatModel()
    assert choose_memory_model(Settings(_env_file=None), main) is main
    assert choose_memory_model(Settings(_env_file=None, memory_model="gpt-6-luna"), main) is main


def test_a_different_memory_model_gets_its_own_client() -> None:
    from pydantic import SecretStr

    from booking_agent.config import Settings, choose_memory_model
    from booking_agent.llm import ScriptedChatModel

    settings = Settings(_env_file=None, llm_api_key=SecretStr("key"), memory_model="small-memory-model")
    memory_model = choose_memory_model(settings, ScriptedChatModel())
    assert memory_model.model_name == "small-memory-model"
