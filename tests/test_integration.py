"""End-to-end stub-mode pipeline against a real git repo.

Exercises the wiring: branch creation, sequential agent execution, declared-
output verification, and dirty-tree refusal. Stub mode skips SDK calls so this
runs in CI without ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agentic.context import RunContext
from agentic.runner import DirtyWorkingTree, run_workflow
from agentic.workflow import Workflow

SCAFFOLD = Path(__file__).parent.parent / "src" / "agentic" / "scaffold"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# fixture repo\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def _scaffold_feature(repo: Path) -> None:
    """Copy the feature workflow + prompts into the repo's .agentic/."""
    (repo / ".agentic" / "workflows").mkdir(parents=True)
    (repo / ".agentic" / "prompts").mkdir(parents=True)
    (repo / ".agentic" / ".gitignore").write_text("runs/\n")
    shutil.copy(SCAFFOLD / "workflows" / "feature.yaml",
                repo / ".agentic" / "workflows" / "feature.yaml")
    for p in (SCAFFOLD / "prompts").glob("*.md"):
        shutil.copy(p, repo / ".agentic" / "prompts" / p.name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "scaffold .agentic")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _scaffold_feature(repo)
    return repo


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def test_feature_pipeline_stub_end_to_end(git_repo: Path):
    """Full 6-agent feature workflow in stub mode against a git repo."""
    wf = Workflow.find("feature", git_repo)
    assert [a.id for a in wf.agents] == [
        "spec", "explore", "implement", "test", "review", "pr",
    ]

    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=git_repo,
        inputs={"task": "add a --dry-run flag to the deploy command"},
        stub_mode=True,
    )

    starting_branch = _current_branch(git_repo)
    run_workflow(wf, ctx)

    # Branch was created from HEAD and checked out.
    expected_branch = f"agentic/feature-{ctx.short_id}"
    assert ctx.branch == expected_branch
    assert _current_branch(git_repo) == expected_branch
    assert starting_branch != expected_branch

    # All six declared outputs exist with stub content.
    expected_outputs = ["SPEC.md", "CONTEXT.md", "CHANGES.md",
                        "TEST_NOTES.md", "REVIEW.md", "PR_BODY.md"]
    for fname in expected_outputs:
        path = ctx.working_dir / fname
        assert path.exists(), f"missing {fname}"
        text = path.read_text()
        assert "[stub] agent" in text, f"{fname} not produced by stub: {text!r}"

    # events.jsonl tracks the run end-to-end.
    import json
    events_path = ctx.working_dir / "events.jsonl"
    assert events_path.exists()
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "run.start"
    assert types[-1] == "run.complete"
    assert events[-1]["payload"]["status"] == "success"
    starts = [e["payload"]["agent_id"] for e in events if e["type"] == "agent.start"]
    completes = [e["payload"]["agent_id"] for e in events if e["type"] == "agent.complete"]
    assert starts == ["spec", "explore", "implement", "test", "review", "pr"]
    assert completes == starts


def test_dirty_tree_refused(git_repo: Path):
    """Runner refuses to run when the target repo has uncommitted changes."""
    (git_repo / "dirty.txt").write_text("uncommitted\n")
    wf = Workflow.find("feature", git_repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=git_repo,
        inputs={"task": "x"},
        stub_mode=True,
    )
    with pytest.raises(DirtyWorkingTree, match="uncommitted"):
        run_workflow(wf, ctx)

    # No branch was created.
    assert ctx.branch is None
    assert _current_branch(git_repo) == "main"


def test_branch_is_from_current_head_not_main(git_repo: Path):
    """Branch is created from wherever the developer currently is, not main."""
    _git(git_repo, "checkout", "-b", "my-feature-branch")
    (git_repo / "wip.txt").write_text("wip\n")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "wip")
    parent_sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    wf = Workflow.find("feature", git_repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=git_repo,
        inputs={"task": "x"},
        stub_mode=True,
    )
    run_workflow(wf, ctx)

    # Parent of the agentic branch's tip is the commit we made on my-feature-branch.
    # In stub mode no commits happen, so the tip itself == parent_sha.
    tip = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    assert tip == parent_sha


def test_run_dir_persists_after_failure(tmp_path: Path):
    """If an agent fails, the working dir stays put (for inspection)."""
    from agentic.runner import AgentFailure

    repo = tmp_path / "repo"
    _init_repo(repo)
    _scaffold_feature(repo)

    wf = Workflow.find("feature", repo)
    # No task input → spec agent's resolve_input fails.
    ctx = RunContext.create(
        workflow_name=wf.name, target_repo_path=repo, inputs={}, stub_mode=True,
    )
    with pytest.raises(AgentFailure) as exc:
        run_workflow(wf, ctx)
    assert exc.value.failed_agent == "spec"
    assert ctx.working_dir.exists()
