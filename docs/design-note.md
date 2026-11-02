# Design note

The schema and APIs are in docs/system-design.md.

## State model

State is rows, not a blob. A conversation's position is derived from them.

| Table | Holds |
|---|---|
| `conversations` | messages (json, with cards), summary, summarized_count, version |
| `searches` | criteria and results snapshot; the newest one is current |
| `quotes` | one exact action (book, cancel, reschedule) with provider, slot, price, hold, version, status |
| `jobs` | queued provider work: book, cancel, reschedule, check, research, memory_update |
| `bookings` | written only after the provider verifies the booking |
| `user_profiles` | addresses, phones, memory (json object, one key per fact) |

Position, from the newest rows:

- Search missing a field: the model asks for it, with a code-built summary of what is known and missing.
- Search with results, no current quote: offers card. Quote shown: waiting for Confirm or Not now.
- Quote confirmed: waiting for the worker. A correction copies the newest search with one field changed.

## Approval enforcement

- The quote is the approval record. It holds action, provider, slot, price and version.
- Confirm sends `quote_id` and `version`. Typed text never confirms. The model has no tool that books,
  cancels or moves anything.
- Confirm sets the quote to `confirmed` and inserts the job in one transaction. If it fails, nothing is written,
  the quote stays `shown` and the API returns 503.
- A re-hold that changes price or time raises the quote version and sets it back to `shown`. A confirm for an
  older version does nothing.
- The worker checks for a double booking (same user, provider, start) and the daily cap before writing.
  A unique index on booked rows is the backstop.

## Idempotency and recovery

- The provider key is `f"{kind}:{job.id}"`. One job has one key. Retries (timeout, crash, lease expiry) reuse
  the job and the key. Backoff is min(5 * 2^(attempts-1), 300) s, up to 5 attempts, with a 60 s lease.
- On a timeout the worker calls `get_booking_status(key)` before retrying or reporting. A booking row is
  inserted only after the provider says booked.
- A new key is used only after a verified "not applied": the job is `failed` and a new job is created.
- If the status is unknown after all attempts, the job is `dead`, the quote stays `confirmed`, a `check` job
  keeps asking with the same key, and the user gets a message. The ERROR log line is `job_dead`.
- Slot failure (hold unavailable, re-hold failure, `slot_taken`): the quote is marked failed, the user is told
  at once, and a `research` job runs the original search plus four relaxed variants in parallel (any time,
  date_to + 7 days, every day, price x 1.25 when a budget is set). The original wins if it has results,
  then the variants in that order. Failed slots are excluded.
- A restart loses nothing: jobs and quotes are rows, and expired leases are claimed again.

## Memory

- Memory is the JSON `user_profiles.memory`: `{key: {value, source, confirmed, asked?}}`. Only confirmed keys go
  to the model, as PERSONAL_MEMORY.
- After each user turn a `memory_update` job asks the memory model (current turn only) for set and remove
  operations. A stated set is confirmed. An inferred set is unconfirmed and never overwrites a confirmed key.
- Code validates the operations (key format, value size, 30 keys) and ignores a confirmed flag from the model.
- An unconfirmed key is asked about once. Yes confirms it (source kept). No removes it. Forget removes the key
  and, in the same job, from the chat summary; the state line says it was forgotten here, ask again. Chat-only
  details stay in the chat.
- Confirmed keys pre-fill area, budget, earliest time and preferred provider (the technician is stored too) of a new search.
- Context: system prompt, PERSONAL_MEMORY, summary, state line, last 8 turns. The job folds older messages in.

## Observability

- One trace per turn and one per job attempt, written as JSONL.
- Spans cover model calls (tokens, cost), tool calls (sanitised args and results, latency) and state transitions.
- Provider text appears only as a fingerprint with an injection flag. Keys and phone numbers are redacted.
- Two alert rules on ERROR log lines: `enqueue_failed` (a confirm could not be saved) and `job_dead` (a job ran
  out of attempts). Azure Monitor rules, severity 1, no action group yet, so they show in the portal only.
- The admin page shows traces, bookings, quotes and jobs.

## Trace excerpt

Raw file: docs/traces/timeout-after-success.jsonl (35 lines), one JSON line per event, model call or tool call.
Line numbers refer to that file.

```
13 quote_status   q-c34a4429a1 none -> shown                       hold made in code, quote card shown
16 quote_confirmed q-c34a4429a1 v1 book                            Confirm tapped with id and version
17 run_finished   queued                                         no provider call in the chat request
18 quote_status   shown -> confirmed
19 job_status     book d4d8d013 none -> pending                  inserted in the same transaction
28 write_attempt  key book:d4d8d013                              key is kind plus job id
29 tool_call      book -> unknown, error timeout                 the mock booked, the reply was lost
30 tool_call      get_booking_status same key -> booked          checked before any retry
32 booking_created bk-7ebfd3892f, provider bk-c2eb0332             row written only after verification
33 quote_status   confirmed -> done                              user told; job done at line 34
```

## What breaks first at 100x and what I would change

| What | Why | Change |
|---|---|---|
| In-process SSE bus and rate limiter | They need one replica; a second one would miss updates and count separately | Postgres LISTEN/NOTIFY or Redis |
| One worker thread | Jobs queue behind each other | Run more workers. Claims are atomic (`SKIP LOCKED` on Postgres), so this is safe |
| Provider rate limits | Search, hold and verify calls grow with users | Budget calls per provider and back off on errors |
| `conversations.messages` is one JSON list per conversation | It grows with every turn and is rewritten on each save | Cap it, or move messages to rows |
| Database round trips per turn | Several queries per turn | Same region as the database, fewer queries, a sized pool |
| Model cost and latency | 1 to 3 model calls per turn plus memory updates | Prompt caching, a cheaper memory model |

A real provider must give an authoritative booking status. If it cannot, writes stay unknown and are retried
with the same key.

## Assumptions

Readings of ambiguous points in the brief.

- "Any date" means today to today + 30 days.
- Mornings are 06-12, afternoons 12-17, evenings 17-22.
- A budget is a strict upper bound. A price above it is not quoted.
- An inferred memory key is asked about once. Yes confirms it, no removes it.
- Hold expiry comes from the provider (10 minutes in the mock). The worker re-holds under 30 s.
- One replica: the SSE bus and the rate limiter live in the process.
