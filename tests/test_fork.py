"""`agentic fork` — copy a run's state + outputs up to a step, resume there."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic.context import RunContext
from agentic.runner import fork_run, run_workflow
from agentic.state import RunState
from agentic.workflow import Workflow


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(l)
        for l in (run_dir / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]


def _source_run(repo: Path) -> RunContext:
    """A completed 3-agent stub run (spec -> explore -> plan)."""
    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=repo,
        inputs={"task": "original task"},
        stub_mode=True,
    )
    run_workflow(wf, ctx)
    return ctx


@pytest.mark.parametrize("step", [0, 1, 2, 3])
def test_fork_at_step_roundtrips(repo: Path, step: int) -> None:
    """Forking at any valid step yields a fresh, succeeded run with every
    output present — carried over for agents before `step`, regenerated after.
    """
    src = _source_run(repo)
    forked = fork_run(src.working_dir, step=step, target_repo_path=repo)

    assert forked.working_dir != src.working_dir
    assert forked.working_dir.exists()

    state = RunState.load(forked.working_dir)
    assert state.status == "succeeded"
    assert state.forked_from == src.run_id
    assert state.fork_step == step

    for name in ("SPEC.md", "CONTEXT.md", "PLAN.md"):
        assert (forked.working_dir / name).exists(), name

    # source run is untouched
    assert RunState.load(src.working_dir).run_id == src.run_id


def test_fork_copies_carried_outputs_verbatim(repo: Path) -> None:
    src = _source_run(repo)
    spec_before = (src.working_dir / "SPEC.md").read_text()
    context_before = (src.working_dir / "CONTEXT.md").read_text()

    forked = fork_run(src.working_dir, step=2, target_repo_path=repo)

    # agents 0..1 (spec, explore) were carried over byte-for-byte
    assert (forked.working_dir / "SPEC.md").read_text() == spec_before
    assert (forked.working_dir / "CONTEXT.md").read_text() == context_before


def test_fork_emits_run_fork_event(repo: Path) -> None:
    src = _source_run(repo)
    forked = fork_run(src.working_dir, step=1, target_repo_path=repo)

    events = _events(forked.working_dir)
    assert events[0]["type"] == "run.fork"
    assert events[0]["payload"]["forked_from"] == src.run_id
    assert events[0]["payload"]["fork_step"] == 1
    assert "run.resume" in [e["type"] for e in events]


def test_fork_task_override(repo: Path) -> None:
    src = _source_run(repo)
    forked = fork_run(
        src.working_dir, step=1, target_repo_path=repo, task="a different task"
    )
    assert RunState.load(forked.working_dir).inputs["task"] == "a different task"
    # the source run's inputs are not mutated
    assert RunState.load(src.working_dir).inputs["task"] == "original task"


def test_fork_extra_inputs_merge(repo: Path) -> None:
    src = _source_run(repo)
    forked = fork_run(
        src.working_dir, step=1, target_repo_path=repo, extra_inputs={"flag": "on"}
    )
    inputs = RunState.load(forked.working_dir).inputs
    assert inputs["flag"] == "on"
    assert inputs["task"] == "original task"  # untouched source input preserved


def test_fork_rejects_out_of_range_step(repo: Path) -> None:
    src = _source_run(repo)
    with pytest.raises(ValueError, match="out of range"):
        fork_run(src.working_dir, step=99, target_repo_path=repo)


def test_fork_rejects_missing_carried_outputs(repo: Path) -> None:
    """Forking past the point the source actually reached fails cleanly."""
    src = _source_run(repo)
    (src.working_dir / "PLAN.md").unlink()
    with pytest.raises(ValueError, match="missing carried-over outputs"):
        fork_run(src.working_dir, step=3, target_repo_path=repo)


def test_fork_from_a_paused_source(repo: Path) -> None:
    """A fork can be taken from a run that paused mid-pipeline."""
    body = {
        "name": "pw",
        "agents": [
            {"id": "a", "outputs": ["A.md"], "pause_after": True},
            {"id": "b", "inputs": ["A.md"], "outputs": ["B.md"]},
        ],
    }
    wf_path = repo / ".agentic" / "workflows" / "pw.yaml"
    wf_path.write_text(yaml.safe_dump(body))
    wf = Workflow.load(wf_path)
    ctx = RunContext.create(
        workflow_name="pw", target_repo_path=repo, inputs={"task": "t"}, stub_mode=True
    )
    run_workflow(wf, ctx)
    assert RunState.load(ctx.working_dir).status == "paused"

    # A.md exists, so a fork from step 1 roundtrips to completion
    forked = fork_run(ctx.working_dir, step=1, target_repo_path=repo)
    state = RunState.load(forked.working_dir)
    assert state.status == "succeeded"
    assert (forked.working_dir / "A.md").exists()
    assert (forked.working_dir / "B.md").exists()
