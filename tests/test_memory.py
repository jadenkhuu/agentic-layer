"""Tests for the end-of-run memory writer (`agentic.memory`)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from agentic.events import EventEmitter
from agentic.memory import DOCS_PATH_ENV, write_memory

RUN_ID = "20260516-180312-abcd1234"


def _make_run(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    workflow: str = "feature",
    with_sources: bool = True,
) -> Path:
    """A run dir with state.json and (optionally) the summary source files."""
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workflow_name": workflow,
                "target_repo_path": str(tmp_path / "myrepo"),
                "status": "succeeded",
            }
        ),
        encoding="utf-8",
    )
    if with_sources:
        (run_dir / "SPEC.md").write_text(
            "# Spec\nAdd a CSV export button to the reports page.\n",
            encoding="utf-8",
        )
        (run_dir / "CHANGES.md").write_text(
            "# Changes\nAdded exportCsv() to reports.ts and a button to "
            "ReportsPage.tsx.\n",
            encoding="utf-8",
        )
        (run_dir / "PR_BODY.md").write_text(
            "## Add CSV export\nLets users download report data.\n",
            encoding="utf-8",
        )
    return run_dir


def test_smoke_writes_memory_file(tmp_path: Path, monkeypatch):
    """A full run dir produces a memory file at the slug/date/workflow path."""
    docs = tmp_path / "docs"
    monkeypatch.setenv(DOCS_PATH_ENV, str(docs))
    run_dir = _make_run(tmp_path)

    dest = write_memory(run_dir, stub_mode=True)

    assert dest is not None
    expected = (
        docs / "projects" / "myrepo" / "memory"
        / "2026-05-16-feature-abcd1234.md"
    )
    assert dest == expected
    assert dest.exists()
    body = dest.read_text(encoding="utf-8")
    # header metadata + the artifact-derived summary are both present
    assert "# feature — 2026-05-16" in body
    assert RUN_ID in body
    assert "CSV export" in body


def test_explicit_summary_used_verbatim(tmp_path: Path, monkeypatch):
    """A caller-supplied summary is written as-is, with no SDK round-trip."""
    docs = tmp_path / "docs"
    monkeypatch.setenv(DOCS_PATH_ENV, str(docs))
    run_dir = _make_run(tmp_path)

    dest = write_memory(run_dir, summary="Hand-written run summary.")

    assert dest is not None
    assert "Hand-written run summary." in dest.read_text(encoding="utf-8")


def test_missing_source_files_handled(tmp_path: Path, monkeypatch):
    """A run that never produced SPEC.md / CHANGES.md / a PR still writes a
    memory file rather than crashing."""
    docs = tmp_path / "docs"
    monkeypatch.setenv(DOCS_PATH_ENV, str(docs))
    run_dir = _make_run(tmp_path, with_sources=False)

    dest = write_memory(run_dir, stub_mode=True)

    assert dest is not None
    assert dest.exists()
    body = dest.read_text(encoding="utf-8")
    assert "only the run metadata is recorded" in body


def test_env_var_missing_handled(tmp_path: Path, monkeypatch, caplog):
    """With HELM_STUDIO_DOCS_PATH unset the writer skips silently — returns
    None, writes nothing, and logs a single info line."""
    monkeypatch.delenv(DOCS_PATH_ENV, raising=False)
    run_dir = _make_run(tmp_path)

    with caplog.at_level(logging.INFO, logger="agentic.memory"):
        dest = write_memory(run_dir, stub_mode=True)

    assert dest is None
    skip_lines = [r for r in caplog.records if "not set" in r.getMessage()]
    assert len(skip_lines) == 1


def test_memory_written_event_emitted(tmp_path: Path, monkeypatch):
    """A successful write emits a `memory.written` event with the path,
    size and a summary preview."""
    docs = tmp_path / "docs"
    monkeypatch.setenv(DOCS_PATH_ENV, str(docs))
    run_dir = _make_run(tmp_path)
    events_path = run_dir / "events.jsonl"
    emitter = EventEmitter(events_path)

    dest = write_memory(run_dir, events=emitter, stub_mode=True)
    assert dest is not None

    records = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    memory_events = [r for r in records if r["type"] == "memory.written"]
    assert len(memory_events) == 1
    payload = memory_events[0]["payload"]
    assert payload["path"] == str(dest)
    assert payload["size_bytes"] == dest.stat().st_size
    assert payload["size_bytes"] > 0
    assert payload["summary_preview"]


def test_sdk_path_uses_generated_summary(tmp_path: Path, monkeypatch):
    """When not stubbed and no summary is supplied, the SDK-generated text
    is what lands in the memory file."""
    docs = tmp_path / "docs"
    monkeypatch.setenv(DOCS_PATH_ENV, str(docs))
    run_dir = _make_run(tmp_path)

    async def fake_query(prompt: str) -> str:
        assert "feature" in prompt  # the workflow name reaches the prompt
        return "SDK-generated memory summary."

    monkeypatch.setattr("agentic.memory._query_summary", fake_query)

    dest = write_memory(run_dir)

    assert dest is not None
    assert "SDK-generated memory summary." in dest.read_text(encoding="utf-8")
