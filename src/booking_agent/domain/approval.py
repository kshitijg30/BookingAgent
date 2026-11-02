"""What a quote needs before it can be confirmed, and the provider idempotency key of a job.

The approval record is the ``Quote`` itself: the user confirms one quote id and version, and a quote that
changed price or time gets a new version. There is no separate approval object.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from booking_agent.domain.entities import Quote

MIN_LEAD = timedelta(minutes=30)  # a slot that starts sooner than this is not offered or confirmed


def idempotency_key(kind: str, job_id: str) -> str:
    """The key sent to the provider. One job is one key, so retries of a job repeat it."""
    return f"{kind}:{job_id}"


def confirm_refusal(current: Quote | None, quote_id: str, version: int, now: datetime) -> str | None:
    """Why a confirm for (quote_id, version) must do nothing, or None if it may go ahead."""
    if current is None or current.id != quote_id:
        return "stale"
    if current.status == "confirmed":
        return "already_confirmed"
    if current.status != "shown" or current.version != version:
        return "stale"
    if current.action != "cancel" and current.start - now < MIN_LEAD:
        return "too_soon"
    return None
