"""HITL pause / resume / abort behaviour."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic.context import RunContext
from agentic.runner import abort_run, resume_run, run_workflow
from agentic.state import RunState
from agentic.workflow import Workflow


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(l)
        for l in (run_dir / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]


def _two_agent_wf(tmp_path: Path, pause_after_first: bool = True) -> Workflow:
    body = {
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"], "pause_after": pause_after_first},
            {"id": "b", "inputs": ["A.md"], "outputs": ["B.md"]},
        ],
    }
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    return Workflow.load(p)


def test_pause_after_halts_before_next_agent(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = RunState.load(ctx.working_dir)
    assert state.status == "paused"
    assert state.current_agent_index == 1  # next agent to run

    types = [e["type"] for e in _events(ctx.working_dir)]
    assert "agent.pause" in types
    assert "run.complete" not in types  # no completion yet


def test_pause_at_final_agent_completes_normally(tmp_path: Path) -> None:
    """`pause_after` on the last agent has nothing to pause for — run should
    still complete instead of dangling forever.
    """
    body = {
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"]},
            {"id": "b", "inputs": ["A.md"], "outputs": ["B.md"], "pause_after": True},
        ],
    }
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    wf = Workflow.load(p)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = RunState.load(ctx.working_dir)
    assert state.status == "succeeded"


def test_resume_continues_past_pause(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)

    resume_run(ctx.working_dir, wf)
    final = RunState.load(ctx.working_dir)
    assert final.status == "succeeded"
    # both agents' outputs exist
    assert (ctx.working_dir / "A.md").exists()
    assert (ctx.working_dir / "B.md").exists()

    types = [e["type"] for e in _events(ctx.working_dir)]
    assert "run.resume" in types
    assert types[-1] == "run.complete"


def test_resume_refuses_non_paused_run(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path, pause_after_first=False)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    assert RunState.load(ctx.working_dir).status == "succeeded"
    with pytest.raises(RuntimeError, match="paused"):
        resume_run(ctx.working_dir, wf)


def test_abort_marks_paused_run_aborted(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = abort_run(ctx.working_dir)
    assert state.status == "aborted"

    types_after = [e["type"] for e in _events(ctx.working_dir)]
    completes = [t for t in types_after if t == "run.complete"]
    assert completes, "abort should emit a run.complete event"
    # the run.complete event for abort should have status=aborted
    abort_evt = [e for e in _events(ctx.working_dir) if e["type"] == "run.complete"][-1]
    assert abort_evt["payload"]["status"] == "aborted"


def test_abort_is_idempotent_for_terminal_runs(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path, pause_after_first=False)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    # already succeeded; abort is a no-op
    s = abort_run(ctx.working_dir)
    assert s.status == "succeeded"


def test_state_json_persists_inputs_for_resume(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t", "extra": "v"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = RunState.load(ctx.working_dir)
    assert state.inputs == {"task": "t", "extra": "v"}
    assert state.stub_mode is True
