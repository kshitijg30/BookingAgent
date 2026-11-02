"""Tools for existing bookings and quotes: list them, and propose a cancellation or a new time."""

from __future__ import annotations

from typing import Any

from booking_agent.domain.entities import Booking, Criteria, Quote
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.quotes import pick_slot, show_quote
from booking_agent.flow.search import run_new_search
from booking_agent.flow.tools.arguments import (
    DATE_PARAMETER,
    DAYS_PARAMETER,
    TIMES_PARAMETER,
    parse_date,
    parse_days,
    parse_times,
)
from booking_agent.flow.tools.base import Tool, ToolOutput, fixed_parameters
from booking_agent.flow.tools.booking import MAX_NAME_CHARS, pick_result, quote_result

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
