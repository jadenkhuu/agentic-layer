"""`--context-file` / `helm_context`: helm-injected briefing context.

helm assembles a project briefing + studio patterns and passes them to
`agentic run --context-file`. The runner prepends that text to every
agent's effective system prompt (ahead of any client-config conventions)
and records a preview on each `agent.start` event so the injection is
auditable from events.jsonl alone.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from agentic.cli import main
from agentic.context import RunContext
from agentic.runner import resume_run, run_workflow
from agentic.state import RunState
from agentic.workflow import Workflow


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _two_agent_wf(tmp_path: Path, *, pause_after_first: bool = False) -> Workflow:
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


def test_helm_context_in_every_agent_start_preview(tmp_path: Path) -> None:
    """Every `agent.start` event carries the injected context in its
    `system_prompt_preview` field — the Phase 2 acceptance criterion."""
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    marker = "MARKER-BRIEF-7F3A"
    run_workflow(wf, ctx, helm_context=f"PROJECT BRIEFING:\n==========\n{marker}\n")

    starts = [e for e in _events(ctx.working_dir) if e["type"] == "agent.start"]
    assert len(starts) == 2
    for ev in starts:
        assert marker in ev["payload"]["system_prompt_preview"]


def test_no_context_yields_empty_preview(tmp_path: Path) -> None:
    """With no helm context and no client config the preview is empty —
    never missing, so consumers can read the field unconditionally."""
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx)
    starts = [e for e in _events(ctx.working_dir) if e["type"] == "agent.start"]
    assert starts
    for ev in starts:
        assert ev["payload"]["system_prompt_preview"] == ""


def test_helm_context_reaches_stub_prompt(tmp_path: Path) -> None:
    """Stub mode mirrors the system prefix into an `assistant.text` event so
    the injection is verifiable without an SDK round-trip."""
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx, helm_context="STUB-MARKER-9 the briefing body")
    texts = [e for e in _events(ctx.working_dir) if e["type"] == "assistant.text"]
    assert texts
    assert all("STUB-MARKER-9" in t["payload"]["text"] for t in texts)


def test_helm_context_persisted_to_run_dir(tmp_path: Path) -> None:
    """`run_workflow` persists the context to `helm-context.md` so a later
    resume / fork can re-apply it."""
    wf = _two_agent_wf(tmp_path)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx, helm_context="persisted-MARKER body")
    persisted = ctx.working_dir / "helm-context.md"
    assert persisted.exists()
    assert "persisted-MARKER" in persisted.read_text()


def test_helm_context_carried_across_resume(tmp_path: Path) -> None:
    """A run paused at a `pause_after` checkpoint still injects the context
    into the agents that run after `agentic resume` — the context is reloaded
    from the persisted `helm-context.md`."""
    wf = _two_agent_wf(tmp_path, pause_after_first=True)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    run_workflow(wf, ctx, helm_context="resume-MARKER briefing")
    assert RunState.load(ctx.working_dir).status == "paused"

    resume_run(ctx.working_dir, wf)
    assert RunState.load(ctx.working_dir).status == "succeeded"

    starts = [e for e in _events(ctx.working_dir) if e["type"] == "agent.start"]
    assert len(starts) == 2  # agent 'b' started after the resume
    assert "resume-MARKER" in starts[1]["payload"]["system_prompt_preview"]


def test_context_file_cli_option() -> None:
    """`agentic run --context-file <path>` reads the file and injects it."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".agentic/workflows").mkdir(parents=True)
        Path(".agentic/workflows/x.yaml").write_text(yaml.safe_dump({
            "name": "x",
            "agents": [{"id": "a", "inputs": ["task"], "outputs": ["A.md"]}],
        }))
        Path("ctx.md").write_text("PROJECT BRIEFING:\n==========\nCLI-MARKER-42\n")

        result = runner.invoke(
            main, ["run", "x", "--task", "t", "--stub", "--context-file", "ctx.md"]
        )
        assert result.exit_code == 0, result.output

        runs = [d for d in Path(".agentic/runs").iterdir() if d.is_dir()]
        assert len(runs) == 1
        events = _events(runs[0])
        starts = [e for e in events if e["type"] == "agent.start"]
        assert starts
        assert "CLI-MARKER-42" in starts[0]["payload"]["system_prompt_preview"]
        assert (runs[0] / "helm-context.md").exists()


def test_context_file_missing_is_a_clean_error() -> None:
    """A `--context-file` that does not exist fails fast with exit code 2."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".agentic/workflows").mkdir(parents=True)
        Path(".agentic/workflows/x.yaml").write_text(yaml.safe_dump({
            "name": "x",
            "agents": [{"id": "a", "inputs": ["task"], "outputs": ["A.md"]}],
        }))
        result = runner.invoke(
            main, ["run", "x", "--task", "t", "--stub", "--context-file", "nope.md"]
        )
        assert result.exit_code == 2
        assert "not found" in result.output
