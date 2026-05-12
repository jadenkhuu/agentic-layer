from __future__ import annotations

import logging
import subprocess
import time

from agentic.agent import AgentSpec, run_agent
from agentic.auth import AuthMethod, detect_auth
from agentic.context import RunContext
from agentic.events import EventEmitter
from agentic.workflow import Workflow

logger = logging.getLogger(__name__)


class AgentFailure(RuntimeError):
    """Raised when an agent halts the workflow.

    The original exception (if any) is chained via __cause__. The run's working
    directory and any created branch are left intact for inspection.
    """

    def __init__(self, failed_agent: str, message: str):
        super().__init__(f"agent '{failed_agent}' failed: {message}")
        self.failed_agent = failed_agent


class DirtyWorkingTree(RuntimeError):
    """Refused to run because the target repo has uncommitted changes."""


def run_workflow(workflow: Workflow, ctx: RunContext) -> RunContext:
    logger.info("run %s start :: workflow=%s agents=%d stub=%s",
                ctx.run_id, workflow.name, len(workflow.agents), ctx.stub_mode)

    if not ctx.stub_mode:
        _log_auth_method()

    _maybe_prepare_branch(ctx)

    # Emitter is wired up only after auth + branch checks pass — refused runs
    # leave no events.jsonl behind.
    ctx.events = EventEmitter(ctx.working_dir / "events.jsonl")
    start_t = time.monotonic()
    ctx.events.emit(
        "run.start",
        workflow=workflow.name,
        agent_count=len(workflow.agents),
        branch=ctx.branch,
        target_repo=str(ctx.target_repo_path),
        stub_mode=ctx.stub_mode,
    )

    try:
        for spec in workflow.agents:
            _run_one(spec, ctx)
    except AgentFailure as e:
        ctx.events.emit(
            "run.complete",
            status="failed",
            elapsed_seconds=round(time.monotonic() - start_t, 3),
            failed_agent=e.failed_agent,
        )
        raise
    except Exception:
        ctx.events.emit(
            "run.complete",
            status="failed",
            elapsed_seconds=round(time.monotonic() - start_t, 3),
            failed_agent=None,
        )
        raise

    ctx.events.emit(
        "run.complete",
        status="success",
        elapsed_seconds=round(time.monotonic() - start_t, 3),
        failed_agent=None,
    )
    logger.info("run %s complete", ctx.run_id)
    return ctx


def _log_auth_method() -> None:
    """Report which auth path is active. Fails fast if neither is configured."""
    method = detect_auth()  # raises NoAuthConfigured before any agent runs
    if method is AuthMethod.API_KEY:
        logger.warning("auth: ANTHROPIC_API_KEY (billing: API account)")
    else:
        logger.info("auth: claude CLI login (billing: Max/Pro plan)")


def _maybe_prepare_branch(ctx: RunContext) -> None:
    """Create `agentic/<workflow>-<short-id>` from current HEAD.

    Skipped (with a log line) if the target repo is not a git repo — this keeps
    the runner usable in tests against plain temp dirs. If it IS a git repo and
    the working tree is dirty, we refuse to run.
    """
    if not (ctx.target_repo_path / ".git").exists():
        logger.info("run %s :: not a git repo, skipping branch management", ctx.run_id)
        return

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ctx.target_repo_path, capture_output=True, text=True, check=True,
    )
    if status.stdout.strip():
        raise DirtyWorkingTree(
            "working tree has uncommitted changes; commit or stash first.\n"
            f"{status.stdout}"
        )

    base = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ctx.target_repo_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    ctx.base_branch = base if base != "HEAD" else None  # detached HEAD → unknown

    branch = f"agentic/{ctx.workflow_name}-{ctx.short_id}"
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=ctx.target_repo_path, check=True,
        capture_output=True, text=True,
    )
    ctx.branch = branch
    logger.info("run %s :: created branch %s", ctx.run_id, branch)


def _run_one(spec: AgentSpec, ctx: RunContext) -> None:
    try:
        run_agent(spec, ctx)
    except Exception as e:
        logger.error("run %s halted at agent %s: %s", ctx.run_id, spec.id, e)
        ctx.events.emit("agent.fail", agent=spec.id, agent_id=spec.id, error=str(e))
        raise AgentFailure(spec.id, str(e)) from e

    missing = [o for o in spec.outputs if not (ctx.working_dir / o).exists()]
    if missing:
        msg = f"missing declared outputs: {missing}"
        logger.error("run %s halted: agent %s did not produce outputs %s",
                     ctx.run_id, spec.id, missing)
        ctx.events.emit("agent.fail", agent=spec.id, agent_id=spec.id, error=msg)
        raise AgentFailure(spec.id, msg)
