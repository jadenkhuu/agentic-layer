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


def test_resume_with_feedback_records_event_and_input(tmp_path: Path) -> None:
    """`agentic resume --feedback` (HITL) should record the feedback as a
    `hitl.feedback` event and fold it into the run inputs so the next agent
    can read it. This is the contract helm's client portal depends on."""
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    assert RunState.load(ctx.working_dir).status == "paused"

    resume_run(ctx.working_dir, wf, feedback="tighten the hero copy")
    final = RunState.load(ctx.working_dir)
    assert final.status == "succeeded"
    assert final.inputs["hitl_feedback"] == "tighten the hero copy"

    fb = [e for e in _events(ctx.working_dir) if e["type"] == "hitl.feedback"]
    assert len(fb) == 1
    assert fb[0]["payload"]["text"] == "tighten the hero copy"


def test_resume_without_feedback_adds_no_event(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    resume_run(ctx.working_dir, wf)
    final = RunState.load(ctx.working_dir)
    assert "hitl_feedback" not in final.inputs
    assert not any(e["type"] == "hitl.feedback" for e in _events(ctx.working_dir))


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


# ---------------------------------------------------------------------------
# pause_reason
# ---------------------------------------------------------------------------


def _wf_with_pause_reason(tmp_path: Path, reason: str | None) -> Workflow:
    """Two-agent workflow whose first agent pauses, optionally with an
    explicit pause_reason on the spec.
    """
    first: dict = {"id": "a", "outputs": ["A.md"], "pause_after": True}
    if reason is not None:
        first["pause_reason"] = reason
    body = {"name": "x", "agents": [first, {"id": "b", "inputs": ["A.md"],
                                            "outputs": ["B.md"]}]}
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    return Workflow.load(p)


def test_pause_reason_defaults_when_spec_omits_it(tmp_path: Path) -> None:
    wf = _wf_with_pause_reason(tmp_path, reason=None)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = RunState.load(ctx.working_dir)
    assert state.status == "paused"
    # a non-empty generic reason that names the agent and its successor
    assert state.pause_reason
    assert "a" in state.pause_reason and "b" in state.pause_reason

    pause_evt = [e for e in _events(ctx.working_dir) if e["type"] == "agent.pause"][0]
    assert pause_evt["payload"]["reason"] == state.pause_reason


def test_pause_reason_uses_spec_value_when_set(tmp_path: Path) -> None:
    wf = _wf_with_pause_reason(tmp_path, reason="Approve the spec before build.")
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    state = RunState.load(ctx.working_dir)
    assert state.pause_reason == "Approve the spec before build."

    pause_evt = [e for e in _events(ctx.working_dir) if e["type"] == "agent.pause"][0]
    assert pause_evt["payload"]["reason"] == "Approve the spec before build."


def test_resume_clears_pause_reason(tmp_path: Path) -> None:
    wf = _wf_with_pause_reason(tmp_path, reason="Approve the spec before build.")
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    assert RunState.load(ctx.working_dir).pause_reason

    resume_run(ctx.working_dir, wf)
    final = RunState.load(ctx.working_dir)
    assert final.status == "succeeded"
    assert final.pause_reason is None


# ---------------------------------------------------------------------------
# resume --feedback
# ---------------------------------------------------------------------------


def test_resume_feedback_prepends_to_next_agent_input(tmp_path: Path) -> None:
    """Feedback is prepended to the next agent's primary input — here a file
    output (A.md) produced by the paused agent.
    """
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    original = (ctx.working_dir / "A.md").read_text()

    resume_run(ctx.working_dir, wf, feedback="tighten the scope")
    final = RunState.load(ctx.working_dir)
    assert final.status == "succeeded"
    a_md = (ctx.working_dir / "A.md").read_text()
    assert "tighten the scope" in a_md
    assert original in a_md  # original content preserved below the feedback

    types = [e["type"] for e in _events(ctx.working_dir)]
    assert "run.resume" in types


def test_resume_feedback_prepends_to_kv_task_input(tmp_path: Path) -> None:
    """When the next agent's primary input is a kv key (`task`), feedback is
    folded into that value on state.json.
    """
    body = {
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"], "pause_after": True},
            {"id": "b", "inputs": ["task"], "outputs": ["B.md"]},
        ],
    }
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    wf = Workflow.load(p)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "ship it"}, stub_mode=True)
    run_workflow(wf, ctx)

    resume_run(ctx.working_dir, wf, feedback="revise the copy first")
    final = RunState.load(ctx.working_dir)
    assert final.status == "succeeded"
    assert "revise the copy first" in str(final.inputs["task"])
    assert "ship it" in str(final.inputs["task"])


def test_resume_without_feedback_leaves_inputs_untouched(tmp_path: Path) -> None:
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    before = (ctx.working_dir / "A.md").read_text()
    resume_run(ctx.working_dir, wf)
    assert (ctx.working_dir / "A.md").read_text() == before
