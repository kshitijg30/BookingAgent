"""The model's tools for bookings, one group after the other. None of them books, cancels or moves anything.

- Booking tools: search for a new visit (offers card) and choose a slot (quote card).
- Tools for existing bookings and quotes: list them, and propose a cancellation or a new time.
- Memory tools: remember what the user stated, forget a key, and record the answer to a confirmation question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from booking_agent.config import ServiceCatalog
from booking_agent.domain.entities import Booking, Criteria, Quote
from booking_agent.domain.exits import ExitReason
from booking_agent.domain.memory import confirm_key, remove_key, set_key, valid_key, valid_value
from booking_agent.domain.memory_facts import join_forgotten, split_forgotten
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.memory import change_memory, drop_forgotten, forget_in_chat, prefill_criteria
from booking_agent.flow.quotes import PickResult, pick_slot, show_quote
from booking_agent.flow.search import run_new_search
from booking_agent.flow.tools.base import (
    DATE_PARAMETER,
    DAYS_PARAMETER,
    TIMES_PARAMETER,
    Tool,
    ToolOutput,
    criteria_from_args,
    fixed_parameters,
    object_schema,
    parse_date,
    parse_days,
    parse_times,
)
from booking_agent.ports import Repository
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


STATUSES = ["booked", "cancelled", "cancelled_by_provider"]
QUOTE_STATUSES = ["shown", "confirmed", "done", "failed", "declined", "expired"]


def _list_bookings(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    bookings = deps.store.list_bookings_for_user(
        turn.user_id,
        service=args.get("service"),
        date_from=parse_date(args.get("date_from")),
        date_to=parse_date(args.get("date_to")),
        status=args.get("status"),
    )
    return {
        "bookings": [
            {
                "booking_id": b.id,
                "service": b.service,
                "provider": b.provider_name[:MAX_NAME_CHARS],
                "start": b.start.isoformat(),
                "price_rupees": b.price,
                "status": b.status,
            }
            for b in bookings
        ]
    }


def _list_quotes(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    quotes = deps.store.list_quotes_for_user(turn.user_id, status=args.get("status"), service=args.get("service"))
    return {
        "quotes": [
            {
                "quote_id": q.id,
                "action": q.action,
                "provider": q.provider_name[:MAX_NAME_CHARS],
                "start": q.start.isoformat(),
                "price_rupees": q.price,
                "status": q.status,
            }
            for q in quotes[:20]
        ]
    }


def _changeable(deps: Deps, turn: TurnContext, booking_id: str) -> Booking | str:
    """The user's upcoming booked booking, or an error message for the model."""
    booking = deps.store.get_booking(booking_id)
    if booking is None or booking.user_id != turn.user_id:
        return "no booking with that id for this user; call list_my_bookings"
    if booking.status != "booked" or booking.start <= turn.now:
        return "that booking is past, cancelled or already moved and cannot be changed"
    if turn.quote is not None and turn.quote.status == "confirmed":
        return "a confirmed request is waiting for the provider; wait for its result first"
    return booking


def _propose_cancellation(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    booking = _changeable(deps, turn, args["booking_id"])
    if isinstance(booking, str):
        return {"error": booking}
    quote = Quote(
        id=f"q-{deps.new_id()}",
        conversation_id=turn.conversation_id,
        user_id=turn.user_id,
        action="cancel",
        booking_id=booking.id,
        provider_id=booking.provider_id,
        provider_name=booking.provider_name,
        service=booking.service,
        area=booking.area,
        start=booking.start,
        end=booking.end,
        price=booking.price,
        created_at=turn.now,
        updated_at=turn.now,
    )
    show_quote(turn, quote)
    return quote_result(quote)


def _propose_reschedule(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    booking = _changeable(deps, turn, args["booking_id"])
    if isinstance(booking, str):
        return {"error": booking}
    criteria = Criteria(
        service=booking.service,
        area=booking.area,
        date_from=parse_date(args["date_from"]),
        date_to=parse_date(args["date_to"]),
        days=parse_days(args.get("days", [])),
        times=parse_times(args.get("times", [])),
        no_budget=True,
        provider_id=booking.provider_id,
        reschedule_booking_id=booking.id,
    )
    outcome = run_new_search(deps, turn, criteria)
    offers = [
        offer.model_copy(update={"slots": tuple(s for s in offer.slots if s.start != booking.start)})
        for offer in outcome.offers
    ]
    first = next((offer for offer in offers if offer.slots), None)
    if first is None:
        return {"status": "could_not_propose", "reason": outcome.reason or "no_slots_in_window"}
    turn.exit = None
    return pick_result(pick_slot(deps, turn, first.provider_id, first.slots[0].start))


LIST_MY_BOOKINGS = Tool(
    "list_my_bookings",
    fixed_parameters(
        {
            "service": {"type": "string"},
            "date_from": DATE_PARAMETER,
            "date_to": DATE_PARAMETER,
            "status": {"type": "string", "enum": STATUSES},
        }
    ),
    _list_bookings,
)
LIST_MY_QUOTES = Tool(
    "list_my_quotes",
    fixed_parameters({"status": {"type": "string", "enum": QUOTE_STATUSES}, "service": {"type": "string"}}),
    _list_quotes,
)
PROPOSE_CANCELLATION = Tool(
    "propose_cancellation", fixed_parameters({"booking_id": {"type": "string"}}, ["booking_id"]), _propose_cancellation
)
PROPOSE_RESCHEDULE = Tool(
    "propose_reschedule",
    fixed_parameters(
        {
            "booking_id": {"type": "string"},
            "date_from": DATE_PARAMETER,
            "date_to": DATE_PARAMETER,
            "days": DAYS_PARAMETER,
            "times": TIMES_PARAMETER,
        },
        ["booking_id", "date_from", "date_to"],
    ),
    _propose_reschedule,
)
CHANGE_TOOLS = (LIST_MY_BOOKINGS, LIST_MY_QUOTES, PROPOSE_CANCELLATION, PROPOSE_RESCHEDULE)


def _remember(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key, value = args.get("key"), args.get("value")
    if not valid_key(key) or not valid_value(value):
        return {"error": "invalid key or value"}
    user_id, now = turn.user_id, turn.now
    turn.write(lambda store: change_memory(store, user_id, lambda m: set_key(m, str(key), value, stated=True), now))
    return {"key": key, "stated": True, "confirmed": True}


def _forget(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key = str(args.get("key"))
    user_id, now = turn.user_id, turn.now
    conversation_id = turn.conversation_id

    def write(store: Repository) -> None:
        change_memory(store, user_id, lambda m: remove_key(m, key), now)
        forget_in_chat(store, conversation_id, [key])

    turn.write(write)
    text, forgotten = split_forgotten(turn.conversation.summary)
    summary = join_forgotten(text, [*forgotten, key])
    turn.conversation = turn.conversation.model_copy(update={"summary": summary})
    return {"removed": key}


def _confirm_memory(deps: Deps, turn: TurnContext, args: dict[str, Any]) -> ToolOutput:
    key, answer = str(args["key"]), args["answer"]
    if key not in deps.store.get_profile(turn.user_id).memory:
        return {"error": "no such memory key"}
    user_id, now = turn.user_id, turn.now
    change = confirm_key if answer == "yes" else remove_key
    turn.write(lambda store: change_memory(store, user_id, lambda m: change(m, key), now))
    return {"key": key, "recorded": answer}


_VALUE = {"type": ["string", "number", "boolean", "object", "array"]}
REMEMBER = Tool("remember", fixed_parameters({"key": {"type": "string"}, "value": _VALUE}, ["key", "value"]), _remember)
FORGET = Tool("forget", fixed_parameters({"key": {"type": "string"}}, ["key"]), _forget)
CONFIRM_MEMORY = Tool(
    "confirm_memory",
    fixed_parameters(
        {"key": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no"]}}, ["key", "answer"]
    ),
    _confirm_memory,
)
MEMORY_TOOLS = (REMEMBER, FORGET, CONFIRM_MEMORY)
