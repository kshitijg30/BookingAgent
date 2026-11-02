"""The tools the model can call, and the two functions built from them.

To add a tool, define a ``Tool`` in the module for its group, add it to ``TOOLS`` and add its description to the
``[tools]`` table in prompts.toml.
"""

from __future__ import annotations

from typing import Any

from booking_agent.catalog import ServiceCatalog
from booking_agent.flow.context import Deps, TurnContext
from booking_agent.flow.tools.base import Tool, ToolOutput
from booking_agent.flow.tools.booking import BOOKING_TOOLS
from booking_agent.flow.tools.changes import CHANGE_TOOLS
from booking_agent.flow.tools.memory import MEMORY_TOOLS
from booking_agent.prompts import Prompts

TOOLS: tuple[Tool, ...] = (*BOOKING_TOOLS, *CHANGE_TOOLS, *MEMORY_TOOLS)
TOOL_NAMES = [tool.name for tool in TOOLS]
_TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_definitions(prompts: Prompts, catalog: ServiceCatalog) -> list[dict[str, Any]]:
    return [tool.definition(prompts, catalog) for tool in TOOLS]


def run_tool(deps: Deps, turn: TurnContext, name: str, args: dict[str, Any]) -> ToolOutput:
    """Run one tool call. Bad arguments come back to the model as an error result."""
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None or args.get("_invalid_json"):
        return {"error": f"unknown tool or invalid arguments: {name}"}
    try:
        return tool.handler(deps, turn, args)
    except (ValueError, KeyError, TypeError) as error:
        return {"error": f"invalid arguments: {error}"}
