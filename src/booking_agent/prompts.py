"""Loading the agent's prompts from prompts.toml (or the file named by BA_PROMPTS_FILE)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("prompts.toml")


@dataclass(frozen=True)
class Prompts:
    system: str
    tools: dict[str, str] = field(default_factory=dict)
    replies: dict[str, str] = field(default_factory=dict)

    def tool(self, name: str) -> str:
        return self.tools.get(name, "")

    def with_system(self, system: str | None) -> Prompts:
        """A copy with the system prompt replaced (empty keeps the file's version)."""
        return replace(self, system=system) if system and system.strip() else self


def load_prompts(path: Path | str | None = None) -> Prompts:
    data = tomllib.loads(Path(path or DEFAULT_FILE).read_text(encoding="utf-8"))
    return Prompts(
        system=data["agent"]["system"].strip(),
        tools={k: v.strip() for k, v in data["tools"].items()},
        replies=dict(data.get("replies", {})),
    )
