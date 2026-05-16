"""Phase 7 deploy hook — `deploy.trigger` event emission.

A successful run that produced a PR emits one `deploy.trigger` event; helm's
run sync consumes it to fire a Vercel preview deploy. Non-PR workflows emit
nothing.
"""
import json
from pathlib import Path

from agentic.agent import AgentSpec
from agentic.context import RunContext
from agentic.events import AgenticEventType, EventEmitter
from agentic.runner import _find_pr_ref, _maybe_emit_deploy_trigger, run_workflow
from agentic.state import RunState
from agentic.workflow import Workflow

PR_BODY = """# feat: add a thing

Adds a thing to the codebase.

PR: https://github.com/purpl/caat/pull/318
"""


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pr_workflow() -> Workflow:
    """A one-agent workflow whose agent declares a PR_BODY.md output."""
    return Workflow(
        name="ship",
        agents=[AgentSpec(id="pr", inputs=["task"], outputs=["PR_BODY.md"])],
    )


def _ctx(tmp_path: Path, **kw) -> RunContext:
    return RunContext(
        workflow_name="ship",
        target_repo_path=tmp_path,
        working_dir=tmp_path,
        **kw,
    )


# --------------------------------------------------------------------------
# event-type enum
# --------------------------------------------------------------------------

def test_event_type_enum_includes_deploy_trigger():
    assert AgenticEventType.DEPLOY_TRIGGER.value == "deploy.trigger"
    # str-enum: JSON-serialises as the bare string
    assert isinstance(AgenticEventType.DEPLOY_TRIGGER, str)
    assert json.dumps({"type": AgenticEventType.DEPLOY_TRIGGER}) == (
        '{"type": "deploy.trigger"}'
    )


# --------------------------------------------------------------------------
# _find_pr_ref
# --------------------------------------------------------------------------

def test_find_pr_ref_extracts_url_and_number(tmp_path: Path):
    (tmp_path / "PR_BODY.md").write_text(PR_BODY)
    number, url = _find_pr_ref(_pr_workflow(), _ctx(tmp_path))
    assert number == 318
    assert url == "https://github.com/purpl/caat/pull/318"


def test_find_pr_ref_falls_back_to_bare_number(tmp_path: Path):
    (tmp_path / "PR_BODY.md").write_text("opened PR #42 against main")
    number, url = _find_pr_ref(_pr_workflow(), _ctx(tmp_path))
    assert number == 42
    assert url is None


def test_find_pr_ref_none_when_no_pr_output(tmp_path: Path):
    # no PR_BODY.md written — non-PR run
    assert _find_pr_ref(_pr_workflow(), _ctx(tmp_path)) == (None, None)


# --------------------------------------------------------------------------
# _maybe_emit_deploy_trigger
# --------------------------------------------------------------------------

def test_maybe_emit_deploy_trigger_emits_when_pr_present(tmp_path: Path):
    ctx = _ctx(tmp_path, branch="agentic/ship-abc123", base_branch="main")
    ctx.events = EventEmitter(tmp_path / "events.jsonl")
    (tmp_path / "PR_BODY.md").write_text(PR_BODY)

    _maybe_emit_deploy_trigger(_pr_workflow(), ctx)

    events = _read(tmp_path / "events.jsonl")
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "deploy.trigger"
    p = ev["payload"]
    assert p["branch"] == "agentic/ship-abc123"
    assert p["base_branch"] == "main"
    assert p["pr_number"] == 318
    assert p["pr_url"] == "https://github.com/purpl/caat/pull/318"
    assert p["sha"] is None  # tmp_path is not a git repo
    assert p["target_repo"] == str(tmp_path)


def test_maybe_emit_deploy_trigger_noop_without_pr(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.events = EventEmitter(tmp_path / "events.jsonl")

    _maybe_emit_deploy_trigger(_pr_workflow(), ctx)

    # no PR output -> no event file written at all
    assert not (tmp_path / "events.jsonl").exists()


def test_maybe_emit_deploy_trigger_persists_pr_ref_to_state(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.events = EventEmitter(tmp_path / "events.jsonl")
    (tmp_path / "PR_BODY.md").write_text(PR_BODY)
    RunState(
        run_id="r", workflow_name="ship", target_repo_path=str(tmp_path)
    ).save(tmp_path)

    _maybe_emit_deploy_trigger(_pr_workflow(), ctx)

    state = RunState.load(tmp_path)
    assert state.pr_number == 318
    assert state.pr_url == "https://github.com/purpl/caat/pull/318"


# --------------------------------------------------------------------------
# run_workflow wiring
# --------------------------------------------------------------------------

def test_run_workflow_emits_deploy_trigger_before_run_complete(
    tmp_path: Path, monkeypatch
):
    """A successful run that produced a PR emits deploy.trigger, and it lands
    before the terminal run.complete event so a tailing consumer sees it.
    """
    import agentic.runner as runner_mod

    def fake_run_agent(spec: AgentSpec, ctx: RunContext) -> None:
        (ctx.working_dir / "PR_BODY.md").write_text(PR_BODY)

    monkeypatch.setattr(runner_mod, "run_agent", fake_run_agent)

    ctx = RunContext.create(
        workflow_name="ship",
        target_repo_path=tmp_path,
        inputs={"task": "ship it"},
        stub_mode=True,
    )
    run_workflow(_pr_workflow(), ctx)

    events = _read(ctx.working_dir / "events.jsonl")
    types = [e["type"] for e in events]
    assert "deploy.trigger" in types
    assert types.index("deploy.trigger") < types.index("run.complete")
    deploy = next(e for e in events if e["type"] == "deploy.trigger")
    assert deploy["payload"]["pr_number"] == 318
    assert deploy["payload"]["pr_url"] == "https://github.com/purpl/caat/pull/318"


def test_run_workflow_no_deploy_trigger_for_non_pr_workflow(repo: Path):
    """test-workflow declares no PR_* output — a clean run emits no deploy hook."""
    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=repo,
        inputs={"task": "x"},
        stub_mode=True,
    )
    run_workflow(wf, ctx)

    events = _read(ctx.working_dir / "events.jsonl")
    assert "deploy.trigger" not in [e["type"] for e in events]
