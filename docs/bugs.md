# Bugs found while building

Each regression test is in `tests/test_regressions.py`.

## 1. Two users confirm the same slot

- What broke: user A and user B both tried Raj on Tuesday at 10:00.
- Why: availability belongs to the provider. A slot shown in search is a snapshot, and our side must not assume it
  is still free.
- Fix: `hold_slot` is the gate, and whoever holds first gets the slot. B's hold fails with slot_unavailable, B's
  quote is recorded `failed`, B is told at once, and a `research` job offers the next best option. A unique booked
  slot per user and one active job per quote stop double writes.
- Regression test: `test_two_users_same_slot_second_gets_alternative`

## 2. A timeout was reported as a failure

- What broke: `book()` timed out after the provider had booked (the timeout-after-success case in the brief).
- Why: reporting "failed" would make the user book again, which double-books.
- Fix: on a timeout, call `get_booking_status` with the same idempotency key (`kind:job_id`) and report only the
  verified result. If the status is unknown, retry with the same key and never a new one. After the maximum
  attempts, a `check` job keeps asking.
- Regression test: `test_timeout_after_success_is_reported_as_booked_once`

## 3. A confirmed booking was queued without its state

- What broke: a booking was queued, then nothing was booked.
- Why: while building the queue, the job was committed in its own transaction before the conversation was saved.
  When the save failed (version conflict), the worker found the quote not confirmed, treated the job as done, and
  nothing was booked.
- Fix: a transactional outbox. Confirm writes the quote as confirmed and the job in one transaction. If it fails,
  nothing is written, the API returns 503 "Couldn't take your request, please try again.", and `enqueue_failed` is
  logged.
- Regression test: `test_confirm_save_failure_leaves_no_job_and_quote_shown`

## 4. Re-holds and reschedules always failed after a reload

- What broke: a re-hold after the hold expired always returned slot_unavailable, and a reschedule always returned
  slot_conflict.
- Why: quotes read back from the database carry `start` in UTC. The provider checks the slot hour in local time,
  so 10:00 IST arrived as 04:30 and was outside the working hours.
- Fix: the provider client converts slot datetimes to the business timezone (IST) before every call that sends a
  slot (`hold_slot` and `reschedule`), so every caller is covered.
- Regression test: `test_rehold_after_expiry_sends_the_slot_in_business_timezone` and
  `test_reschedule_sends_the_new_slot_in_business_timezone`
