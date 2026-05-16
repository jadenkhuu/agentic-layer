"""End-of-run memory writer — distils a successful workflow run into a
~600-word markdown summary saved to the helm studio docs tree.

`write_memory` reads the run's SPEC.md, CHANGES.md and PR description, runs
one small `claude_agent_sdk.query` to summarise them, and saves the result
under `$HELM_STUDIO_DOCS_PATH/projects/<project-slug>/memory/`. The runner
calls it once, best-effort, after the final agent completes (see
`runner._maybe_write_memory`).

When `HELM_STUDIO_DOCS_PATH` is unset the deployment has no docs tree yet —
`write_memory` logs a single info line and returns None. Every other failure
mode (missing artifacts, an SDK hiccup) degrades gracefully rather than
raising, so a memory write can never fail an otherwise-green run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from agentic.archive import run_id_timestamp
from agentic.events import AgenticEventType, EventEmitter, truncate

logger = logging.getLogger(__name__)

#: env var pointing at the helm studio docs tree (the dir holding `projects/`).
DOCS_PATH_ENV = "HELM_STUDIO_DOCS_PATH"

#: run artifacts the summary draws on, in reading order.
_SOURCE_FILES = ("SPEC.md", "CHANGES.md")

#: roughly how long the generated summary should be, in words.
TARGET_WORDS = 600

#: chars of the summary echoed into the `memory.written` event payload.
_PREVIEW_CHARS = 200


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------


def _read_run_meta(run_dir: Path) -> dict[str, str]:
    """Pull run id, workflow name and target repo path from `state.json`.

    A run without a readable state.json degrades to placeholders rather than
    failing — the directory name still yields a usable run id.
    """
    meta = {"run_id": run_dir.name, "workflow": "unknown", "target_repo_path": ""}
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return meta
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return meta
    if isinstance(data, dict):
        meta["run_id"] = str(data.get("run_id") or meta["run_id"])
        meta["workflow"] = str(data.get("workflow_name") or meta["workflow"])
        meta["target_repo_path"] = str(data.get("target_repo_path") or "")
    return meta


def _project_slug(target_repo_path: str) -> str:
    """Slug for the project's memory dir — the repo dir name lowercased and
    reduced to `[a-z0-9-]`. Falls back to `unknown-project` when empty.
    """
    name = Path(target_repo_path).name if target_repo_path else ""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown-project"


def _run_date(run_id: str) -> str:
    """`yyyy-mm-dd` for the run, from its run-id timestamp prefix; today (UTC)
    when the id carries no parseable timestamp.
    """
    ts = run_id_timestamp(run_id) or datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%d")


def _short_id(run_id: str) -> str:
    """Last hex chunk of a run id — matches `RunContext.short_id`."""
    return run_id.rsplit("-", 1)[-1]


def _memory_path(docs_root: Path, meta: dict[str, str]) -> Path:
    """`<docs>/projects/<slug>/memory/<date>-<workflow>-<short-id>.md`."""
    slug = _project_slug(meta["target_repo_path"])
    fname = (
        f"{_run_date(meta['run_id'])}-{meta['workflow']}-"
        f"{_short_id(meta['run_id'])}.md"
    )
    return docs_root / "projects" / slug / "memory" / fname


# ---------------------------------------------------------------------------
# Source gathering
# ---------------------------------------------------------------------------


def _find_pr_description(run_dir: Path) -> str | None:
    """The PR body the run's `pr` agent produced, if any.

    Matches the runner's PR-output convention — a file named `PR*.md`
    (`PR_BODY.md`, `PR.md`, …). Returns the first non-empty one.
    """
    for path in sorted(run_dir.glob("PR*.md")):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                return text
    return None


def _gather_sources(run_dir: Path) -> dict[str, str]:
    """Read the run artifacts the summary is built from. Absent files are
    skipped — a run that halted early simply contributes less context.
    """
    sources: dict[str, str] = {}
    for name in _SOURCE_FILES:
        path = run_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                sources[name] = text
    pr = _find_pr_description(run_dir)
    if pr:
        sources["PR description"] = pr
    return sources


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------


def _build_prompt(meta: dict[str, str], sources: dict[str, str]) -> str:
    """The summarisation prompt handed to the SDK."""
    if sources:
        artifacts = "\n\n".join(
            f"=== {name} ===\n{text}" for name, text in sources.items()
        )
    else:
        artifacts = "(no SPEC.md, CHANGES.md or PR description was produced)"
    return (
        "You are writing a project memory entry for a software team's "
        "knowledge base.\n\n"
        f"Below are the artifacts from one completed run of the "
        f"'{meta['workflow']}' workflow. Write a single markdown summary of "
        f"about {TARGET_WORDS} words capturing what this run accomplished: "
        "the goal, the concrete changes made, key decisions or trade-offs, "
        "and anything a teammate should know later. Write flowing prose, not "
        "a file-by-file dump. Do not add a top-level heading — start straight "
        f"with the summary text.\n\n{artifacts}\n"
    )


async def _query_summary(prompt: str) -> str:
    """Run one tool-less SDK query and return the concatenated assistant text."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(allowed_tools=[])
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks).strip()


def _generate_summary(meta: dict[str, str], sources: dict[str, str]) -> str:
    """Summarise the run via the SDK. On any SDK failure, fall back to a
    deterministic summary so a memory file is still written.
    """
    prompt = _build_prompt(meta, sources)
    try:
        summary = asyncio.run(_query_summary(prompt))
    except Exception as e:  # SDK hiccup must not lose the memory entry
        logger.warning("memory: SDK summarisation failed (%s); using fallback", e)
        summary = ""
    return summary or _fallback_summary(meta, sources)


def _fallback_summary(meta: dict[str, str], sources: dict[str, str]) -> str:
    """A deterministic summary built straight from the artifacts — used in
    stub mode and whenever the SDK call is unavailable or fails.
    """
    lines = [
        f"Run `{meta['run_id']}` of the `{meta['workflow']}` workflow "
        f"completed successfully.",
        "",
    ]
    if sources:
        for name, text in sources.items():
            excerpt = " ".join(text.split())
            if len(excerpt) > 600:
                excerpt = excerpt[:597] + "..."
            lines.append(f"**{name}:** {excerpt}")
            lines.append("")
    else:
        lines.append(
            "No SPEC.md, CHANGES.md or PR description was captured for this "
            "run, so only the run metadata is recorded here."
        )
    return "\n".join(lines).strip()


def _render_memory(meta: dict[str, str], summary: str) -> str:
    """Wrap the summary in a small markdown header for the docs tree."""
    date = _run_date(meta["run_id"])
    return (
        f"# {meta['workflow']} — {date}\n\n"
        f"- **Run:** `{meta['run_id']}`\n"
        f"- **Workflow:** {meta['workflow']}\n"
        f"- **Project:** {_project_slug(meta['target_repo_path'])}\n"
        f"- **Date:** {date}\n\n"
        "## Summary\n\n"
        f"{summary.strip()}\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def write_memory(
    run_dir: Path | str,
    summary: str | None = None,
    *,
    events: EventEmitter | None = None,
    stub_mode: bool = False,
) -> Path | None:
    """Write a ~600-word markdown memory of the run at `run_dir`.

    Reads the run's SPEC.md, CHANGES.md and PR description, summarises them
    with one small SDK query, and saves the result under
    `$HELM_STUDIO_DOCS_PATH/projects/<project-slug>/memory/`.

    `summary`, when given, is used verbatim — the SDK call is skipped. The
    runner leaves it None; tests pass it to exercise the file path offline.
    In `stub_mode` a deterministic summary is built from the artifacts with
    no SDK round-trip.

    Returns the path written, or None when `HELM_STUDIO_DOCS_PATH` is unset
    (the deployment has no docs tree yet) — that case is logged once and
    skipped silently. On a successful write a `memory.written` event is
    emitted via `events` when one is supplied.
    """
    run_dir = Path(run_dir)
    docs_env = os.environ.get(DOCS_PATH_ENV)
    if not docs_env:
        logger.info(
            "memory: %s not set; skipping memory write for %s",
            DOCS_PATH_ENV, run_dir.name,
        )
        return None

    meta = _read_run_meta(run_dir)
    sources = _gather_sources(run_dir)

    if summary is None:
        summary = (
            _fallback_summary(meta, sources)
            if stub_mode
            else _generate_summary(meta, sources)
        )

    dest = _memory_path(Path(docs_env), meta)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_render_memory(meta, summary), encoding="utf-8")
    size_bytes = dest.stat().st_size
    logger.info("memory: wrote %s (%d bytes)", dest, size_bytes)

    if events is not None:
        events.emit(
            AgenticEventType.MEMORY_WRITTEN,
            path=str(dest),
            size_bytes=size_bytes,
            summary_preview=truncate(summary.strip(), _PREVIEW_CHARS),
        )
    return dest
