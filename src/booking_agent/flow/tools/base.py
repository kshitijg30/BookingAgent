"""The ``Tool`` type (a name, a parameter schema builder and a handler) and the argument parsing shared by the
tools. Descriptions live in prompts.toml."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time
from typing import Any

from booking_agent.catalog import ServiceCatalog
from booking_agent.domain.entities import Criteria, TimeRange, Weekday
from booking_agent.domain.planning import WEEKDAYS, part_range
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.prompts import Prompts

ToolOutput = dict[str, Any]
ToolHandler = Callable[[Deps, TurnContext, dict[str, Any]], ToolOutput]
ParameterBuilder = Callable[[ServiceCatalog], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    """A tool the model can call. ``handler`` returns a small JSON-able result for the model."""

    name: str
    parameters: ParameterBuilder
    handler: ToolHandler

    def definition(self, prompts: Prompts, catalog: ServiceCatalog) -> dict[str, Any]:
        function = {"name": self.name, "description": prompts.tool(self.name), "parameters": self.parameters(catalog)}
        return {"type": "function", "function": function}


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def fixed_parameters(properties: dict[str, Any], required: list[str] | None = None) -> ParameterBuilder:
    def build(catalog: ServiceCatalog) -> dict[str, Any]:
        return object_schema(properties, required)

    return build


DATE_PARAMETER: dict[str, Any] = {"type": "string", "description": "YYYY-MM-DD"}
DAYS_PARAMETER: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "enum": list(WEEKDAYS)},
    "description": "weekdays allowed; empty means every day ('no weekends' is mon..fri)",
}
TIMES_PARAMETER: dict[str, Any] = {
    "type": "array",
    "description": "time ranges each day, or part names morning, afternoon, evening; empty means any time",
    "items": {
        "anyOf": [
            {"type": "string", "enum": ["morning", "afternoon", "evening"]},
            {
                "type": "object",
                "properties": {"from": {"type": "string", "description": "HH:MM"}, "to": {"type": "string"}},
                "required": ["from", "to"],
            },
        ]
    },
}


def parse_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def parse_clock(text: str) -> time:
    hours, _, minutes = text.partition(":")
    return time(int(hours), int(minutes or 0))


def parse_times(raw: list[Any]) -> tuple[TimeRange, ...]:
    ranges: list[TimeRange] = []
    for item in raw:
        if isinstance(item, str):
            part = part_range(item)
            if part is None:
                raise ValueError(f"unknown time of day: {item}")
            ranges.append(part)
        else:
            ranges.append(TimeRange(from_time=parse_clock(item["from"]), to_time=parse_clock(item["to"])))
    return tuple(ranges)


def parse_days(raw: list[str]) -> tuple[Weekday, ...]:
    days = [name.strip().lower()[:3] for name in raw]
    unknown = [name for name in days if name not in WEEKDAYS]
    if unknown:
        raise ValueError(f"unknown weekday: {unknown[0]}")
    return tuple(day for day in WEEKDAYS if day in days)


def criteria_from_args(args: dict[str, Any], base: Criteria) -> Criteria:
    """Apply the model's arguments on top of ``base``. Fields the model left out keep their value."""
    update: dict[str, Any] = {"replaces_booking_id": None, "reschedule_booking_id": None, "relaxed": []}
    for name in ("service", "area"):
        if args.get(name):
            update[name] = args[name]
    if date_from := parse_date(args.get("date_from")):
        update |= {"date_from": date_from, "date_to": parse_date(args.get("date_to")) or date_from, "any_date": False}
    elif args.get("any_date"):
        update |= {"any_date": True, "date_from": None, "date_to": None}
    if "days" in args:
        update["days"] = parse_days(args["days"])
    if "times" in args:
        update["times"] = parse_times(args["times"])
    if args.get("max_price_rupees") is not None:
        update |= {"max_price": int(args["max_price_rupees"]), "no_budget": False}
    elif args.get("no_budget"):
        update |= {"max_price": None, "no_budget": True}
    if args.get("service") and args["service"] != base.service and "same_provider_as_last_time" not in args:
        update["provider_id"] = None
    return base.model_copy(update=update)
