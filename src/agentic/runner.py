from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from agentic.agent import AgentSpec, run_agent
from agentic.auth import AuthMethod, detect_auth
from agentic.client_config import ClientConfig
from agentic.context import RunContext
from agentic.events import EventEmitter
from agentic.scripts import ScriptFailure, run_script
from agentic.state import RunState
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


class RunPaused(Exception):
    """Internal signal — the run paused cleanly at a pause_after agent.

    Surfaces to callers so `agentic run` can exit 0 with a "paused"
    status. Distinct from AgentFailure (which is a hard halt).
    """

    def __init__(self, run_id: str, working_dir: Path, next_agent: str):
        super().__init__(f"run {run_id} paused before agent '{next_agent}'")
        self.run_id = run_id
        self.working_dir = working_dir
        self.next_agent = next_agent


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_workflow(
    workflow: Workflow,
    ctx: RunContext,
    *,
    client_config: ClientConfig | None = None,
    auto_fix_ci: bool = False,
    max_fix_attempts: int = 3,
) -> RunContext:
    """Run a workflow from agent 0 to the end (or until pause/abort/failure).

    Optional knobs:
      - `client_config`: when set, ctx.client_prefix is filled and a
        client.set event is emitted; agent.py prepends it to prompts.
      - `auto_fix_ci`: after the run's `pr` agent, poll CI and re-invoke a
        `fix` agent on failure (capped by `max_fix_attempts`).
    """
    logger.info("run %s start :: workflow=%s agents=%d stub=%s",
                ctx.run_id, workflow.name, len(workflow.agents), ctx.stub_mode)

    if not ctx.stub_mode:
        _log_auth_method()

    _maybe_prepare_branch(ctx)

    # carry workflow MCP servers + client config onto the ctx so agent.py
    # can consume them per-agent without re-loading workflow YAML.
    ctx.workflow_mcp_servers = list(workflow.mcp_servers)
    if client_config is not None:
        ctx.client_name = client_config.name
        ctx.client_prefix = client_config.as_system_prefix()

    ctx.events = EventEmitter(ctx.working_dir / "events.jsonl")
    start_t = time.monotonic()
    ctx.events.emit(
        "run.start",
        workflow=workflow.name,
        agent_count=len(workflow.agents),
        branch=ctx.branch,
        target_repo=str(ctx.target_repo_path),
        stub_mode=ctx.stub_mode,
        client=ctx.client_name,
    )
    _persist_state(workflow, ctx, status="running")

    try:
        if workflow.pre_run:
            _run_phase_script(workflow.pre_run, "pre_run", None, ctx)
        _walk_agents(workflow, ctx, start_index=0)
        if auto_fix_ci:
            _maybe_run_ci_fix_loop(workflow, ctx, max_fix_attempts)
        if workflow.post_run:
            _run_phase_script(workflow.post_run, "post_run", None, ctx)
    except RunPaused:
        # state.json already marks the run paused. swallow + return so the
        # CLI exits 0 with a 'paused' status; resume picks up from here.
        return ctx
    except AgentFailure as e:
        ctx.events.emit(
            "run.complete",
            status="failed",
            elapsed_seconds=round(time.monotonic() - start_t, 3),
            failed_agent=e.failed_agent,
        )
        _persist_state(workflow, ctx, status="failed")
        raise
    except ScriptFailure as e:
        ctx.events.emit(
            "run.complete",
            status="failed",
            elapsed_seconds=round(time.monotonic() - start_t, 3),
            failed_agent=None,
            script_failure=e.result.cmd,
        )
        _persist_state(workflow, ctx, status="failed")
        raise
    except Exception:
        ctx.events.emit(
            "run.complete",
            status="failed",
            elapsed_seconds=round(time.monotonic() - start_t, 3),
            failed_agent=None,
        )
        _persist_state(workflow, ctx, status="failed")
        raise

    ctx.events.emit(
        "run.complete",
        status="success",
        elapsed_seconds=round(time.monotonic() - start_t, 3),
        failed_agent=None,
    )
    _persist_state(workflow, ctx, status="succeeded",
                   current_agent_index=len(workflow.agents))
    logger.info("run %s complete", ctx.run_id)
    return ctx


# ---------------------------------------------------------------------------
# Resume / abort
# ---------------------------------------------------------------------------


def resume_run(
    run_dir: Path,
    workflow: Workflow,
    *,
    client_config: ClientConfig | None = None,
) -> RunContext:
    """Resume a paused run from the next agent.

    Reads state.json, reconstructs a RunContext pointing at the same
    working dir, and walks agents from current_agent_index.
    """
    state = RunState.load(run_dir)
    if state.status != "paused":
        raise RuntimeError(
            f"cannot resume run {state.run_id!r} with status={state.status!r}; "
            "expected 'paused'"
        )
    ctx = RunContext(
        run_id=state.run_id,
        workflow_name=state.workflow_name,
        target_repo_path=Path(state.target_repo_path),
        working_dir=run_dir,
        inputs=state.inputs,
        stub_mode=state.stub_mode,
        branch=state.branch,
        base_branch=state.base_branch,
        client_name=state.client,
    )
    ctx.workflow_mcp_servers = list(workflow.mcp_servers)
    if client_config is not None:
        ctx.client_name = client_config.name
        ctx.client_prefix = client_config.as_system_prefix()
    ctx.events = EventEmitter(run_dir / "events.jsonl")
    ctx.events.emit("run.resume", from_agent_index=state.current_agent_index)

    state.status = "running"
    state.save(run_dir)

    start_t = time.monotonic()
    try:
        _walk_agents(workflow, ctx, start_index=state.current_agent_index)
        if workflow.post_run:
            _run_phase_script(workflow.post_run, "post_run", None, ctx)
    except RunPaused:
        return ctx
    except AgentFailure as e:
        ctx.events.emit("run.complete", status="failed",
                        elapsed_seconds=round(time.monotonic() - start_t, 3),
                        failed_agent=e.failed_agent)
        _persist_state(workflow, ctx, status="failed")
        raise
    except ScriptFailure:
        _persist_state(workflow, ctx, status="failed")
        raise
    ctx.events.emit("run.complete", status="success",
                    elapsed_seconds=round(time.monotonic() - start_t, 3),
                    failed_agent=None)
    _persist_state(workflow, ctx, status="succeeded",
                   current_agent_index=len(workflow.agents))
    return ctx


def abort_run(run_dir: Path) -> RunState:
    """Mark a run aborted. Idempotent for terminal states.

    Appends a run.complete{status:aborted} event so any live UI tailing
    events.jsonl sees the transition.
    """
    state = RunState.load(run_dir)
    if state.status in ("succeeded", "failed", "aborted"):
        return state
    state.status = "aborted"
    state.save(run_dir)
    events = EventEmitter(run_dir / "events.jsonl")
    events.emit("run.complete", status="aborted", failed_agent=None)
    return state


def fork_run(
    source_run_dir: Path,
    *,
    step: int,
    target_repo_path: Path,
    task: str | None = None,
    extra_inputs: dict[str, str] | None = None,
    client_config: ClientConfig | None = None,
) -> RunContext:
    """Fork a run: copy its state + the outputs of agents [0, step) into a
    fresh run dir, then resume the new run from agent `step`.

    `step` is a 0-based agent index — the first agent the forked run
    re-runs. The outputs of every agent before it are carried over so the
    resumed pipeline has the inputs it expects; everything from `step`
    onward is produced anew. `task` / `extra_inputs` override the source
    run's inputs (e.g. to retry a step with a tweaked task description).

    Cost aggregates start fresh — the forked run accounts only for the
    agents it actually re-runs. Returns the resumed RunContext.
    """
    source_state = RunState.load(source_run_dir)
    workflow = Workflow.find(source_state.workflow_name, target_repo_path)
    n_agents = len(workflow.agents)
    if not 0 <= step <= n_agents:
        raise ValueError(
            f"fork step {step} out of range; workflow '{workflow.name}' has "
            f"{n_agents} agents (valid: 0..{n_agents})"
        )

    # the outputs of every agent before the fork point must exist in the
    # source, or the resumed pipeline will halt on missing inputs.
    carried: list[str] = []
    for spec in workflow.agents[:step]:
        carried.extend(spec.outputs)
    missing = [o for o in carried if not (source_run_dir / o).exists()]
    if missing:
        raise ValueError(
            f"cannot fork {source_state.run_id} at step {step}: source is "
            f"missing carried-over outputs {missing} — it did not progress "
            f"that far."
        )

    new_inputs = dict(source_state.inputs)
    if task is not None:
        new_inputs["task"] = task
    if extra_inputs:
        new_inputs.update(extra_inputs)

    ctx = RunContext.create(
        workflow_name=source_state.workflow_name,
        target_repo_path=target_repo_path,
        inputs=new_inputs,
        stub_mode=source_state.stub_mode,
    )

    for name in carried:
        shutil.copy2(source_run_dir / name, ctx.working_dir / name)

    forked_state = RunState(
        run_id=ctx.run_id,
        workflow_name=source_state.workflow_name,
        target_repo_path=str(target_repo_path),
        status="paused",
        current_agent_index=step,
        branch=source_state.branch,
        base_branch=source_state.base_branch,
        client=source_state.client,
        inputs=new_inputs,
        stub_mode=source_state.stub_mode,
        completed_agents=[a.id for a in workflow.agents[:step]],
        forked_from=source_state.run_id,
        fork_step=step,
    )
    forked_state.save(ctx.working_dir)

    # seed events.jsonl so `agentic watch` and helm's run sync treat the
    # fork as a first-class run rather than a stray dir.
    EventEmitter(ctx.working_dir / "events.jsonl").emit(
        "run.fork",
        workflow=workflow.name,
        agent_count=n_agents,
        branch=source_state.branch,
        stub_mode=source_state.stub_mode,
        forked_from=source_state.run_id,
        fork_step=step,
    )

    logger.info("fork %s -> %s at step %d", source_state.run_id, ctx.run_id, step)
    return resume_run(ctx.working_dir, workflow, client_config=client_config)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def _walk_agents(workflow: Workflow, ctx: RunContext, *, start_index: int) -> None:
    for index, spec in enumerate(workflow.agents):
        if index < start_index:
            continue
        _persist_state(workflow, ctx, status="running", current_agent_index=index)

        if spec.pre:
            _run_phase_script(spec.pre, "pre", index, ctx, agent_id=spec.id)

        _run_one(spec, ctx)
        _record_agent_cost(ctx)

        if spec.post:
            _run_phase_script(spec.post, "post", index, ctx, agent_id=spec.id)

        if spec.pause_after and index < len(workflow.agents) - 1:
            next_agent = workflow.agents[index + 1].id
            ctx.events.emit(
                "agent.pause",
                agent=spec.id,
                agent_id=spec.id,
                next_agent=next_agent,
                reason="pause_after",
            )
            _persist_state(workflow, ctx, status="paused",
                           current_agent_index=index + 1)
            raise RunPaused(run_id=ctx.run_id, working_dir=ctx.working_dir,
                            next_agent=next_agent)


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


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


def _run_phase_script(
    cmd: str,
    phase: str,
    agent_index: int | None,
    ctx: RunContext,
    *,
    agent_id: str | None = None,
) -> None:
    ctx.events.emit(
        "script.start",
        phase=phase,
        cmd=cmd,
        agent=agent_id,
        agent_index=agent_index,
    )
    result = run_script(
        cmd,
        phase=phase,  # type: ignore[arg-type]
        cwd=ctx.target_repo_path,
        log_dir=ctx.working_dir / "scripts",
        agent_index=agent_index,
    )
    ctx.events.emit(
        "script.complete",
        phase=phase,
        cmd=cmd,
        agent=agent_id,
        agent_index=agent_index,
        exit_code=result.exit_code,
        stdout_tail=result.stdout_tail(),
        stderr_tail=result.stderr_tail(),
    )
    if not result.ok():
        raise ScriptFailure(result)


# ---------------------------------------------------------------------------
# CI-failure loop
# ---------------------------------------------------------------------------


def _maybe_run_ci_fix_loop(
    workflow: Workflow, ctx: RunContext, max_attempts: int
) -> None:
    """If the workflow has a `pr` agent and it produced a PR number, poll
    `gh pr checks` and re-invoke a `fix` agent on failure.
    """
    from agentic import ci_loop  # lazy to keep import graph light

    pr_number = _find_pr_number_from_outputs(workflow, ctx)
    if pr_number is None:
        logger.info("run %s :: auto-fix-ci enabled but no PR number found; skipping",
                    ctx.run_id)
        return

    def fix_callback(failure_output: str, attempt: int) -> bool:
        fix_spec = AgentSpec(
            id=f"ci_fix_{attempt}",
            prompt_file="prompts/ci_fix.md",  # bundled in scaffold
            inputs=["task"],
            outputs=[f"CI_FIX_{attempt}.md"],
            allowed_tools=["Read", "Write", "Edit", "Bash"],
        )
        ctx.inputs["task"] = (
            f"attempt {attempt} of {max_attempts}\n\n"
            f"CI on PR #{pr_number} failed. Fix the failure and push.\n\n"
            f"--- failure output ---\n{failure_output[:4000]}"
        )
        # overwrite FIX_NOTES.md each attempt so the next iteration reads fresh
        ctx.events.emit("ci.fix.start", attempt=attempt, pr_number=pr_number)
        try:
            run_agent(fix_spec, ctx)
        except Exception as e:
            ctx.events.emit("ci.fix.fail", attempt=attempt, error=str(e))
            return False
        ctx.events.emit("ci.fix.complete", attempt=attempt)
        return True

    ci_loop.watch_and_fix(
        pr_number=pr_number,
        events=ctx.events,
        fix=fix_callback,
        max_attempts=max_attempts,
    )


def _find_pr_number_from_outputs(workflow: Workflow, ctx: RunContext) -> int | None:
    """Scan PR_BODY.md (or any output starting with PR_) for a #NNN reference
    or a PR URL.
    """
    import re
    for spec in workflow.agents:
        for out in spec.outputs:
            if not out.startswith("PR_") and out != "PR.md":
                continue
            p = ctx.working_dir / out
            if not p.exists():
                continue
            blob = p.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"https?://\S+/pull/(\d+)", blob)
            if m:
                return int(m.group(1))
            m = re.search(r"#(\d{1,7})\b", blob)
            if m:
                return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Branch + state helpers
# ---------------------------------------------------------------------------


def _log_auth_method() -> None:
    """Report which auth path is active. Fails fast if neither is configured."""
    method = detect_auth()  # raises NoAuthConfigured before any agent runs
    if method is AuthMethod.API_KEY:
        logger.warning("auth: ANTHROPIC_API_KEY (billing: API account)")
    else:
        logger.info("auth: claude CLI login (billing: Max/Pro plan)")


def _maybe_prepare_branch(ctx: RunContext) -> None:
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
    ctx.base_branch = base if base != "HEAD" else None

    branch = f"agentic/{ctx.workflow_name}-{ctx.short_id}"
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=ctx.target_repo_path, check=True,
        capture_output=True, text=True,
    )
    ctx.branch = branch
    logger.info("run %s :: created branch %s", ctx.run_id, branch)


def _record_agent_cost(ctx: RunContext) -> None:
    """Fold the just-finished agent's cost into the persisted RunState.

    agent.py stashes the cost on `ctx.last_agent_cost`; we load state.json,
    aggregate, save, and clear the hand-off. Best-effort — a run with no
    state.json yet (some unit-test paths) is silently skipped, and the cost
    aggregates are non-critical to the run completing.
    """
    cost = ctx.last_agent_cost
    if not cost:
        return
    if not RunState.path_for(ctx.working_dir).exists():
        return
    try:
        state = RunState.load(ctx.working_dir)
        state.add_cost(
            agent=str(cost["agent"]),
            input_tokens=int(cost["input_tokens"]),
            output_tokens=int(cost["output_tokens"]),
            cost_usd=float(cost["cost_usd"]),
        )
        state.save(ctx.working_dir)
    except Exception as e:  # cost accounting must never halt a run
        logger.warning("run %s :: failed to record agent cost: %s", ctx.run_id, e)
    finally:
        ctx.last_agent_cost = None


def _persist_state(
    workflow: Workflow,
    ctx: RunContext,
    *,
    status: str,
    current_agent_index: int | None = None,
) -> None:
    """Write state.json reflecting the current point in the run."""
    state_path = ctx.working_dir / "state.json"
    if state_path.exists():
        state = RunState.load(ctx.working_dir)
    else:
        state = RunState(
            run_id=ctx.run_id,
            workflow_name=workflow.name,
            target_repo_path=str(ctx.target_repo_path),
            inputs=dict(ctx.inputs),
            stub_mode=ctx.stub_mode,
        )
    if current_agent_index is not None:
        state.current_agent_index = current_agent_index
    state.status = status  # type: ignore[assignment]
    state.branch = ctx.branch
    state.base_branch = ctx.base_branch
    state.client = ctx.client_name
    state.save(ctx.working_dir)
