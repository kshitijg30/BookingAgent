# BookingAgent

A chat agent that finds, books, cancels and reschedules home-service visits (plumbing, electrical, AC servicing,
carpentry, cleaning) against a mocked provider API. It asks only for what is missing. Nothing is sent to the
provider until the user taps Confirm on a card that shows the exact provider, slot and price.

## Quick start

Install uv (it also installs Python 3.12):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # or: brew install uv
```

Then, from the repo:

```bash
uv sync --all-extras
uv run pytest
uv run booking-agent serve
```

Set `OPENAI_API_KEY` in `.env` first (`cp .env.example .env`). Tests need no key or network. The test run
writes `reports/eval_table.md`. The app is at http://127.0.0.1:8000. The root URL redirects to the chat UI at `/ui/`.

## Using the app

- Chat: describe the job ("a plumber for my kitchen tap this week, mornings, under ₹1,500, Koramangala").
  The agent asks only for what is missing.
- Offers card: providers ordered by rating, then price. Each shows its free slots as chips. Tap a slot to
  pick it. The top 5 show first; "Show more" reveals the rest.
- Quote card: provider, slot and price, with Confirm and Not now. Typed text such as "yes" never confirms.
- After Confirm the chat says "We've taken your request." The result arrives as a live update once the
  background worker has made the provider call and the provider has verified the booking.
- If the chosen slot was just taken, the chat reports it at once and a background search offers the next
  best options. Nothing is booked until you confirm again.
- Profile and memory panel: edit your name, phones and addresses. Each memory key can be edited or deleted.
  Keys not yet confirmed are marked "Not confirmed yet"; answering yes in chat confirms them. The API also
  returns each key's source (stated or inferred), but the panel does not show it.
- Admin page: set `BA_ADMIN_CODE`, then open http://127.0.0.1:8000/admin/ and enter the code. Tabs: activity
  (turn traces), bookings and quotes, jobs (attempts and last error), users, settings, demo controls.

## Configuration

Every setting is in `.env.example` with comments.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Required to run the app |
| `BA_LLM_MODEL` | Chat model. Default `gpt-6-luna` |
| `BA_MEMORY_MODEL` | Model for memory updates. Default: the same as `BA_LLM_MODEL` |
| `BA_DATABASE_URL` | Optional Postgres URL. If unset, SQLite in `BA_DATA_DIR` |
| `BA_ACCESS_CODE` | Code the UI and API ask for (`X-Access-Code`). Set it before a public deployment |
| `BA_ADMIN_CODE` | Set to enable `/admin/` |

## Docs

- [docs/system-design.md](docs/system-design.md): requirements, schema, HTTP routes, slot failure and concurrency, one booking end to end
- [docs/design-note.md](docs/design-note.md): state model, approval, idempotency, memory, observability, scale limits
- [docs/bugs.md](docs/bugs.md): four bugs found while building, each with a regression test in `tests/test_regressions.py`
- [docs/library.md](docs/library.md): using `BookingEngine` as a library
- [docs/deploy-azure.md](docs/deploy-azure.md): deployment, Azure logs, traces and alerts
- [docs/traces/timeout-after-success.jsonl](docs/traces/timeout-after-success.jsonl): raw trace of a timeout-after-success run
- [reports/eval_table.md](reports/eval_table.md): written by the test run

## Limits

- One replica. The SSE bus and the rate limiter are in process.
- Mock provider only. A real provider needs its trust assumptions reviewed.
