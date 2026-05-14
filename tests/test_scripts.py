"""pre/post script execution: order, capture, halt-on-failure."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic.context import RunContext
from agentic.runner import run_workflow
from agentic.scripts import ScriptFailure, run_script
from agentic.workflow import Workflow


def _read_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(l)
        for l in (run_dir / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]


def _stub_wf(tmp_path: Path, **kw) -> Workflow:
    body = {
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"]},
        ],
    }
    body.update(kw)
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    return Workflow.load(p)


def test_script_result_captures_stdout_and_stderr(tmp_path: Path) -> None:
    r = run_script(
        "echo out; echo err 1>&2",
        phase="pre_run",
        cwd=tmp_path,
        log_dir=tmp_path / "scripts",
    )
    assert r.ok()
    assert "out" in r.stdout
    assert "err" in r.stderr
    # log file written
    assert (tmp_path / "scripts" / "pre_run.log").exists()


def test_script_failure_classification(tmp_path: Path) -> None:
    r = run_script("false", phase="pre_run", cwd=tmp_path, log_dir=tmp_path / "scripts")
    assert not r.ok()
    assert r.exit_code != 0


def test_pre_run_failure_halts_run_before_any_agent(tmp_path: Path) -> None:
    wf = _stub_wf(tmp_path, pre_run="false")
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    with pytest.raises(ScriptFailure):
        run_workflow(wf, ctx)
    events = _read_events(ctx.working_dir)
    types = [e["type"] for e in events]
    assert "script.start" in types
    assert "script.complete" in types
    # no agent fired
    assert "agent.start" not in types


def test_post_run_runs_after_success(tmp_path: Path) -> None:
    marker = tmp_path / "post.txt"
    wf = _stub_wf(tmp_path, post_run=f"echo done > {marker}")
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    assert marker.exists()
    events = _read_events(ctx.working_dir)
    phases = [e["payload"].get("phase") for e in events if e["type"] == "script.complete"]
    assert "post_run" in phases


def test_agent_level_pre_post_run_in_order(tmp_path: Path) -> None:
    """Verify pre runs before agent, post runs after, with the right index."""
    body = {
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"],
             "pre": "echo pre", "post": "echo post"},
        ],
    }
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(body))
    wf = Workflow.load(p)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    events = _read_events(ctx.working_dir)
    # ordering: pre -> agent.start -> agent.complete -> post -> run.complete
    types = [(e["type"], e["payload"].get("phase")) for e in events]
    seq = [t for t in types]
    # find indices
    types_only = [t[0] for t in seq]
    i_pre = types_only.index("script.start")
    i_agent_start = types_only.index("agent.start")
    i_agent_done = types_only.index("agent.complete")
    # 2nd script.start is the post phase
    i_post_start = types_only.index("script.start", i_pre + 1)
    assert i_pre < i_agent_start < i_agent_done < i_post_start

    # log files for pre/post written with -0 suffix (agent index)
    scripts = ctx.working_dir / "scripts"
    assert (scripts / "pre-0.log").exists()
    assert (scripts / "post-0.log").exists()
    assert "pre" in (scripts / "pre-0.log").read_text()
