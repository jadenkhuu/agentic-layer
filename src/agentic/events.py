"""Structured event emission for run observation.

Each event is a single JSON line in `<run-dir>/events.jsonl`:

    {"ts": "<ISO-8601 UTC ms>", "type": "<type>", "agent": "<id|null>",
     "payload": {...}}

The schema is the contract between the orchestrator (which emits) and the
consumers (the watch TUI, helm's run sync). Keep it stable; if a field
must change, add — don't rename or remove.

The closed set of `type` values is enumerated by `AgenticEventType` below.
See README "Watching a run" for the payload of each.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AgenticEventType(str, Enum):
    """Every `type` value the orchestrator writes to events.jsonl.

    A `str` enum so members JSON-serialise as their value and consumers can
    switch exhaustively. `EventEmitter.emit` accepts either a member or a
    raw string, so unknown/forward-compat types are still permitted.
    """

    RUN_START = "run.start"
    RUN_COMPLETE = "run.complete"
    RUN_RESUME = "run.resume"
    # emitted by `agentic fork` into the new run's events.jsonl. payload:
    # {workflow, agent_count, branch, stub_mode, forked_from, fork_step}
    RUN_FORK = "run.fork"
    AGENT_START = "agent.start"
    AGENT_COMPLETE = "agent.complete"
    AGENT_FAIL = "agent.fail"
    AGENT_PAUSE = "agent.pause"
    # human-in-the-loop feedback supplied when a paused run is resumed via
    # `agentic resume --feedback`. payload: {text}. The same text is folded
    # into the run's inputs as `hitl_feedback` so the next agent can read it.
    HITL_FEEDBACK = "hitl.feedback"
    ASSISTANT_TEXT = "assistant.text"
    TOOL_USE = "tool.use"
    TOOL_RESULT = "tool.result"
    SCRIPT_START = "script.start"
    SCRIPT_COMPLETE = "script.complete"
    CI_POLL = "ci.poll"
    CI_FIX_START = "ci.fix.start"
    CI_FIX_COMPLETE = "ci.fix.complete"
    CI_FIX_FAIL = "ci.fix.fail"
    # one per SDK round-trip — token usage + estimated USD cost. payload:
    # {model, input_tokens, output_tokens, cache_read, cache_creation, cost_usd}
    COST = "cost"
    # emitted once when a successful run produced a PR — the cue for helm's
    # run sync to fire a Vercel preview deploy. payload:
    # {branch, base_branch, pr_number, pr_url, sha, target_repo}
    DEPLOY_TRIGGER = "deploy.trigger"
    # emitted once at the end of a successful run when the memory writer
    # saved a run summary to the helm studio docs tree. payload:
    # {path, size_bytes, summary_preview}
    MEMORY_WRITTEN = "memory.written"

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

    def emit(
        self,
        event_type: "str | AgenticEventType",
        *,
        agent: str | None = None,
        **payload: Any,
    ) -> None:
        if self.path is None:
            return
        record = {
            "ts": _now_iso(),
            "type": event_type.value if isinstance(event_type, AgenticEventType) else event_type,
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

    def emit_cost(
        self,
        *,
        agent: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_creation: int = 0,
        cost_usd: float,
    ) -> None:
        """Emit a `cost` event for one SDK round-trip.

        The payload shape is the cost contract consumed by helm's run sync;
        keep the field names stable. Callers stay terse — `agent.py` hands
        this the SDK usage numbers and an already-resolved `cost_usd`.
        """
        self.emit(
            AgenticEventType.COST,
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation,
            cost_usd=round(cost_usd, 6),
        )
