"""The ``Tool`` type: a name, a parameter schema builder and a handler. Descriptions live in prompts.toml."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from booking_agent.catalog import ServiceCatalog
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
