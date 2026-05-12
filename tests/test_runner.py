from pathlib import Path

import pytest

from agentic.agent import AgentSpec
from agentic.context import RunContext
from agentic.runner import AgentFailure, run_workflow
from agentic.workflow import Workflow


def test_happy_path_full_pipeline(repo: Path):
    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=repo,
        inputs={"task": "build a thing"},
        stub_mode=True,
    )

    run_workflow(wf, ctx)

    for fname in ["SPEC.md", "CONTEXT.md", "PLAN.md"]:
        assert (ctx.working_dir / fname).exists(), f"missing {fname}"

    spec_text = (ctx.working_dir / "SPEC.md").read_text()
    assert "agent spec" in spec_text
    assert "['task']" in spec_text


def test_run_dir_is_unique_per_run(repo: Path):
    wf = Workflow.find("test-workflow", repo)
    a = RunContext.create(workflow_name=wf.name, target_repo_path=repo, inputs={"task": "a"}, stub_mode=True)
    b = RunContext.create(workflow_name=wf.name, target_repo_path=repo, inputs={"task": "b"}, stub_mode=True)
    assert a.run_id != b.run_id
    assert a.working_dir != b.working_dir
    assert a.working_dir.exists() and b.working_dir.exists()


def test_missing_input_halts(repo: Path):
    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=repo, inputs={}, stub_mode=True)

    with pytest.raises(AgentFailure) as exc:
        run_workflow(wf, ctx)
    assert exc.value.failed_agent == "spec"
    assert ctx.working_dir.exists(), "working dir must be left for inspection"


def test_missing_declared_output_halts(repo: Path, monkeypatch):
    """If an agent fails to produce its declared outputs, the runner halts and the
    failure names that agent. Simulate by patching run_agent to be a no-op."""
    import agentic.runner as runner_mod

    def silent_agent(spec: AgentSpec, ctx: RunContext) -> None:
        return  # writes nothing

    monkeypatch.setattr(runner_mod, "run_agent", silent_agent)

    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=repo, inputs={"task": "x"}, stub_mode=True)

    with pytest.raises(AgentFailure) as exc:
        run_workflow(wf, ctx)
    assert exc.value.failed_agent == "spec"
    assert "SPEC.md" in str(exc.value)


def test_halt_stops_subsequent_agents(repo: Path, monkeypatch):
    import agentic.runner as runner_mod

    calls: list[str] = []
    real_run_agent = runner_mod.run_agent

    def tracked(spec: AgentSpec, ctx: RunContext) -> None:
        calls.append(spec.id)
        if spec.id == "explore":
            raise RuntimeError("boom")
        real_run_agent(spec, ctx)

    monkeypatch.setattr(runner_mod, "run_agent", tracked)

    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=repo, inputs={"task": "x"}, stub_mode=True)

    with pytest.raises(AgentFailure):
        run_workflow(wf, ctx)
    assert calls == ["spec", "explore"], "plan should not have run after explore failed"


def test_run_context_resolves_kv_then_file(repo: Path):
    ctx = RunContext.create(workflow_name="t", target_repo_path=repo, inputs={"task": "hello"})
    assert ctx.resolve_input("task") == "hello"

    (ctx.working_dir / "NOTES.md").write_text("from file")
    assert ctx.resolve_input("NOTES.md") == "from file"

    with pytest.raises(KeyError):
        ctx.resolve_input("missing")
