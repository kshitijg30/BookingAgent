"""Framework-independent tracing. Optional exporters consume completed structured spans.

No SDK, filesystem or network is touched at import. File export is enabled only by trace_dir.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as timer
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from booking_agent.domain.untrusted import Untrusted

_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|password|authorization|bearer|(^|[._-])(access_|auth_|refresh_|id_)?token$)", re.IGNORECASE
)
_SECRET_VAL = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|sk-or-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")
# Not inside an identifier: a hex job id with ten digits in a row is not a phone number.
_PHONE = re.compile(r"(?<![A-Za-z0-9])(\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![A-Za-z0-9])")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, Untrusted):
        return value.trace_view()
    if isinstance(value, BaseModel):
        return sanitize(value.model_dump(mode="json"), depth + 1)
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if _SECRET_KEY.search(str(key)) and key != "idem_key" else sanitize(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize(item, depth + 1) for item in value][:50]
    if isinstance(value, str):
        return _PHONE.sub("[phone]", _SECRET_VAL.sub("[redacted]", value))[:400]
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:400]


def to_attributes(fields: dict[str, Any]) -> dict[str, Any]:
    """Span attributes must be primitives; nested values become JSON strings."""
    result: dict[str, Any] = {}
    for key, value in sanitize(fields).items():
        if value is None:
            continue
        primitive = isinstance(value, bool | int | float | str)
        if primitive or (isinstance(value, list) and value and all(isinstance(item, str) for item in value)):
            result[key] = value
        else:
            result[key] = json.dumps(value, ensure_ascii=False, default=str)
    return result


def summarize(spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One line per trace for the admin activity list."""
    root = next((span for span in spans if span["parent_id"] is None), None)
    if root is None:
        return None
    attributes = root["attributes"]
    models = [span for span in spans if span["name"].startswith("model")]
    return {
        "trace_id": root["trace_id"],
        "name": root["name"],
        "start": root["start"],
        "duration_ms": root["duration_ms"],
        "session_id": attributes.get("session.id"),
        "user_id": attributes.get("user.id"),
        "message": attributes.get("user.message"),
        "exit": attributes.get("exit.kind"),
        "answered_by": attributes.get("agent.answered_by"),
        "status": root["status"],
        "tool_calls": sum(1 for span in spans if span["name"].startswith("tool ")),
        "tokens": sum(
            int(span["attributes"].get("gen_ai.usage.input_tokens", 0))
            + int(span["attributes"].get("gen_ai.usage.output_tokens", 0))
            for span in models
        ),
        "cost_usd": round(sum(float(span["attributes"].get("cost_usd", 0)) for span in models), 6),
    }


class RecentSpans:
    def __init__(self, max_traces: int = 300) -> None:
        self.max = max_traces
        self._traces: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def add(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._traces.setdefault(record["trace_id"], []).append(record)
            self._traces.move_to_end(record["trace_id"])
            while len(self._traces) > self.max:
                self._traces.popitem(last=False)

    def get(self, trace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def all(self) -> list[list[dict[str, Any]]]:
        with self._lock:
            return [list(spans) for spans in self._traces.values()]


class Telemetry:
    """Bounded local spans plus an optional export callback, owned by the caller."""

    def __init__(
        self,
        *,
        trace_dir: Path | str | None = None,
        service_name: str = "booking-agent",
        exporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.trace_dir = Path(trace_dir) if trace_dir is not None else None
        self.service_name = service_name
        self.exporter = exporter
        self.recent = RecentSpans()
        self._lock = threading.RLock()
        if self.trace_dir is not None:
            self.trace_dir.mkdir(parents=True, exist_ok=True)

    def export(self, record: dict[str, Any]) -> None:
        self.recent.add(record)
        try:
            with self._lock:
                if self.trace_dir is not None:
                    with (self.trace_dir / f"{record['trace_id']}.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                if self.exporter is not None:
                    self.exporter(record)
        except Exception:
            logging.getLogger(__name__).exception("trace export failed")

    def load(self, trace_id: str) -> list[dict[str, Any]]:
        if not _TRACE_ID.fullmatch(trace_id):
            return []
        spans = self.recent.get(trace_id)
        if spans or self.trace_dir is None:
            return spans
        return _read_spans(self.trace_dir / f"{trace_id}.jsonl")

    def summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        traces = self.recent.all()
        if self.trace_dir is not None:
            seen = {trace[0]["trace_id"] for trace in traces if trace}
            files = sorted(self.trace_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)[-limit:]
            traces += [_read_spans(path) for path in files if path.stem not in seen]
        result = [item for trace in traces if (item := summarize(trace)) is not None]
        result.sort(key=lambda item: item["start"] or "", reverse=True)
        return result[:limit]

    def shutdown(self) -> None:
        """The exporter lifecycle belongs to its owner; this local sink has no worker threads."""


def _read_spans(path: Path) -> list[dict[str, Any]]:
    result = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except json.JSONDecodeError:
                continue
    return result


@dataclass
class Span:
    record: dict[str, Any]
    started: float = field(default_factory=timer.perf_counter)

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self.record["attributes"].update(attributes)


class Tracer:
    """Per-operation trace; nested spans are explicit and never use SDK-global state."""

    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry
        self.trace_id: str | None = None
        self._root: Span | None = None
        self._stack: list[Span] = []

    def _new_span(self, name: str, parent_id: str | None, fields: dict[str, Any]) -> Span:
        return Span(
            {
                "trace_id": self.trace_id,
                "span_id": uuid.uuid4().hex[:16],
                "parent_id": parent_id,
                "name": name,
                "start": datetime.now(UTC).isoformat(),
                "end": None,
                "duration_ms": 0,
                "status": "UNSET",
                "attributes": to_attributes(fields),
                "events": [],
            }
        )

    def start(self, *, session_id: str, user_id: str, now: datetime, name: str = "turn", **fields: Any) -> str:
        self.trace_id = uuid.uuid4().hex
        self._root = self._new_span(
            name, None, {"session.id": session_id, "user.id": user_id, "clock.now": now.isoformat(), **fields}
        )
        return self.trace_id

    def record(self, type_: str, *, now: datetime, **fields: Any) -> None:
        if self._root is not None:
            self._root.record["events"].append(
                {
                    "name": type_,
                    "ts": datetime.now(UTC).isoformat(),
                    "attributes": to_attributes({"clock.now": now.isoformat(), **fields}),
                }
            )

    def _end(self, span: Span) -> None:
        span.record["end"] = datetime.now(UTC).isoformat()
        span.record["duration_ms"] = round((timer.perf_counter() - span.started) * 1000, 2)
        self.telemetry.export(span.record)

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else self._root
        span = self._new_span(name, parent.record["span_id"] if parent else None, fields)
        self._stack.append(span)
        try:
            yield span
        except Exception:
            span.record["status"] = "ERROR"
            raise
        finally:
            self._stack.pop()
            self._end(span)

    @staticmethod
    def set(span: Span, **fields: Any) -> None:
        span.set_attributes(to_attributes(fields))

    def finish(self, *, error: bool = False, **fields: Any) -> None:
        if self._root is not None:
            self.set(self._root, **fields)
            self._root.record["status"] = "ERROR" if error else "UNSET"
            self._end(self._root)
            self._root = None

    @property
    def path(self) -> Path | None:
        directory = self.telemetry.trace_dir
        return directory / f"{self.trace_id}.jsonl" if directory is not None and self.trace_id else None


def _maybe_json(value: Any) -> Any:
    """Undo ``to_attributes``: attributes that hold JSON text are parsed back."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def timeline(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten spans into ordered lines: run start, events, model calls and tool calls."""
    lines: list[tuple[str, int, dict[str, Any]]] = []
    for span in spans:
        attributes = {key: _maybe_json(value) for key, value in span["attributes"].items()}
        if span["parent_id"] is None:
            lines.append(
                (
                    span["start"],
                    0,
                    {
                        "type": "run_started",
                        "trace_id": span["trace_id"],
                        "span": span["name"],
                        "session_id": attributes.get("session.id"),
                        "user_id": attributes.get("user.id"),
                    },
                )
            )
            for event in span["events"]:
                lines.append(
                    (
                        event["ts"],
                        1,
                        {
                            "type": event["name"],
                            "trace_id": span["trace_id"],
                            **{key: _maybe_json(value) for key, value in event["attributes"].items()},
                        },
                    )
                )
        elif span["name"].startswith("tool "):
            lines.append(
                (
                    span["start"],
                    1,
                    {
                        "type": "tool_call",
                        "trace_id": span["trace_id"],
                        "tool": attributes.get("tool.name"),
                        "args": attributes.get("tool.args", {}),
                        "outcome": attributes.get("outcome"),
                        "error": attributes.get("error"),
                        "result": attributes.get("result"),
                        "latency_ms": span["duration_ms"],
                    },
                )
            )
        elif span["name"].startswith("model"):
            lines.append(
                (
                    span["start"],
                    1,
                    {
                        "type": "model_call",
                        "trace_id": span["trace_id"],
                        "latency_ms": span["duration_ms"],
                        **attributes,
                    },
                )
            )
    lines.sort(key=lambda line: (line[0] or "", line[1]))
    return [line for _, _, line in lines]


def read_jsonl_dir(directory: Path | str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.jsonl"), key=lambda path: path.stat().st_mtime):
        result.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return result
