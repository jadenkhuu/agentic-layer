"""Retrospective builder — turns a run's `events.jsonl` + `state.json` into a
`RETRO.md` report.

The `retrospective` agent template (`scaffold/workflows/retrospective.yaml`)
declares an agent whose declared output is `RETRO.md`. In stub mode the
orchestrator fills that output with `build_retro(...)` directly, so a useful
retrospective is produced deterministically with no SDK round-trip; for real
runs the bundled prompt (`scaffold/prompts/retro.md`) drives the SDK agent,
which reads the same files and can layer qualitative analysis on top.

`build_retro` is pure: give it a run directory and it returns Markdown. It
never raises on a malformed event line — observability data is best-effort,
and a retro should still render from a partial log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RETRO_FILENAME = "RETRO.md"


@dataclass
class AgentStat:
    """Per-agent rollup distilled from the event stream."""

    agent: str
    started: bool = False
    completed: bool = False
    failed: bool = False
    paused: bool = False
    error: str = ""
    elapsed_seconds: float | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0

    @property
    def ok(self) -> bool:
        """Completed cleanly — done, not failed."""
        return self.completed and not self.failed

    @property
    def in_progress(self) -> bool:
        """Started but not yet finished (e.g. the retro agent itself)."""
        return self.started and not self.completed and not self.failed


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Parse `events.jsonl`, skipping blank/malformed lines."""
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


def _read_state(run_dir: Path) -> dict[str, Any]:
    """Parse `state.json`, returning {} when absent or unreadable."""
    path = run_dir / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _event_agent(event: dict[str, Any]) -> str | None:
    """The agent an event belongs to — top-level `agent`, else payload id."""
    agent = event.get("agent")
    if isinstance(agent, str) and agent:
        return agent
    payload = event.get("payload")
    if isinstance(payload, dict):
        aid = payload.get("agent_id")
        if isinstance(aid, str) and aid:
            return aid
    return None


def collect_stats(events: list[dict[str, Any]]) -> list[AgentStat]:
    """Walk the event stream into ordered per-agent stats. Deterministic."""
    by_id: dict[str, AgentStat] = {}
    order: list[str] = []

    def ensure(agent_id: str) -> AgentStat:
        if agent_id not in by_id:
            by_id[agent_id] = AgentStat(agent=agent_id)
            order.append(agent_id)
        return by_id[agent_id]

    for event in events:
        etype = event.get("type")
        agent_id = _event_agent(event)
        if not agent_id:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        stat = ensure(agent_id)

        if etype == "agent.start":
            stat.started = True
        elif etype == "agent.complete":
            stat.completed = True
            elapsed = payload.get("elapsed_seconds")
            if isinstance(elapsed, (int, float)):
                stat.elapsed_seconds = float(elapsed)
        elif etype == "agent.fail":
            stat.failed = True
            err = payload.get("error")
            if isinstance(err, str):
                stat.error = err
        elif etype == "agent.pause":
            stat.paused = True
        elif etype == "cost":
            cost = payload.get("cost_usd")
            if isinstance(cost, (int, float)):
                stat.cost_usd += float(cost)
            for field_name in ("input_tokens", "output_tokens"):
                val = payload.get(field_name)
                if isinstance(val, (int, float)):
                    setattr(stat, field_name, getattr(stat, field_name) + int(val))
        elif etype == "tool.use":
            stat.tool_calls += 1
        elif etype == "tool.result":
            if payload.get("success") is False:
                stat.tool_errors += 1

    return [by_id[a] for a in order]


def _usd(amount: float) -> str:
    """USD formatter — four decimals for sub-cent amounts so stub runs read."""
    if amount <= 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _short_error(error: str) -> str:
    """First line of an error, trimmed for a one-line bullet."""
    first = error.strip().splitlines()[0] if error.strip() else ""
    return first if len(first) <= 160 else first[:157] + "..."


def _run_meta(events: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    """Run-level facts — id, workflow, status, wall time — from both sources."""
    meta: dict[str, Any] = {
        "run_id": state.get("run_id"),
        "workflow": state.get("workflow_name"),
        "status": state.get("status"),
        "elapsed_seconds": None,
        "failed_agent": None,
    }
    for event in events:
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if event.get("type") == "run.start":
            meta["workflow"] = payload.get("workflow") or meta["workflow"]
        elif event.get("type") == "run.complete":
            elapsed = payload.get("elapsed_seconds")
            if isinstance(elapsed, (int, float)):
                meta["elapsed_seconds"] = float(elapsed)
            status = payload.get("status")
            if isinstance(status, str):
                meta["status"] = status
            failed = payload.get("failed_agent")
            if isinstance(failed, str):
                meta["failed_agent"] = failed
    return meta


def _improvements(stats: list[AgentStat], meta: dict[str, Any]) -> list[str]:
    """Heuristic, run-specific suggestions. Empty -> a clean-run note is used."""
    out: list[str] = []
    timed = [s for s in stats if s.elapsed_seconds is not None]
    total_time = sum(s.elapsed_seconds or 0 for s in timed)
    total_cost = sum(s.cost_usd for s in stats)

    for stat in stats:
        if stat.failed:
            detail = f" — {_short_error(stat.error)}" if stat.error else ""
            out.append(
                f"`{stat.agent}` failed{detail}. Resolve it, then re-run from "
                f"that step (`agentic fork <run-id> --step <i>`)."
            )

    if total_time > 0:
        slowest = max(timed, key=lambda s: s.elapsed_seconds or 0)
        share = (slowest.elapsed_seconds or 0) / total_time
        if share >= 0.5 and len(timed) > 1:
            out.append(
                f"`{slowest.agent}` took {share * 100:.0f}% of agent time "
                f"({slowest.elapsed_seconds:.1f}s) — the obvious place to "
                f"trim or parallelise."
            )

    if total_cost > 0:
        priciest = max(stats, key=lambda s: s.cost_usd)
        share = priciest.cost_usd / total_cost
        if share >= 0.6 and len([s for s in stats if s.cost_usd > 0]) > 1:
            out.append(
                f"`{priciest.agent}` drove {share * 100:.0f}% of spend "
                f"({_usd(priciest.cost_usd)}) — tighten its prompt or scope "
                f"to cut cost."
            )

    for stat in stats:
        if stat.tool_errors >= 3:
            out.append(
                f"`{stat.agent}` hit {stat.tool_errors} failed tool calls — "
                f"review its `allowed_tools` and prompt for a wrong assumption."
            )

    if any(s.paused for s in stats):
        out.append(
            "Run paused for human review — expected for a HITL workflow; "
            "no action needed unless the pause was unintended."
        )

    return out


def build_retro(run_dir: Path) -> str:
    """Build a `RETRO.md` Markdown report for the run at `run_dir`."""
    run_dir = Path(run_dir)
    events = _read_events(run_dir)
    state = _read_state(run_dir)
    stats = collect_stats(events)
    meta = _run_meta(events, state)

    agent_time = sum(s.elapsed_seconds or 0 for s in stats)
    state_cost = state.get("total_cost_usd")
    total_cost = (
        float(state_cost)
        if isinstance(state_cost, (int, float))
        else sum(s.cost_usd for s in stats)
    )
    state_tokens = state.get("total_tokens")
    total_tokens = (
        int(state_tokens)
        if isinstance(state_tokens, (int, float))
        else sum(s.input_tokens + s.output_tokens for s in stats)
    )
    wall = meta["elapsed_seconds"] if meta["elapsed_seconds"] is not None else agent_time

    lines: list[str] = []
    lines.append(f"# Retrospective — {meta.get('run_id') or run_dir.name}")
    lines.append("")
    lines.append(f"- **Workflow:** {meta.get('workflow') or 'unknown'}")
    lines.append(f"- **Status:** {meta.get('status') or 'unknown'}")
    lines.append(f"- **Agents:** {len(stats)}")
    lines.append(f"- **Wall time:** {wall:.1f}s")
    lines.append(f"- **Total cost:** {_usd(total_cost)} ({total_tokens:,} tokens)")
    lines.append("")

    # --- What worked -------------------------------------------------------
    lines.append("## What worked")
    worked = [s for s in stats if s.ok]
    if worked:
        for stat in worked:
            time_part = (
                f" in {stat.elapsed_seconds:.1f}s"
                if stat.elapsed_seconds is not None
                else ""
            )
            cost_part = f" ({_usd(stat.cost_usd)})" if stat.cost_usd > 0 else ""
            lines.append(f"- `{stat.agent}` completed{time_part}{cost_part}.")
    else:
        lines.append("- No agent completed cleanly.")
    lines.append("")

    # --- What didn't -------------------------------------------------------
    lines.append("## What didn't")
    problems: list[str] = []
    for stat in stats:
        if stat.failed:
            detail = f" — {_short_error(stat.error)}" if stat.error else ""
            problems.append(f"- `{stat.agent}` failed{detail}.")
        elif stat.paused:
            problems.append(f"- `{stat.agent}` paused for human review.")
        if stat.tool_errors > 0:
            problems.append(
                f"- `{stat.agent}` had {stat.tool_errors} failed tool "
                f"call{'s' if stat.tool_errors != 1 else ''}."
            )
    if problems:
        lines.extend(problems)
    else:
        lines.append("- Nothing — every agent completed without errors.")
    lines.append("")

    # --- Time by agent -----------------------------------------------------
    lines.append("## Time by agent")
    lines.append("")
    lines.append("| Agent | Time | Share |")
    lines.append("| --- | --- | --- |")
    for stat in stats:
        if stat.elapsed_seconds is None:
            time_cell = "running" if stat.in_progress else "—"
            share_cell = "—"
        else:
            time_cell = f"{stat.elapsed_seconds:.1f}s"
            share = (stat.elapsed_seconds / agent_time * 100) if agent_time > 0 else 0
            share_cell = f"{share:.0f}%"
        lines.append(f"| `{stat.agent}` | {time_cell} | {share_cell} |")
    lines.append("")

    # --- Cost by agent -----------------------------------------------------
    lines.append("## Cost by agent")
    lines.append("")
    lines.append("| Agent | Cost | Tokens |")
    lines.append("| --- | --- | --- |")
    for stat in stats:
        tokens = stat.input_tokens + stat.output_tokens
        lines.append(f"| `{stat.agent}` | {_usd(stat.cost_usd)} | {tokens:,} |")
    lines.append("")

    # --- Improvements ------------------------------------------------------
    lines.append("## Improvements")
    improvements = _improvements(stats, meta)
    if improvements:
        for item in improvements:
            lines.append(f"- {item}")
    else:
        lines.append(
            "- No structural issues stood out — the pipeline ran clean. "
            "Keep this run as a baseline."
        )
    lines.append("")

    return "\n".join(lines)


def write_retro(run_dir: Path) -> Path:
    """Build the retro for `run_dir` and write it to `RETRO.md`. Returns the path."""
    run_dir = Path(run_dir)
    path = run_dir / RETRO_FILENAME
    path.write_text(build_retro(run_dir), encoding="utf-8")
    return path
