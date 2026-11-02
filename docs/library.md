# Embedding BookingAgent

`BookingEngine` is the typed Python API. It needs no web framework, chat model or database. It is synchronous;
call it from a worker thread in an async host. Not published to PyPI: install with `python -m pip install .` or
from a built wheel in `dist/`. `examples/headless_booking.py` runs fully offline.

```python
from booking_agent import BookingEngine, BookingRequest, ConfirmQuote

with BookingEngine(provider=my_provider) as engine:  # my_provider implements ProviderAPI
    request = BookingRequest(service="plumber", area="Koramangala")
    result = engine.propose(user_id=uid, conversation_id="c1", request=request)
    quote = result.quotes[0]  # show exactly this quote to the user
    engine.confirm(user_id=uid, conversation_id="c1", command=ConfirmQuote(quote_id=quote.id, version=quote.version))
    engine.run_jobs()  # the provider is called here, not in confirm
    bookings = engine.list_bookings(user_id=uid)
```

`user_id` must come from your authenticated principal. Each command takes a `conversation_id` and runs as one turn.

## Commands

- `search(request=)` is read-only: ranked offers, no conversation, hold or write.
- `propose(...)` searches, holds the best slot and returns it in `BookingResult.quotes`. `choose_slot(...,
  command=SelectSlot(provider_id, slot_start))` quotes another slot, only one the last search offered.
- `confirm(..., command=ConfirmQuote(quote_id, version))` approves the quote on screen and queues the write. The
  result has exit kind `queued`. A replaced quote (older version) is refused. `EnqueueFailed` means nothing was
  saved. `decline(..., quote_id=)` drops the quote.
- `cancel(..., booking_id=)` and `reschedule(..., booking_id=, request=)` return a quote. Nothing changes until
  `confirm`.
- `list_bookings(user_id=)` returns the provider's own list (the source of truth).
- `BookingResult` holds `exit`, `quotes`, `booking_ids`, `notices` and `currency`.

## Jobs

`confirm`, `cancel` and `reschedule` queue a job; the provider is called only by a worker. Use
`engine.run_jobs()` (runs the due jobs, returns the count) or `engine.job_worker().run_forever(stop_event)` in
your own thread. A timed-out write is checked with `get_booking_status` under the same key before anything else.
A failed attempt retries after 5 s, 10 s, ... up to 5 min, 5 attempts, then the job is dead and the user gets a
notice. Keep running the worker: later attempts and `check` jobs are due after a delay.

## Provider contract

`ProviderAPI` combines `AvailabilityProvider` (`search_providers`, `get_provider`), `BookingProvider`
(`list_bookings`, `hold_slot`, `book`, `get_booking_status`) and `ChangeProvider` (`cancel`, `reschedule`) from
`booking_agent.provider.contract`. Write methods take an idempotency key. Rules for an adapter:

- Return plain dicts; they are validated into typed models immediately. Prices are integer rupees.
- Raise `ProviderError(code)` or `ProviderTimeout` for vendor failures. Set finite timeouts yourself.
- Declare `ProviderCapabilities` honestly. Both flags default to false, so a `not_found` status never triggers a
  second write under a new key. Raise `ProviderError` for an unsupported call; do not fake a negative answer.

## Repositories and configuration

The default `MemoryRepository` is thread-safe and lost on exit. For restart recovery pass a durable
`Repository`, for example `Store(path_or_url)` from `booking_agent.store` (extra `sqlalchemy` or `postgres`).
Protocols are in `ports.py`. The engine closes only what it created; a closed engine raises.

`EngineConfig` sets services, areas, labels, timezone and currency. `limits=` takes `Limits`, `telemetry=`
takes `Telemetry(trace_dir=..., exporter=callable)` for JSONL files or your own span sink.
`memory_model=` takes a `ChatModel` for memory updates; without one, no memory is written.

## Install

The base install needs only Pydantic. Extras: `openai` (chat adapter), `sqlalchemy` and `postgres` (storage),
`http` (FastAPI app), `demo` (chat and web UI), `all`.

Development: `uv sync --all-extras`. `uv build` then `uv run python scripts/check_wheel.py dist` checks the wheel
in a clean minimal environment, and CI runs it.
