"""Property tests for the approval rules: the quote version check and the one-job-one-key rule.

- a confirm is accepted only for the current quote, at its current version, while it is shown and not too soon;
- the idempotency key is a function of (kind, job id) and nothing else, so retries of one job repeat it;
- a quote never has two active jobs, and a retried job keeps its id (so its key).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booking_agent.domain.entities import Quote, QuoteStatus, idempotency_key
from booking_agent.domain.planning import MIN_LEAD, confirm_refusal
from booking_agent.errors import RequestConflict
from booking_agent.memory_repository import MemoryRepository

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@st.composite
def quotes(draw: st.DrawFn, status: QuoteStatus = "shown") -> Quote:
    start = NOW + MIN_LEAD + timedelta(minutes=draw(st.integers(0, 60 * 24 * 14)))
    return Quote(
        id=draw(st.sampled_from(["q-1", "q-2"])),
        conversation_id="c1",
        user_id="u1",
        action=draw(st.sampled_from(["book", "reschedule"])),
        provider_id=draw(st.sampled_from(["p1", "p2"])),
        provider_name="Provider",
        service="plumber",
        area="Koramangala",
        start=start,
        end=start + timedelta(hours=1),
        price=draw(st.integers(100, 5000)),
        version=draw(st.integers(1, 5)),
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


@given(quotes())
def test_the_exact_id_and_version_of_a_shown_quote_is_accepted(quote: Quote) -> None:
    assert confirm_refusal(quote, quote.id, quote.version, NOW) is None


@given(quotes(), st.integers(1, 5))
def test_another_version_is_refused(quote: Quote, version: int) -> None:
    if version != quote.version:
        assert confirm_refusal(quote, quote.id, version, NOW) == "stale"


@given(quotes())
def test_another_quote_id_or_no_quote_is_refused(quote: Quote) -> None:
    assert confirm_refusal(quote, quote.id + "x", quote.version, NOW) == "stale"
    assert confirm_refusal(None, quote.id, quote.version, NOW) == "stale"


@given(quotes(), st.sampled_from(["done", "failed", "declined", "expired"]))
def test_a_quote_that_is_not_shown_cannot_be_confirmed(quote: Quote, status: QuoteStatus) -> None:
    assert confirm_refusal(quote.model_copy(update={"status": status}), quote.id, quote.version, NOW) == "stale"


@given(quotes())
def test_a_second_confirm_of_a_confirmed_quote_is_reported_as_already_confirmed(quote: Quote) -> None:
    confirmed = quote.model_copy(update={"status": "confirmed"})
    assert confirm_refusal(confirmed, quote.id, quote.version, NOW) == "already_confirmed"


@given(quotes(), st.integers(0, 29))
def test_a_slot_starting_within_the_minimum_lead_is_refused(quote: Quote, minutes_left: int) -> None:
    soon = quote.model_copy(update={"start": NOW + timedelta(minutes=minutes_left)})
    assert confirm_refusal(soon, soon.id, soon.version, NOW) == "too_soon"


@given(st.sampled_from(["book", "cancel", "reschedule"]), st.text(min_size=1, max_size=20))
def test_the_idempotency_key_depends_only_on_kind_and_job_id(kind: str, job_id: str) -> None:
    assert idempotency_key(kind, job_id) == idempotency_key(kind, job_id) == f"{kind}:{job_id}"
    assert idempotency_key(kind, job_id) != idempotency_key(kind, job_id + "x")


@given(st.lists(st.sampled_from(["enqueue", "retry", "fail", "complete", "dead"]), min_size=1, max_size=25))
def test_one_active_job_per_quote_and_retries_keep_the_key(steps: list[str]) -> None:
    store = MemoryRepository()
    last = None
    for step in steps:
        active = [job for job in store.jobs_for_quote("q-1") if job.status in ("pending", "running")]
        if step == "enqueue":
            if active:
                with pytest.raises(RequestConflict):
                    store.enqueue_job(kind="book", quote_id="q-1", payload={}, now=NOW)
            else:
                last = store.enqueue_job(kind="book", quote_id="q-1", payload={}, now=NOW)
        elif active:
            job = active[0]
            if step == "retry":
                key_before = idempotency_key(job.kind, job.id)
                store.fail_job(job.id, error="timeout", now=NOW, retry_at=NOW)
                again = store.claim_job(now=NOW, lease=timedelta(seconds=60))
                assert again is not None and again.id == job.id
                assert idempotency_key(again.kind, again.id) == key_before
            elif step == "fail":
                store.mark_job_failed(job.id, error="not applied", now=NOW)
            elif step == "complete":
                store.complete_job(job.id, now=NOW)
            else:
                store.fail_job(job.id, error="gave up", now=NOW, retry_at=None)
        assert len([j for j in store.jobs_for_quote("q-1") if j.status in ("pending", "running")]) <= 1
    keys = [idempotency_key(job.kind, job.id) for job in store.jobs_for_quote("q-1")]
    assert len(keys) == len(set(keys)) and (last is None or idempotency_key("book", last.id) in keys)
