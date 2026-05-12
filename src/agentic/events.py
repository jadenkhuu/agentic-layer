"""Structured event emission for run observation.

Each event is a single JSON line in `<run-dir>/events.jsonl`:

    {"ts": "<ISO-8601 UTC ms>", "type": "<type>", "agent": "<id|null>",
     "payload": {...}}

The schema is the contract between the orchestrator (which emits) and the
watch TUI (which tails and renders). Keep it stable; if a field must
change, add — don't rename or remove.

Event types: run.start, run.complete, agent.start, agent.complete,
agent.fail, tool.use, tool.result, assistant.text.

See README "Watching a run" for the payload of each.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOOL_INPUT_MAX = 500
TOOL_RESULT_MAX = 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def serialize_tool_input(tool_input: Any) -> str:
    """Render a tool's input dict as a string and cap at TOOL_INPUT_MAX."""
    try:
        s = json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(tool_input)
    return truncate(s, TOOL_INPUT_MAX)


def serialize_tool_result(content: Any) -> str:
    """Render a tool result as a string and cap at TOOL_RESULT_MAX."""
    if content is None:
        return ""
    if isinstance(content, str):
        return truncate(content, TOOL_RESULT_MAX)
    try:
        s = json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(content)
    return truncate(s, TOOL_RESULT_MAX)


class EventEmitter:
    """Append-only JSONL writer with fsync per line.

    If `path` is None, emit is a no-op — this lets RunContext have a default
    emitter that works in unit tests that don't go through run_workflow.

    Emission failures are caught and logged at WARNING level; they do not
    propagate. The TUI is non-critical to a run.
    """

    def __init__(self, path: Path | None):
        self.path = path
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, *, agent: str | None = None, **payload: Any) -> None:
        if self.path is None:
            return
        record = {
            "ts": _now_iso(),
            "type": event_type,
            "agent": agent,
            "payload": payload,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                import os
                os.fsync(f.fileno())
        except Exception as e:  # never let observability break a run
            logger.warning("event emit failed (type=%s agent=%s): %s",
                           event_type, agent, e)
