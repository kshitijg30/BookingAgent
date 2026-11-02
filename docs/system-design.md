# BookingAgent system design

## What it does

A chat agent that finds, books, cancels and reschedules home-service visits against a provider API.
It writes to the provider only after the user taps Confirm on a card, and reports a booking only after the provider verifies it.

## Requirements

Functional:
- Ask only for what is missing (service, area, dates, budget). Never re-ask a known fact. A correction keeps the other details.
- Show offers (rating desc, price asc, top 5 first), let the user pick a slot, hold it, show a quote card with Confirm and Not now.
- Confirm and decline by card button only. Typed text never confirms.
- Cancel and reschedule an existing booking, each with its own quote and Confirm.
- Remember, show, correct and forget preferences. Inferred facts are used only after the user confirms them.
- When a provider cancels a booking, find a replacement and book it only after a new Confirm.

Non-functional:
- No double booking and no unverified success.
- Provider writes go through a job queue, so a slow provider never blocks the chat request.
- Caps: model_calls_per_turn 3, tool_calls_per_turn 12, wall_clock_s 25, turns_per_session 40, bookings_per_user_per_day 5, messages_per_minute 20. Every exit ends with a message to the user.
- One trace per turn and per job attempt: state changes, model calls, tool name and args, sanitised results, latency. No secrets.
- One replica: the SSE bus and the rate limiter live in the process. Alerts on ERROR lines `enqueue_failed` and `job_dead` (see docs/deploy-azure.md).

Out of scope: real providers, payments, multiple replicas, search caching, WebSockets (POST plus SSE is used).

## Data model

```
conversations: id, user_id, title, messages (json), summary, summarized_count, version, created_at, updated_at
searches: id, conversation_id, criteria (json), results (json), created_at
quotes: id, conversation_id, user_id, search_id, action (book | cancel | reschedule), booking_id, provider_id, provider_name, service, area, start, end, price, hold_id, hold_expires_at, version, status (shown | confirmed | done | failed | declined | expired), confirmed_at, created_at, updated_at
jobs: id, kind (book | cancel | reschedule | check | memory_update | research), quote_id, payload (json), status (pending | running | done | failed | dead), attempts, max_attempts, run_at, locked_until, last_error, created_at, updated_at
bookings: id, user_id, conversation_id, quote_id, provider_id, provider_name, service, area, start, end, price, status (booked | cancelled | cancelled_by_provider), provider_booking_id, created_at, updated_at
user_profiles: user_id, name, phones (json list), addresses (json list of {label, address}), memory (json object), updated_at
```

- `messages` is a list of `{role, text, card, at}`. Cards hold ids only (offers, quote, confirmation, question, notice); the UI reads the current status. Turn count is the number of user messages. Worker results are appended as agent messages.
- `summary` is a short text of older messages. `summarized_count` is how many messages it covers.
- The quote is the approval record: one exact action, provider, slot, price and version. The provider is the source of truth for availability and booking status.
- A booking row is created only after the provider verifies it. Unique on (user_id, provider_id, start) where status is booked.

PERSONAL_MEMORY (`user_profiles.memory`), one key per fact:

```json
{"area": {"value": "Koramangala", "source": "stated", "confirmed": true},
 "earliest_time": {"value": "10:00", "source": "inferred", "confirmed": false, "asked": true},
 "preferred_providers": {"plumber": {"provider": "Iyer Plumbing", "technician": "Ravi"}}}
```

`searches.criteria` (a correction copies the newest search with the changed fields; max_price is paise):

```json
{"service": "plumber", "area": "Indiranagar", "date_from": "2026-11-02", "date_to": "2026-11-08",
 "any_date": false, "days": ["mon", "tue", "wed", "thu", "fri"], "times": [{"from": "06:00", "to": "12:00"}],
 "max_price": 150000, "no_budget": false, "provider_id": null, "replaces_booking_id": null}
```

Missing, so the model asks: service, area, dates (no date_from and not any_date), budget (no max_price and not no_budget).
Mornings are 06-12, afternoons 12-17, evenings 17-22. Any date means today to today + 30.

## How a booking flows

1. The user sends a message. The model calls `find_and_propose_booking`; the search is saved and an offers card is shown.
2. The user taps a slot. Code calls `hold_slot`, which returns hold_id and the quoted price.
3. A quote (status shown) is saved and a quote card is shown.
4. The user taps Confirm with quote_id and version. One transaction sets the quote to confirmed and inserts a `book` job.
5. The user sees "We've taken your request". The chat request ends here.
6. The worker claims the job (60 s lease) and calls `book(hold_id, key)` with key `book:<job_id>`.
7. It checks `get_booking_status(key)`. Only if the provider says booked does it write the bookings row, mark the quote done and append the confirmation.
8. The page gets an SSE event and refetches the conversation.

Before step 6 the worker checks for the same user, provider and start (booked row or other confirmed quote) and the daily cap.
If the hold expires within 30 s, it re-holds. A changed price or time gives the quote version + 1 and a new Confirm; an older confirm does nothing.

## When things go wrong

- Enqueue fails at Confirm: the transaction rolls back, the quote stays shown, the API returns 503, ERROR `enqueue_failed` is logged.
- Timeout on `book`: call `get_booking_status(key)` with the same key before retrying or reporting. Booked: settle. Unknown: retry with the same key.
- Verified not booked (`not_found` or `failed`): the job ends failed and a new job with a new key is created.
- Slot taken (`hold_slot` unavailable, re-hold failure, or `book` slot_taken): the quote is failed, the user is told at once, and a `research` job is queued in the same transaction. It searches the original criteria plus four relaxed variants in parallel (any time, date_to + 7 days, every day, max_price x 1.25 when a budget is set), skipping failed slots. The original wins if it has results; then the variants in that order. Original: best slot held, quote card. Relaxed: offers card, no hold. Nothing: a question. Nothing is booked without a new Confirm.
- Retries exhausted (5 attempts, backoff min(5 * 2^(n-1), 300) s): job dead, quote stays confirmed, a `check` job keeps asking with the original key, the user gets a message, ERROR `job_dead`.
- Provider cancels a booking: a background scan marks it `cancelled_by_provider`, creates a search from the original criteria with `replaces_booking_id` and the provider excluded, and appends a message.
- `cancel` and `reschedule` jobs follow the same pattern with read-back verification; a transient 503 is retried with the same key.

Idempotency keys, one line each: Confirm is quote id + version; the provider write is `kind:job_id`; one pending or running job per quote; unique booked (user, provider, start).

## What the model sees each turn

1. System prompt (prompts.toml).
2. PERSONAL_MEMORY: only confirmed keys, as JSON.
3. The chat summary, if any.
4. A code-built state line: known criteria, missing fields, current quote or offers, failed slots, labelled addresses, keys forgotten in this chat, at most one unconfirmed key to ask about.
5. The last 8 turns. Provider review text is never sent.

How memory and forget work:
- After each user turn a `memory_update` job runs the memory model on the current turn only. It returns set and remove operations on keys.
- Code validates them (snake_case key up to 40 chars, value up to 500 chars, 30 keys) and ignores any confirmed flag from the model.
- A stated key is confirmed. An inferred key is unconfirmed and never overwrites a confirmed one. Yes confirms it (source kept), no removes it. It is asked about once.
- Confirmed keys pre-fill area, budget, earliest time and preferred provider (with technician) of a new search.
- Forget removes the key, rewrites the chat summary without it (same job), and the state line says "forgotten in this chat, ask again". Details that apply only to this chat stay in the summary and turns, not in PERSONAL_MEMORY.

## API

Auth: `X-User-Id` and `X-Access-Code` (when `BA_ACCESS_CODE` is set) on `/v1/*`; `X-Admin-Code` on `/admin/api/*`.

| Route | What it does |
|---|---|
| POST /v1/chat | conversation_id plus one of message, pick_slot, action confirm {quote_id, version}, action decline {quote_id}. Returns text, exit, trace_id, blocks |
| GET /v1/conversations, /{id}, /{id}/events | List, messages with cards at current status, SSE |
| GET /v1/bookings, /v1/quotes | The user's rows, newest first |
| GET, PUT /v1/profile | Name, phones, addresses |
| GET /v1/memory, PUT, DELETE /v1/memory/{key} | View, correct (stated, confirmed), forget |
| /admin/api/... | Overview, traces, bookings, jobs, users, settings, demo controls |

Errors: another user's conversation is 404; a failed Confirm is 503; a store conflict is 409.

## Code layout

| Package | Job |
|---|---|
| `domain/` | Models, approval, planning, memory, money, untrusted text |
| `flow/` | Gather, hold, confirm, recovery, card actions, model tools |
| `gateway/`, `jobs/` | Only path to the provider; job queue, worker, handlers |
| `store.py`, `schema.py`, `rows.py` | SQL store, schema versions, rows |
| `turn_runner.py`, `agent.py`, `engine.py` | One turn; chat entry; `BookingEngine` library API |
| `api.py`, `admin.py`, `web/` | Routes and UI |
| `provider/`, `adapters/`, `model.py` | Provider contract and mock; model client |

## Known limits

- One replica: SSE bus and rate limiter are in process.
- Mock provider only; a real one needs its trust assumptions reviewed.
- Search results are snapshots; a slot can be gone by the time it is held.
- Alerts have no action group yet, so they show in the portal only.
