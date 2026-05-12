"""Watch-TUI tests: model logic + a textual smoke test."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic.watch.models import AgentState, RunState, TranscriptEntry
from agentic.watch.panes import render_transcript_plain
from agentic.watch.tail import Tailer, iter_events


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _fixture_events() -> list[dict]:
    """A representative event stream — run.start, two agents, run.complete."""
    return [
        {"ts": "2026-05-12T10:00:00.000Z", "type": "run.start", "agent": None,
         "payload": {"workflow": "feature", "agent_count": 2,
                     "branch": "agentic/feature-abc12345",
                     "target_repo": "/tmp/repo", "stub_mode": False}},
        {"ts": "2026-05-12T10:00:00.100Z", "type": "agent.start", "agent": "spec",
         "payload": {"agent_id": "spec", "prompt_file": "prompts/spec.md",
                     "allowed_tools": ["Read", "Write"], "inputs": ["task"]}},
        {"ts": "2026-05-12T10:00:01.000Z", "type": "assistant.text", "agent": "spec",
         "payload": {"agent_id": "spec", "text": "Drafting the spec..."}},
        {"ts": "2026-05-12T10:00:02.000Z", "type": "tool.use", "agent": "spec",
         "payload": {"agent_id": "spec", "tool_name": "Write",
                     "tool_input": '{"file_path": "SPEC.md"}'}},
        {"ts": "2026-05-12T10:00:02.100Z", "type": "tool.result", "agent": "spec",
         "payload": {"agent_id": "spec", "tool_use_id": "x",
                     "success": True, "content": "File written"}},
        {"ts": "2026-05-12T10:00:03.000Z", "type": "agent.complete", "agent": "spec",
         "payload": {"agent_id": "spec", "status": "success",
                     "outputs": ["SPEC.md"], "elapsed_seconds": 2.9}},
        {"ts": "2026-05-12T10:00:03.100Z", "type": "agent.start", "agent": "explore",
         "payload": {"agent_id": "explore", "prompt_file": "prompts/explore.md",
                     "allowed_tools": ["Read", "Grep"], "inputs": ["SPEC.md"]}},
        {"ts": "2026-05-12T10:00:05.000Z", "type": "agent.complete", "agent": "explore",
         "payload": {"agent_id": "explore", "status": "success",
                     "outputs": ["CONTEXT.md"], "elapsed_seconds": 1.9}},
        {"ts": "2026-05-12T10:00:05.100Z", "type": "run.complete", "agent": None,
         "payload": {"status": "success", "elapsed_seconds": 5.1,
                     "failed_agent": None}},
    ]


def test_runstate_apply_aggregates_events():
    state = RunState(run_id="20260512-100000-abc12345")
    for ev in _fixture_events():
        state.apply(ev)

    assert state.workflow == "feature"
    assert state.branch == "agentic/feature-abc12345"
    assert state.status == "success"
    assert state.is_terminal
    assert state.elapsed_seconds == 5.1
    assert state.agent_order == ["spec", "explore"]
    assert state.agents["spec"].status == "success"
    assert state.agents["spec"].elapsed_seconds == 2.9
    assert state.agents["spec"].outputs == ["SPEC.md"]
    transcript = state.agents["spec"].transcript
    assert [e.kind for e in transcript] == ["text", "tool_use", "tool_result"]
    assert transcript[0].text == "Drafting the spec..."
    assert transcript[1].tool_name == "Write"
    assert transcript[2].success is True


def test_runstate_apply_failed_agent():
    state = RunState(run_id="r")
    state.apply({"ts": "t1", "type": "run.start", "agent": None,
                 "payload": {"workflow": "w", "agent_count": 1, "branch": "b",
                             "target_repo": "/r", "stub_mode": False}})
    state.apply({"ts": "t2", "type": "agent.start", "agent": "spec",
                 "payload": {"agent_id": "spec", "inputs": []}})
    state.apply({"ts": "t3", "type": "agent.fail", "agent": "spec",
                 "payload": {"agent_id": "spec", "error": "boom"}})
    state.apply({"ts": "t4", "type": "run.complete", "agent": None,
                 "payload": {"status": "failed", "failed_agent": "spec",
                             "elapsed_seconds": 1.0}})
    assert state.status == "failed"
    assert state.failed_agent == "spec"
    assert state.agents["spec"].status == "failed"
    assert state.agents["spec"].error == "boom"


def test_runstate_ignores_unknown_event_type():
    state = RunState(run_id="r")
    state.apply({"ts": "t", "type": "future.event", "agent": None, "payload": {}})
    # nothing crashes; nothing changes
    assert state.status == "pending"


def test_tailer_returns_only_new_events_since_last_call(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"type": "a", "agent": None, "payload": {}}) + "\n")
    t = Tailer(path)
    first = t.read_new()
    assert len(first) == 1

    # nothing new
    assert t.read_new() == []

    # append two
    with path.open("a") as f:
        f.write(json.dumps({"type": "b", "agent": None, "payload": {}}) + "\n")
        f.write(json.dumps({"type": "c", "agent": None, "payload": {}}) + "\n")
    new = t.read_new()
    assert [e["type"] for e in new] == ["b", "c"]


def test_tailer_handles_nonexistent_file(tmp_path: Path):
    t = Tailer(tmp_path / "nope.jsonl")
    assert t.read_new() == []


def test_render_transcript_plain_no_markup():
    """Plain rendering must strip rich markup and produce readable sections."""
    a = AgentState(id="spec", status="success", elapsed_seconds=0.5)
    a.transcript = [
        TranscriptEntry(ts="t1", kind="text", text="hello world"),
        TranscriptEntry(ts="t2", kind="tool_use", tool_name="Read",
                        tool_input='{"file_path": "x.py"}'),
        TranscriptEntry(ts="t3", kind="tool_result", text="ok content",
                        success=True),
        TranscriptEntry(ts="t4", kind="tool_result", text="boom",
                        success=False),
    ]
    out = render_transcript_plain(a)
    # no rich markup tokens leak through
    for marker in ("[blue]", "[/]", "[yellow]", "[green]", "[red]"):
        assert marker not in out, f"{marker!r} should not appear in plain output"
    assert "=== agent: spec (success, 0.500s) ===" in out
    assert "[assistant]\nhello world" in out
    assert "[tool: Read]" in out
    assert '{"file_path": "x.py"}' in out
    assert "[result: ok]" in out
    assert "[result: err]" in out


def test_render_transcript_plain_failed_agent_includes_error():
    a = AgentState(id="spec", status="failed", error="missing output SPEC.md")
    out = render_transcript_plain(a)
    assert "=== agent: spec (failed) ===" in out
    assert "[error]" in out
    assert "missing output SPEC.md" in out


def test_iter_events_skips_malformed(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"type": "a", "agent": None, "payload": {}}) + "\n"
        + "this is not json\n"
        + "\n"
        + json.dumps({"type": "b", "agent": None, "payload": {}}) + "\n"
    )
    assert [e["type"] for e in iter_events(path)] == ["a", "b"]


# --- Textual smoke test --------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_app_mounts_without_crashing(tmp_path: Path):
    """App opens against a complete-run fixture and shows the agent list."""
    pytest.importorskip("textual")
    from agentic.watch.app import WatchApp

    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, _fixture_events())

    app = WatchApp(events_path=events_path, run_id="20260512-100000-abc12345")
    async with app.run_test() as pilot:
        # let on_mount run
        await pilot.pause()
        # state populated from fixture
        assert app.state.status == "success"
        assert app.state.agent_order == ["spec", "explore"]
        # the list view has two rows
        lv = app.query_one("#agent-list")
        assert len(lv.children) == 2
        # quit cleanly
        await pilot.press("q")


@pytest.mark.asyncio
async def test_watch_app_handles_empty_events_file(tmp_path: Path):
    """Mounting against an empty file shouldn't crash."""
    pytest.importorskip("textual")
    from agentic.watch.app import WatchApp

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    app = WatchApp(events_path=events_path, run_id="empty")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.state.status == "pending"
        assert app.state.agent_order == []
        await pilot.press("q")


@pytest.mark.asyncio
async def test_agent_list_handles_incremental_agent_arrival(tmp_path: Path):
    """Regression: appending agents one-at-a-time used to throw DuplicateIds.

    `ListView.clear()` in textual returns an AwaitRemove — in a sync caller
    the old children weren't gone yet when new ones with the same ID were
    appended. The fix is to never clear (agent_order is append-only).
    """
    pytest.importorskip("textual")
    from agentic.watch.app import WatchApp

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    app = WatchApp(events_path=events_path, run_id="r")
    async with app.run_test() as pilot:
        await pilot.pause()

        events = [
            {"ts": "t1", "type": "run.start", "agent": None,
             "payload": {"workflow": "feature", "agent_count": 3, "branch": "b",
                         "target_repo": "/r", "stub_mode": True}},
            {"ts": "t2", "type": "agent.start", "agent": "spec",
             "payload": {"agent_id": "spec", "inputs": []}},
            {"ts": "t3", "type": "agent.complete", "agent": "spec",
             "payload": {"agent_id": "spec", "status": "success",
                         "outputs": ["SPEC.md"], "elapsed_seconds": 0.1}},
            {"ts": "t4", "type": "agent.start", "agent": "explore",
             "payload": {"agent_id": "explore", "inputs": ["SPEC.md"]}},
            {"ts": "t5", "type": "agent.complete", "agent": "explore",
             "payload": {"agent_id": "explore", "status": "success",
                         "outputs": ["CONTEXT.md"], "elapsed_seconds": 0.1}},
        ]
        for ev in events:
            app.state.apply(ev)
            app._refresh_ui()
            await pilot.pause()

        assert app.state.agent_order == ["spec", "explore"]
        lv = app.query_one("#agent-list")
        assert len(lv.children) == 2
        await pilot.press("q")


@pytest.mark.asyncio
async def test_copy_transcript_action_runs_without_crashing(tmp_path: Path):
    """Pressing Ctrl+C should invoke action_copy_transcript and not raise."""
    pytest.importorskip("textual")
    from agentic.watch.app import WatchApp

    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, _fixture_events())

    app = WatchApp(events_path=events_path, run_id="r")
    async with app.run_test() as pilot:
        await pilot.pause()
        # invoke the action directly — pressing ctrl+c in a Pilot may not
        # round-trip the same way as a real terminal, so we call the action.
        app.action_copy_transcript()
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_copy_transcript_action_with_no_agents(tmp_path: Path):
    """If no agents have arrived yet, copy_transcript must not crash."""
    pytest.importorskip("textual")
    from agentic.watch.app import WatchApp

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    app = WatchApp(events_path=events_path, run_id="empty")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_copy_transcript()  # should notify, not raise
        await pilot.pause()
        await pilot.press("q")
