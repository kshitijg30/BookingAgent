"""HTTP API. User identity comes from the X-User-Id header (a local stand-in for real auth)."""

from __future__ import annotations

import hmac
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self, cast

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, model_validator
from starlette.types import Send

from booking_agent import __version__
from booking_agent.admin import DemoControls, load_runtime
from booking_agent.admin import router as admin_router
from booking_agent.app_state import App, AppState, UserId, notify_conversation_updated
from booking_agent.config import DEFAULT_CATALOG, ServiceCatalog, Settings, build
from booking_agent.conversation_view import conversation_view
from booking_agent.domain.entities import Address, Conversation, QuoteStatus, UserProfile
from booking_agent.domain.memory import MAX_KEYS, public_memory, remove_key, set_key, valid_key, valid_value
from booking_agent.errors import AccessDenied, EnqueueFailed, RequestConflict
from booking_agent.flow.memory import change_memory, forget_in_chat
from booking_agent.live_updates import Subscription
from booking_agent.model import ChatModel
from booking_agent.ports import Repository
from booking_agent.provider.client import ProviderAPI

if TYPE_CHECKING:
    from booking_agent.agent import Agent
    from booking_agent.flow.context import CardAction

WEB_DIR = Path(__file__).parent / "web"
CONVERSATION_NOT_FOUND = "conversation not found"
ENQUEUE_FAILED_MESSAGE = "Couldn't take your request, please try again."

log = logging.getLogger("booking_agent.api")

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class PickSlot(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)
    slot_start: datetime


class CardActionIn(BaseModel):
    """A button tapped on a quote card. Confirm needs the card's version; decline does not."""

    type: Literal["confirm", "decline"]
    quote_id: str = Field(min_length=1, max_length=128)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def confirm_names_a_version(self) -> Self:
        if self.type == "confirm" and self.version is None:
            raise ValueError("confirm needs the card version")
        return self

    def to_card_action(self) -> CardAction:
        from booking_agent.flow.context import CardAction

        return CardAction(kind=self.type, quote_id=self.quote_id, version=self.version)


class ChatIn(BaseModel):
    """Exactly one of a typed message, a calendar pick, or a card action."""

    conversation_id: str = Field(min_length=1, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    pick_slot: PickSlot | None = None
    action: CardActionIn | None = None

    @model_validator(mode="after")
    def exactly_one_input(self) -> Self:
        given = [value for value in (self.message, self.pick_slot, self.action) if value is not None]
        if len(given) != 1:
            raise ValueError("send exactly one of message, pick_slot or action")
        return self


class AddressIn(BaseModel):
    label: ShortText = ""
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ProfileIn(BaseModel):
    name: ShortText | None = None
    phones: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=10
    )
    addresses: list[AddressIn] = Field(default_factory=list, max_length=10)


class MemoryIn(BaseModel):
    value: Any


router = APIRouter(prefix="/v1")


def _own_conversation(app: AppState, conversation_id: str, user_id: str) -> Conversation:
    """The conversation, if it belongs to this user. Someone else's looks the same as a missing one."""
    conversation = app.agent.store.get_conversation(conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise HTTPException(404, CONVERSATION_NOT_FOUND)
    return conversation


@router.post("/chat")
def chat(body: ChatIn, app: App, x_user_id: UserId) -> dict[str, Any]:
    """One chat turn: a typed message, a tapped slot, or a card button."""
    pick = (body.pick_slot.provider_id, body.pick_slot.slot_start) if body.pick_slot else None
    try:
        result = app.agent.handle(
            conversation_id=body.conversation_id,
            user_id=x_user_id,
            text=body.message or "",
            pick_slot=pick,
            card_action=body.action.to_card_action() if body.action else None,
        )
    except AccessDenied as error:
        raise HTTPException(404, CONVERSATION_NOT_FOUND) from error
    except EnqueueFailed as error:
        log.error(
            "enqueue_failed quote_id=%s user_id=%s conversation_id=%s", error.quote_id, x_user_id, body.conversation_id
        )
        raise HTTPException(503, ENQUEUE_FAILED_MESSAGE) from error
    return result.to_dict()


@router.get("/catalog")
def catalog(app: App) -> dict[str, Any]:
    return app.agent.deps.catalog.model_dump()


@router.get("/conversations")
def list_conversations(app: App, x_user_id: UserId) -> dict[str, Any]:
    rows = app.agent.store.list_conversations(x_user_id)
    return {"conversations": [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, app: App, x_user_id: UserId) -> dict[str, Any]:
    """Saved messages, each card filled in from the current offers, quote or booking."""
    conversation = _own_conversation(app, conversation_id, x_user_id)
    return conversation_view(conversation, app.agent.store)


HEARTBEAT_S = 15.0


class EventStreamResponse(StreamingResponse):
    """A streaming response that releases its subscription however the stream ends.

    A sync generator is only closed when it is garbage collected, which can be late, and it cannot be
    closed while a worker thread is waiting inside it. So the response drops the subscription itself
    when the client disconnects or the stream is cancelled.
    """

    def __init__(self, subscription: Subscription) -> None:
        self.subscription = subscription
        super().__init__(
            _event_frames(subscription),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            self.subscription.close()


def _event_frames(subscription: Subscription) -> Iterator[str]:
    """SSE frames for one subscription. Closing the generator also releases it."""
    with subscription:
        yield "retry: 3000\n\n"
        while True:
            event = subscription.next_event(HEARTBEAT_S)
            if event is None:
                yield ": ping\n\n"
            else:
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@router.get("/conversations/{conversation_id}/events")
def conversation_events(conversation_id: str, app: App, x_user_id: UserId) -> StreamingResponse:
    """Server-sent events for one conversation, so the browser sees agent messages saved by the worker.

    Needs the same headers as the other conversation routes. A conversation that belongs to another user
    is a 404; one that does not exist yet can be subscribed to, since the first message creates it.

    Each update is one frame with ``event: conversation.updated`` and a JSON ``data`` line of the form
    ``{"type": "conversation.updated", "conversation_id": str, "trace_id": str | None, "exit": str}``,
    where ``exit`` is the exit kind of the saved reply. The stream starts with ``retry: 3000`` and sends a
    ``: ping`` comment every 15 seconds. Events are not replayed; reload the conversation after an event.
    """
    conversation = app.agent.store.get_conversation(conversation_id)
    if conversation is not None and conversation.user_id != x_user_id:
        raise HTTPException(404, CONVERSATION_NOT_FOUND)
    return EventStreamResponse(app.updates.subscribe(conversation_id))


@router.get("/bookings")
def bookings(app: App, x_user_id: UserId) -> dict[str, Any]:
    """The bookings this agent made for the user, newest first."""
    rows = sorted(app.agent.store.list_bookings_for_user(x_user_id), key=lambda row: row.created_at, reverse=True)
    return {"bookings": [row.model_dump(mode="json") for row in rows]}


@router.get("/quotes")
def quotes(
    app: App,
    x_user_id: UserId,
    status: QuoteStatus | None = None,
    service: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """The user's quotes, newest first."""
    rows = app.agent.store.list_quotes_for_user(x_user_id, status=status, service=service)
    return {"quotes": [row.model_dump(mode="json") for row in rows]}


@router.post("/scan")
def scan(app: App, x_user_id: UserId) -> dict[str, int]:
    if not app.settings.demo_mode:
        raise HTTPException(403, "Use the administrative scanner")
    return {"queued": app.agent.scan_provider_cancellations()}


def _profile_json(profile: UserProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "phones": list(profile.phones),
        "addresses": [{"label": a.label, "address": a.address} for a in profile.addresses],
    }


@router.get("/profile")
def get_profile(app: App, x_user_id: UserId) -> dict[str, Any]:
    return _profile_json(app.agent.store.get_profile(x_user_id))


@router.put("/profile")
def put_profile(body: ProfileIn, app: App, x_user_id: UserId) -> dict[str, Any]:
    store = app.agent.store
    with store.coordination_lock(f"user:{x_user_id}"), store.tx():
        profile = store.get_profile(x_user_id).model_copy(
            update={
                "name": body.name or None,
                "phones": tuple(body.phones),
                "addresses": tuple(Address(label=a.label, address=a.address) for a in body.addresses),
            }
        )
        return _profile_json(store.save_profile(profile, now=app.agent.clock.now()))


@router.get("/memory")
def get_memory(app: App, x_user_id: UserId) -> dict[str, Any]:
    return {"memory": public_memory(app.agent.store.get_profile(x_user_id).memory)}


@router.put("/memory/{key}")
def put_memory(key: str, body: MemoryIn, app: App, x_user_id: UserId) -> dict[str, Any]:
    """The user sets or edits a key. A user edit counts as stated, so it is confirmed."""
    if not valid_key(key) or not valid_value(body.value):
        raise HTTPException(422, "invalid key or value")
    store = app.agent.store
    with store.coordination_lock(f"user:{x_user_id}"), store.tx():
        if key not in store.get_profile(x_user_id).memory and len(store.get_profile(x_user_id).memory) >= MAX_KEYS:
            raise HTTPException(409, "memory is full; delete a key first")
        memory = change_memory(
            store, x_user_id, lambda m: set_key(m, key, body.value, stated=True), app.agent.clock.now()
        )
    entry: dict[str, Any] = public_memory(memory)[key]
    return entry


@router.delete("/memory/{key}")
def delete_memory(key: str, app: App, x_user_id: UserId, conversation_id: str | None = None) -> dict[str, str]:
    """Remove a key. With ``conversation_id`` (the user's own chat) it is also listed as forgotten in that chat."""
    store = app.agent.store
    with store.coordination_lock(f"user:{x_user_id}"), store.tx():
        if key not in store.get_profile(x_user_id).memory:
            raise HTTPException(404, "memory not found")
        change_memory(store, x_user_id, lambda m: remove_key(m, key), app.agent.clock.now())
        chat = store.get_conversation(conversation_id) if conversation_id else None
        if chat is not None and chat.user_id == x_user_id:
            forget_in_chat(store, chat.id, [key])
    return {"deleted": key}


def create_app(
    settings: Settings | None = None,
    *,
    agent: Agent | None = None,
    model: ChatModel | None = None,
    provider: ProviderAPI | None = None,
    repository: Repository | None = None,
    authenticate: Callable[[Request], str] | None = None,
    catalog: ServiceCatalog = DEFAULT_CATALOG,
    run_workers: bool = True,
) -> FastAPI:
    """Build the app. A ready-made ``agent`` skips ``build``; ``run_workers=False`` starts no threads."""
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    owns_provider = provider is None and agent is None
    if agent is None:
        agent, provider = build(settings, model=model, provider=provider, repository=repository, catalog=catalog)
    app = FastAPI(title="BookingAgent", version=__version__, lifespan=_run_background_workers)
    demo = cast(DemoControls, provider) if settings.demo_mode and hasattr(provider, "forced_book") else None
    app.state.booking = AppState(
        settings=settings,
        agent=agent,
        provider=provider,
        demo=demo,
        authenticate=authenticate,
        owns_store=repository is None and agent is None,
        owns_provider=owns_provider,
        run_workers=run_workers,
    )
    load_runtime(settings, agent, demo)

    @app.exception_handler(RequestConflict)
    async def request_conflict(request: Request, error: RequestConflict) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(AccessDenied)
    async def access_denied(request: Request, error: AccessDenied) -> JSONResponse:
        """Someone else's conversation looks the same as a missing one."""
        return JSONResponse({"detail": CONVERSATION_NOT_FOUND}, status_code=404)

    code = settings.access_code.get_secret_value() if settings.access_code else None

    @app.middleware("http")
    async def access_gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """The user API needs the access code when one is set. The admin API has its own code."""
        needs_code = code is not None and request.url.path.startswith("/v1/")
        if needs_code and not hmac.compare_digest(request.headers.get("x-access-code", ""), code or ""):
            return JSONResponse({"detail": "access code required"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "model": settings.llm_model, "access_code_required": bool(code)}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    app.include_router(router)
    app.include_router(admin_router)
    app.mount("/admin", StaticFiles(directory=WEB_DIR / "admin", html=True), name="admin")
    app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")
    return app


def _watch_provider_cancellations(state: AppState, stop: threading.Event) -> None:
    while not stop.wait(state.settings.watcher_interval_s):
        try:
            if found := state.agent.scan_provider_cancellations():
                log.info("queued %d replacement search(es) after provider cancellations", found)
        except Exception:
            log.exception("watcher failed")


def _close_owned(state: AppState) -> None:
    if state.owns_store:
        state.agent.store.close()
    if state.owns_provider and (close := getattr(state.provider, "close", None)) is not None:
        close()
    state.agent.telemetry.shutdown()


@asynccontextmanager
async def _run_background_workers(app: FastAPI) -> AsyncIterator[None]:
    """Two background threads for the app's lifetime.

    - The job worker runs queued jobs (provider writes, checks, memory updates) and tells open event
      streams when a worker turn saved a new agent message.
    - The watcher checks for provider cancellations every ``watcher_interval_s``.
    """
    state: AppState = app.state.booking
    if not state.run_workers:
        yield
        return
    from booking_agent.jobs import JOB_HANDLERS
    from booking_agent.jobs.worker import JobWorker

    stop = threading.Event()
    job_worker = JobWorker(
        state.agent.deps,
        state.agent.runner,
        JOB_HANDLERS,
        on_session_updated=lambda conversation_id, trace_id, exit_kind: notify_conversation_updated(
            state, conversation_id, trace_id, exit_kind
        ),
    )
    state.agent.runner.on_jobs_queued = job_worker.wake
    state.agent.on_conversation_updated = lambda conversation_id, trace_id, exit_kind: notify_conversation_updated(
        state, conversation_id, trace_id, exit_kind
    )
    jobs_thread = threading.Thread(target=job_worker.run_forever, args=(stop,), name="job-worker", daemon=True)
    watcher_thread = threading.Thread(
        target=_watch_provider_cancellations, args=(state, stop), name="provider-cancellation-watcher", daemon=True
    )
    jobs_thread.start()
    watcher_thread.start()
    try:
        yield
    finally:
        stop.set()
        job_worker.wake()
        jobs_thread.join(timeout=5)
        watcher_thread.join(timeout=5)
        if not jobs_thread.is_alive() and not watcher_thread.is_alive():
            _close_owned(state)


def app_factory() -> FastAPI:
    return create_app()
