"""The buttons on a quote card: confirm and decline. Decided in code, never by the model.

Confirm needs the current quote, status ``shown`` and the same version. One transaction (the turn save) sets the
quote ``confirmed`` and inserts the job. Typed text never confirms anything.
"""

from __future__ import annotations

from booking_agent.domain.approval import confirm_refusal
from booking_agent.domain.exits import ExitReason
from booking_agent.flow.context import CardAction, Deps, TurnContext
from booking_agent.render import NOTICE_CARD

STALE_CARD = "That quote has been replaced. Please confirm the current card."
ALREADY = "I already have your request and I'm waiting for the provider."
DECLINED = "Okay, I won't go ahead with that. Nothing was booked or changed."
TOO_SOON = "That slot starts too soon to book safely now. Tell me if you'd like another time."
QUEUED_NOTICE = "We've taken your request. I'll confirm here as soon as the provider responds."


def confirm_quote(deps: Deps, turn: TurnContext, quote_id: str, version: int) -> None:
    """Confirm the quote on screen if it is the one named, and queue its write."""
    refusal = confirm_refusal(turn.quote, quote_id, version, turn.now)
    quote = turn.quote
    if refusal is None and quote is not None:
        confirmed = quote.model_copy(update={"status": "confirmed", "confirmed_at": turn.now, "updated_at": turn.now})
        turn.save_quote(confirmed)
        turn.queue_job(
            quote.action,
            quote.id,
            {"quote_id": quote.id, "conversation_id": turn.conversation_id, "user_id": turn.user_id},
        )
        turn.confirm_quote_id = quote.id
        turn.tracer.record("quote_confirmed", now=turn.now, quote_id=quote.id, version=version, action=quote.action)
        turn.say(QUEUED_NOTICE, NOTICE_CARD)
        turn.end(ExitReason(kind="queued"))
        return
    turn.tracer.record("confirm_refused", now=turn.now, quote_id=quote_id, reason=refusal)
    if refusal == "too_soon" and quote is not None:
        turn.save_quote(quote.model_copy(update={"status": "expired", "updated_at": turn.now}))
        turn.say(TOO_SOON, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="too_soon"))
        return
    detail = "already_confirmed" if refusal == "already_confirmed" else "stale_proposal"
    turn.say(ALREADY if detail == "already_confirmed" else STALE_CARD, NOTICE_CARD)
    turn.end(ExitReason(kind="needs_input", detail=detail))


def decline_quote(turn: TurnContext, quote_id: str) -> None:
    """Drop the quote on screen if it is the one named and still only shown."""
    quote = turn.quote
    if quote is None or quote.id != quote_id or quote.status != "shown":
        turn.say(STALE_CARD, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="stale_proposal"))
        return
    turn.save_quote(quote.model_copy(update={"status": "declined", "updated_at": turn.now}))
    turn.say(DECLINED, NOTICE_CARD)


def apply_card_action(deps: Deps, turn: TurnContext, action: CardAction) -> None:
    """Run a confirm or decline tapped on a card."""
    if action.kind == "decline":
        decline_quote(turn, action.quote_id)
    elif action.version is None:
        turn.say(STALE_CARD, NOTICE_CARD)
        turn.end(ExitReason(kind="needs_input", detail="stale_proposal"))
    else:
        confirm_quote(deps, turn, action.quote_id, action.version)
