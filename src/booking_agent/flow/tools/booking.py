"""Booking tools: search for a new visit (offers card) and choose a slot (quote card). Neither books."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from booking_agent.catalog import ServiceCatalog
from booking_agent.domain.entities import Criteria, Quote
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.memory_facts import split_forgotten
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.memory import drop_forgotten, prefill_criteria
from booking_agent.flow.quotes import PickResult, pick_slot
from booking_agent.flow.search import run_new_search
from booking_agent.flow.tools.arguments import DATE_PARAMETER, DAYS_PARAMETER, TIMES_PARAMETER, criteria_from_args
from booking_agent.flow.tools.base import Tool, ToolOutput, fixed_parameters, object_schema
from booking_agent.render import blocked_text, offers_card, offers_text, question

MAX_NAME_CHARS = 60  # provider names are untrusted free text; the model sees at most this much
MAX_OFFERS_TOLD = 5


def _find_parameters(catalog: ServiceCatalog) -> dict[str, Any]:
    return object_schema(
        {
            "service": {"type": "string", "enum": list(catalog.services)},
            "area": {"type": "string", "enum": list(catalog.areas)},
            "date_from": DATE_PARAMETER,
            "date_to": DATE_PARAMETER,
            "any_date": {"type": "boolean", "description": "the user said any day is fine"},
            "days": DAYS_PARAMETER,
            "times": TIMES_PARAMETER,
            "max_price_rupees": {"type": "number", "description": "strict upper limit in rupees"},
            "no_budget": {"type": "boolean"},
            "same_provider_as_last_time": {"type": "boolean"},
        }
    )


def _previous_provider(turn: TurnContext, service: str) -> str | None:
    """Provider of the user's most recent completed booking for the service, from the provider's own history."""
    answer = turn.calls.list_bookings(turn.user_id)
    done = [b for b in (answer.value or []) if b.service == service and b.status == "completed"]
    return max(done, key=lambda b: b.slot_start).provider_id if done else None


def _find(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    base = turn.search.criteria if turn.search is not None else Criteria()
    criteria = criteria_from_args(args, base)
    criteria = drop_forgotten(criteria, split_forgotten(turn.conversation.summary)[1], turn.text)
    memory = deps.store.get_profile(turn.user_id).memory
    criteria = prefill_criteria(criteria, memory, deps.catalog, fill_times=turn.search is None and "times" not in args)
    if criteria.service is not None and criteria.service not in deps.catalog.services:
        return {"status": "could_not_propose", "reason": "unsupported_service"}
    if criteria.area is not None and criteria.area not in deps.catalog.areas:
        return {"status": "could_not_propose", "reason": "unsupported_area"}
    if "same_provider_as_last_time" in args:
        provider = _previous_provider(turn, criteria.service or "") if args["same_provider_as_last_time"] else None
        if args["same_provider_as_last_time"] and provider is None:
            return {"status": "could_not_propose", "reason": "no_previous_booking"}
        criteria = criteria.model_copy(update={"provider_id": provider})
    missing = criteria.missing()
    if missing:
        return _ask(deps, turn, criteria, missing)
    return _offer(deps, turn, criteria)


def _ask(deps: Deps, turn: TurnContext, criteria: Criteria, missing: list[str]) -> ToolOutput:
    from booking_agent.flow.search import store_search

    store_search(deps, turn, criteria, ())
    text, card = question(missing, deps.catalog)
    turn.attach = (text, card)
    turn.end(ExitReason(kind="needs_input", detail=",".join(missing)))
    return {"status": "needs_info", "missing": missing}


def _offer(deps: Deps, turn: TurnContext, criteria: Criteria) -> ToolOutput:
    outcome = run_new_search(deps, turn, criteria)
    if outcome.provider_failed:
        return {"status": "could_not_propose", "reason": "provider_unavailable"}
    if not outcome.offers:
        text = blocked_text(outcome.reason or "no_providers")
        turn.end(ExitReason(kind="blocked", detail=outcome.reason))
        return {"status": "could_not_propose", "reason": outcome.reason, "details": [text]}
    search = turn.search
    turn.attach = (offers_text(len(outcome.offers)), offers_card(search.id if search else ""))
    turn.end(ExitReason(kind="needs_input", detail="choose_slot"))
    return {
        "status": "offers_shown_to_user",
        "providers": [
            {
                "provider_id": offer.provider_id,
                "name": offer.name[:MAX_NAME_CHARS],
                "rating": offer.rating,
                "price_rupees": offer.price,
                "first_slot": offer.slots[0].start.isoformat(),
            }
            for offer in outcome.offers[:MAX_OFFERS_TOLD]
        ],
        "next": "Tell the user briefly. They tap a slot on the card; nothing is booked until they confirm.",
    }


def quote_result(quote: Quote) -> dict[str, Any]:
    """What the model is told about a quote on screen."""
    return {
        "status": "quote_shown_to_user",
        "quote": {
            "action": quote.action,
            "provider": quote.provider_name[:MAX_NAME_CHARS],
            "start": quote.start.isoformat(),
            "end": quote.end.isoformat(),
            "price_rupees": quote.price,
            "booking_id": quote.booking_id,
        },
        "next": "Tell the user briefly. They confirm on the card; nothing is booked until they do.",
    }


def pick_result(result: PickResult) -> ToolOutput:
    """The model's view of a pick."""
    if result.status == "quoted" and result.quote is not None:
        return quote_result(result.quote)
    if result.status == "slot_failed":
        return {
            "status": "slot_unavailable",
            "next": "Tell the user the slot was just taken and you are looking for other slots. Do nothing else.",
        }
    notes = {
        "busy": "a confirmed request is waiting for the provider; nothing else can be shown now",
        "not_offered": "that provider and slot are not in the current offers",
        "over_budget": "every remaining provider quoted a price over the budget",
    }
    return {
        "status": "could_not_propose",
        "reason": result.status,
        "details": [notes.get(result.status, result.detail)],
    }


def _choose(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    start = datetime.fromisoformat(args["slot_start"])
    turn.exit = None
    return pick_result(pick_slot(deps, turn, args["provider_id"], start))


FIND_AND_PROPOSE_BOOKING = Tool("find_and_propose_booking", _find_parameters, _find)
CHOOSE_OPTION = Tool(
    "choose_option",
    fixed_parameters(
        {"provider_id": {"type": "string"}, "slot_start": {"type": "string", "description": "ISO start of the slot"}},
        ["provider_id", "slot_start"],
    ),
    _choose,
)
BOOKING_TOOLS = (FIND_AND_PROPOSE_BOOKING, CHOOSE_OPTION)
