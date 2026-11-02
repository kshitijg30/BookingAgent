"""Admin API: activity and traces, bookings and quotes, user lookup, runtime settings, demo controls.

Protected by ``BA_ADMIN_CODE`` (header ``X-Admin-Code``), separate from the user access code. If the
code is not set, every admin route returns 404. Runtime settings are stored in ``app_settings`` and
applied at startup and on every save; API keys and the database URL are never shown or editable here.
"""

from __future__ import annotations

import dataclasses
import hmac
from collections import deque
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from booking_agent.app_state import App
from booking_agent.config import Settings
from booking_agent.domain.entities import Job
from booking_agent.provider.scenario import BookOutcome, Scenario
from booking_agent.tracing import timeline

if TYPE_CHECKING:
    from booking_agent.agent import Agent


class DemoControls(Protocol):
    """Optional demo-control port, separate from the operational booking provider."""

    scenario: Scenario
    forced_book: deque[BookOutcome]
    forced_status_timeouts: int

    def provider_cancel(self, booking_id: str) -> bool: ...


class RuntimeSettings(BaseModel):
    llm_model: str = "gpt-6-luna"
    system_prompt: str = Field("", max_length=20_000)  # empty: use prompts.toml
    llm_reasoning_effort: Literal["", "none", "low", "medium", "high"] = "none"
    tool_calls_per_turn: int = Field(12, ge=3, le=50)
    wall_clock_s: float = Field(25.0, ge=5, le=120)
    bookings_per_user_per_day: int = Field(5, ge=1, le=100)
    messages_per_minute: int = Field(20, ge=1, le=600)
    book_success: float = Field(0.70, ge=0, le=1)
    book_slot_taken: float = Field(0.15, ge=0, le=1)
    book_timeout: float = Field(0.15, ge=0, le=1)  # half of timeouts actually booked
    quote_drift_rupees: int = Field(0, ge=0, le=5_000)
    cancel_first_call_503: bool = True

    @model_validator(mode="after")
    def _mix(self) -> RuntimeSettings:
        if abs(self.book_success + self.book_slot_taken + self.book_timeout - 1) > 0.001:
            raise ValueError("book_success + book_slot_taken + book_timeout must add up to 1")
        return self

    @classmethod
    def from_settings(cls, s: Settings) -> RuntimeSettings:
        return cls(llm_model=s.llm_model, llm_reasoning_effort=s.llm_reasoning_effort or "")


def apply_runtime(cfg: RuntimeSettings, *, base: Settings, agent: Agent, provider: DemoControls | None) -> None:
    """Apply the model, limits and mock behaviour from runtime settings. Raises ValueError if invalid."""
    if agent.model is not None and (reconfigure := getattr(agent.model, "with_model", None)) is not None:
        s = base.model_copy(
            update={"llm_model": cfg.llm_model, "llm_reasoning_effort": cfg.llm_reasoning_effort or None}
        )
        try:
            agent.model = reconfigure(s.llm_model, s.llm_reasoning_effort)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
    limits = dataclasses.replace(
        agent.limits,
        tool_calls_per_turn=cfg.tool_calls_per_turn,
        wall_clock_s=cfg.wall_clock_s,
        bookings_per_user_per_day=cfg.bookings_per_user_per_day,
        messages_per_minute=cfg.messages_per_minute,
    )
    agent.apply_settings(prompts=agent.default_prompts.with_system(cfg.system_prompt), limits=limits)
    if provider is not None:
        provider.scenario = dataclasses.replace(
            provider.scenario,
            book_mix=(cfg.book_success, cfg.book_slot_taken, cfg.book_timeout / 2),
            quote_drift_rupees=cfg.quote_drift_rupees,
            cancel_first_call_503=cfg.cancel_first_call_503,
        )


def stored_runtime(base: Settings, agent: Agent) -> RuntimeSettings:
    """The saved runtime settings, or the environment's defaults if none were saved."""
    stored = agent.store.get_setting("runtime")
    return RuntimeSettings.model_validate(stored) if stored else RuntimeSettings.from_settings(base)


def load_runtime(base: Settings, agent: Agent, provider: DemoControls | None) -> None:
    """Apply the saved runtime settings at startup."""
    apply_runtime(stored_runtime(base, agent), base=base, agent=agent, provider=provider)


class NextBook(BaseModel):
    outcome: BookOutcome
    count: int = Field(1, ge=1, le=10)


class StatusTimeouts(BaseModel):
    count: int = Field(1, ge=0, le=10)


class ProviderCancel(BaseModel):
    booking_id: str = Field(min_length=3, max_length=64)


def require_admin(app: App, x_admin_code: Annotated[str, Header()] = "") -> None:
    code = app.settings.admin_code.get_secret_value() if app.settings.admin_code else None
    if code is None:
        raise HTTPException(404, "admin is disabled; set BA_ADMIN_CODE")
    if not hmac.compare_digest(x_admin_code, code):
        raise HTTPException(401, "admin code required")


router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


@router.get("/overview")
def overview(app: App) -> dict[str, Any]:
    return {
        "counts": app.agent.store.counts(),
        "activity": app.agent.telemetry.summaries(60),
        "demo": {
            "forced_book": list(app.demo.forced_book) if app.demo is not None else [],
            "forced_status_timeouts": app.demo.forced_status_timeouts if app.demo is not None else 0,
        },
    }


@router.get("/traces/{trace_id}")
def trace(trace_id: str, app: App) -> dict[str, Any]:
    spans = app.agent.telemetry.load(trace_id)
    if not spans:
        raise HTTPException(404, "trace not found (traces are kept since the last restart)")
    return {"trace_id": trace_id, "timeline": timeline(spans), "spans": spans}


@router.get("/bookings")
def bookings(app: App) -> dict[str, Any]:
    return {"bookings": app.agent.store.recent_bookings(50), "quotes": app.agent.store.recent_quotes(50)}


@router.get("/jobs")
def jobs(app: App, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
    """Recent background jobs, newest first."""
    return {"jobs": [_job_json(job) for job in app.agent.store.recent_jobs(limit)]}


def _job_json(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_at": job.run_at.isoformat(),
        "locked_until": job.locked_until.isoformat() if job.locked_until else None,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "payload": job.payload,
    }


@router.get("/users/{user_id}")
def user(user_id: str, app: App) -> dict[str, Any]:
    """Everything stored for one user, plus the provider's own list of their bookings."""
    store = app.agent.store
    profile = store.get_profile(user_id)
    answer = app.agent.provider_client.list_bookings(user_id)
    return {
        "profile": {
            "name": profile.name,
            "phones": list(profile.phones),
            "addresses": [address.model_dump() for address in profile.addresses],
        },
        "memory": profile.memory,
        "conversations": [
            {"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()}
            for c in store.list_conversations(user_id)
        ],
        "bookings": [booking.model_dump(mode="json") for booking in store.list_bookings_for_user(user_id)],
        "provider_bookings": [b.model_dump(mode="json") for b in answer.value or []] if answer.ok else None,
    }


@router.get("/settings")
def get_settings(app: App) -> dict[str, Any]:
    s = app.settings
    return {
        "settings": stored_runtime(s, app.agent).model_dump(),
        "info": {
            "llm_key": "set" if s.api_key() else "not set",
            "llm_base_url": s.llm_base_url,
            "database": "postgres" if s.database_url else "local sqlite",
            "access_code": "set" if s.access_code else "not set",
            "trace_dir": str(s.trace_dir),
        },
    }


@router.put("/settings")
def put_settings(cfg: RuntimeSettings, app: App) -> dict[str, Any]:
    try:
        apply_runtime(cfg, base=app.settings, agent=app.agent, provider=app.demo)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    app.agent.store.put_setting("runtime", cfg.model_dump(), app.agent.clock.now())
    return {"saved": True, "settings": cfg.model_dump()}


@router.post("/demo/next-book")
def next_book(body: NextBook, app: App) -> dict[str, Any]:
    _demo(app).forced_book.extend([body.outcome] * body.count)
    return {"forced_book": list(_demo(app).forced_book)}


@router.post("/demo/status-timeouts")
def status_timeouts(body: StatusTimeouts, app: App) -> dict[str, int]:
    _demo(app).forced_status_timeouts = body.count
    return {"forced_status_timeouts": _demo(app).forced_status_timeouts}


@router.post("/demo/clear")
def clear(app: App) -> dict[str, bool]:
    _demo(app).forced_book.clear()
    _demo(app).forced_status_timeouts = 0
    return {"cleared": True}


@router.post("/demo/provider-cancel")
def provider_cancel(body: ProviderCancel, app: App) -> dict[str, Any]:
    """Accepts our booking id or the provider's own id; the provider's table is keyed by its own."""
    booking = app.agent.store.get_booking(body.booking_id)
    provider_id = booking.provider_booking_id if booking and booking.provider_booking_id else body.booking_id
    if not _demo(app).provider_cancel(provider_id):
        raise HTTPException(404, "booking not found")
    return {"cancelled_by_provider": body.booking_id, "replacements_queued": app.agent.scan_provider_cancellations()}


@router.post("/demo/scan")
def scan(app: App) -> dict[str, int]:
    return {"replacements_queued": app.agent.scan_provider_cancellations()}


def _demo(app: App) -> DemoControls:
    if app.demo is None:
        raise HTTPException(404, "demo controls are disabled")
    return app.demo
