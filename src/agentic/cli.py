from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agentic.auth import NoAuthConfigured
from agentic.client_config import load_client
from agentic.context import RunContext
from agentic.logging import setup_run_logging, teardown_run_logging
from agentic.runner import (
    AgentFailure,
    DirtyWorkingTree,
    RunPaused,
    abort_run,
    resume_run,
    run_workflow,
)
from agentic.state import RunState
from agentic.workflow import Workflow, list_workflows

console = Console()


def _target_repo() -> Path:
    return Path.cwd()


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

_MAIN_HELP = """\
Agentic — orchestrate multi-agent Claude Agent SDK workflows.

Each `agentic run` drives a workflow (an ordered list of agents defined in
YAML) against your repo. Agents hand off via files in a per-run working
directory. The default `feature` workflow takes a task description through
spec → explore → implement → test → review → PR.

\b
QUICK START
  agentic init                            # in your repo, scaffold .agentic/
  git add .agentic && git commit -m "add agentic workflows"
  agentic run feature --task "..."        # creates a branch, runs 6 agents
  agentic watch                           # observe the most recent run

\b
TYPICAL WORKFLOW
  1. Start on the branch you want PRs to target (usually main):
       git checkout main
  2. Ensure the tree is clean (agentic refuses dirty trees):
       git status
  3. Drive a workflow:
       agentic run feature --task "..."
     → creates agentic/feature-<short-id> from HEAD
     → 6 agents run; outputs go to .agentic/runs/<run-id>/
     → final agent pushes the branch and opens a PR via `gh`
  4. After success, switch back and start the next task:
       git checkout main
       agentic run feature --task "..."

\b
FILES & DIRECTORIES
  .agentic/workflows/<name>.yaml     workflow definitions
  .agentic/prompts/<id>.md           per-agent prompts (edit to taste)
  .agentic/runs/<run-id>/            per-run working dir (gitignored)
    ├── events.jsonl                 structured event stream (feeds `watch`)
    ├── run.log                      human-readable log
    └── <agent outputs>              SPEC.md, CONTEXT.md, CHANGES.md, ...

\b
AUTH (only for non-stub runs)
  Preferred   : claude login                  bills your Max/Pro plan
  Alternative : export ANTHROPIC_API_KEY=...  bills API account (logs WARN)
  Refused if neither is configured.

\b
TIPS
  • Try a workflow with `--stub` first to confirm wiring (no SDK calls).
  • If a run fails, the branch and run dir are left intact for inspection.
    Use `agentic logs <id>` or `agentic watch <id>` to debug.
  • `--task` and `--issue` are mutually exclusive on `agentic run`.
  • `--issue N` pulls the task description from GitHub issue N (needs `gh`).
  • Each run creates its own branch — you never manage branches yourself.

Per-command help:  agentic <command> -h
"""


@click.group(context_settings=CONTEXT_SETTINGS, help=_MAIN_HELP)
@click.version_option(package_name="agentic")
def main() -> None:
    pass


_RUN_HELP = """\
Run a workflow from .agentic/workflows/<name>.yaml.

\b
Examples:
  agentic run feature --task "add a --dry-run flag to deploy"
  agentic run feature --issue 142
  agentic run feature --task "..." --stub        (no SDK calls; for testing wiring)
  agentic run feature --task "..." --input k=v   (extra inputs to first agent)

\b
What happens:
  1. Refuses if working tree is dirty.
  2. Creates branch agentic/<workflow>-<short-id> from current HEAD.
  3. Logs auth method (WARN if ANTHROPIC_API_KEY is set, INFO for claude login).
  4. Walks each agent in order; outputs land in .agentic/runs/<run-id>/.
  5. On failure: halts, leaves branch + run dir intact for inspection.

`--task` and `--issue` are mutually exclusive.
"""


@main.command("run", context_settings=CONTEXT_SETTINGS, help=_RUN_HELP)
@click.argument("workflow_name")
@click.option("--task", default=None, help="Task description passed as 'task' input to the first agent.")
@click.option("--issue", type=int, default=None,
              help="GitHub issue number; resolved via `gh issue view` and passed as 'task'.")
@click.option("--input", "kv_inputs", multiple=True,
              help="Extra inputs as key=value. Repeatable.")
@click.option("--stub", is_flag=True,
              help="Run agents in stub mode (no SDK calls). Useful for testing wiring.")
@click.option("--client", "client_name", default=None,
              help="Name of a per-client config (see .agentic/clients/<name>.yaml). "
                   "Its conventions get prepended to every agent prompt.")
@click.option("--auto-fix-ci", is_flag=True,
              help="After the `pr` agent opens the PR, poll `gh pr checks` "
                   "and re-invoke a fix agent on failure.")
@click.option("--max-fix-attempts", default=3, type=int,
              help="Cap on the CI-failure fix loop. Default 3.")
def run_cmd(
    workflow_name: str,
    task: str | None,
    issue: int | None,
    kv_inputs: tuple[str, ...],
    stub: bool,
    client_name: str | None,
    auto_fix_ci: bool,
    max_fix_attempts: int,
) -> None:
    target = _target_repo()

    if task is not None and issue is not None:
        console.print("[red]error:[/red] --task and --issue are mutually exclusive")
        sys.exit(2)

    if issue is not None:
        try:
            task = _fetch_issue(issue, target)
        except RuntimeError as e:
            console.print(f"[red]error:[/red] {e}")
            sys.exit(2)

    try:
        workflow = Workflow.find(workflow_name, target)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/red] {e}")
        sys.exit(2)

    inputs: dict[str, str] = {}
    if task is not None:
        inputs["task"] = task
    for kv in kv_inputs:
        if "=" not in kv:
            console.print(f"[red]error:[/red] --input '{kv}' must be key=value")
            sys.exit(2)
        k, v = kv.split("=", 1)
        inputs[k] = v

    ctx = RunContext.create(
        workflow_name=workflow.name,
        target_repo_path=target,
        inputs=inputs,
        stub_mode=stub,
    )
    log_path = setup_run_logging(ctx)

    client_cfg = None
    if client_name:
        try:
            client_cfg = load_client(
                client_name,
                search_roots=[target, Path(__file__).parent / "scaffold"],
            )
        except FileNotFoundError as e:
            console.print(f"[red]error:[/red] {e}")
            sys.exit(2)
        console.print(f"  client: [bold]{client_cfg.name}[/bold]")

    mode_tag = "[yellow](stub)[/yellow]" if stub else "[green](real SDK)[/green]"
    console.print(f"[cyan]▶[/cyan] run [bold]{ctx.run_id}[/bold] :: workflow=[bold]{workflow.name}[/bold] {mode_tag}")
    console.print(f"  working dir: {ctx.working_dir}")
    console.print(f"  log: {log_path}")
    short = ctx.run_id.rsplit("-", 1)[-1]
    console.print(f"  [dim]watch this run: agentic watch {short}[/dim]")

    try:
        run_workflow(
            workflow, ctx,
            client_config=client_cfg,
            auto_fix_ci=auto_fix_ci,
            max_fix_attempts=max_fix_attempts,
        )
    except DirtyWorkingTree as e:
        console.print(f"[red]✗ refused to run:[/red] {e}")
        sys.exit(1)
    except NoAuthConfigured as e:
        console.print(f"[red]✗ refused to run:[/red] {e}")
        sys.exit(1)
    except AgentFailure as e:
        console.print(f"[red]✗ run halted:[/red] {e}")
        console.print(f"  inspect: {ctx.working_dir}")
        if ctx.branch:
            console.print(f"  branch:  {ctx.branch}")
        sys.exit(1)
    finally:
        teardown_run_logging(ctx)

    # If the run paused at a pause_after agent, surface that instead of "complete".
    state_path = ctx.working_dir / "state.json"
    if state_path.exists():
        try:
            st = RunState.load(ctx.working_dir)
            if st.status == "paused":
                console.print(
                    f"[yellow]⏸ run paused[/yellow] :: {ctx.run_id} — "
                    f"resume with [cyan]agentic resume {short}[/cyan]"
                )
                return
        except Exception:  # pragma: no cover - defensive
            pass

    if ctx.branch:
        console.print(f"[green]✓ run complete[/green] :: {ctx.run_id} on branch [bold]{ctx.branch}[/bold]")
    else:
        console.print(f"[green]✓ run complete[/green] :: {ctx.run_id}")

    if ctx.base_branch:
        console.print(
            f"  [dim]next task:[/dim] [bold]git checkout {ctx.base_branch} && "
            f"agentic run {workflow.name} --task \"...\"[/bold]"
        )


def _fetch_issue(number: int, target: Path) -> str:
    if shutil.which("gh") is None:
        raise RuntimeError("`gh` (GitHub CLI) is not installed; cannot resolve --issue")
    proc = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "title,body"],
        cwd=target, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue view {number} failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    return f"{data.get('title', '').strip()}\n\n{data.get('body', '').strip()}".strip()


@main.command("list", context_settings=CONTEXT_SETTINGS)
def list_cmd() -> None:
    """List workflows defined in .agentic/workflows/ in the current repo."""
    target = _target_repo()
    names = list_workflows(target)
    if not names:
        console.print("[yellow]no workflows found in .agentic/workflows/[/yellow]")
        console.print("hint: run [bold]agentic init[/bold] to scaffold one.")
        return

    table = Table(title="workflows")
    table.add_column("name")
    table.add_column("description")
    table.add_column("agents")
    for name in names:
        try:
            wf = Workflow.find(name, target)
            table.add_row(name, wf.description, str(len(wf.agents)))
        except Exception as e:  # pragma: no cover - defensive
            table.add_row(name, f"[red]load error: {e}[/red]", "-")
    console.print(table)


_SCHEMA_HELP = """\
Print the JSON Schema for a config-file kind, to stdout.

\b
Examples:
  agentic schema --workflow         # schema for .agentic/workflows/<name>.yaml
  agentic schema --client-config    # schema for .agentic/clients/<name>.yaml
  agentic schema                    # defaults to --workflow

The schema is derived directly from the Pydantic models the runner uses
(`Workflow`, `ClientConfig`), so it always matches what `agentic run`
will accept. Tooling — e.g. helm's Monaco YAML editors — consumes this to
drive validation and autocomplete.
"""


@main.command("schema", context_settings=CONTEXT_SETTINGS, help=_SCHEMA_HELP)
@click.option("--workflow", "kind", flag_value="workflow",
              help="JSON schema for a workflow YAML file (the default).")
@click.option("--client-config", "kind", flag_value="client-config",
              help="JSON schema for a client-config YAML file.")
def schema_cmd(kind: str | None) -> None:
    from agentic.client_config import ClientConfig

    model = ClientConfig if kind == "client-config" else Workflow
    click.echo(json.dumps(model.model_json_schema(), indent=2))


_WATCH_HELP = """\
Open a TUI to watch a run (live for in-progress, static for complete).

\b
Examples:
  agentic watch                     # most recent run in this repo
  agentic watch a3009f81            # specific run; 8-char short prefix is fine
  agentic watch --list              # table of recent runs (no TUI)

\b
What it shows:
  Left pane  — agents in workflow order with status icons (►/✓/✗) + elapsed time.
  Right pane — chronological transcript for the selected agent (assistant text,
               tool calls, tool results).

\b
Keys inside the TUI:
  ↑/↓   select agent       r   force refresh
  q     quit               Ctrl+C   quit
"""


@main.command("watch", context_settings=CONTEXT_SETTINGS, help=_WATCH_HELP)
@click.argument("run_id", required=False)
@click.option("--list", "list_only", is_flag=True,
              help="Print recent runs instead of opening the TUI.")
def watch_cmd(run_id: str | None, list_only: bool) -> None:
    target = _target_repo()
    runs_dir = target / ".agentic" / "runs"
    if not runs_dir.exists():
        console.print(f"[red]error:[/red] no runs in {runs_dir}")
        sys.exit(2)

    if list_only:
        _print_runs_table(runs_dir)
        return

    chosen = _resolve_run(runs_dir, run_id)
    if chosen is None:
        if run_id:
            console.print(f"[red]error:[/red] no run matching '{run_id}' in {runs_dir}")
        else:
            console.print(f"[red]error:[/red] no runs found in {runs_dir}")
        sys.exit(2)

    events_path = chosen / "events.jsonl"
    if not events_path.exists():
        console.print(f"[red]error:[/red] no events.jsonl in {chosen} "
                      "(run was refused before any events were emitted?)")
        sys.exit(2)

    # lazy-import textual so `agentic list`/`run`/`logs` stay snappy
    from agentic.watch import run_watch
    run_watch(events_path, run_id=chosen.name)


def _resolve_run(runs_dir: Path, run_id: str | None) -> Path | None:
    candidates = sorted([p for p in runs_dir.iterdir() if p.is_dir()],
                        key=lambda p: p.name, reverse=True)
    if not candidates:
        return None
    if run_id is None:
        return candidates[0]
    for p in candidates:
        if p.name == run_id or p.name.endswith("-" + run_id) or p.name.startswith(run_id):
            return p
    return None


def _print_runs_table(runs_dir: Path) -> None:
    import json as _json
    table = Table(title="recent runs")
    for col in ("run id", "workflow", "branch", "status", "started", "duration"):
        table.add_column(col)

    rows: list[tuple[str, ...]] = []
    for d in sorted(runs_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir():
            continue
        ev_path = d / "events.jsonl"
        workflow = branch = status = started = duration = "—"
        if ev_path.exists():
            try:
                with ev_path.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        ev = _json.loads(line)
                        if ev["type"] == "run.start":
                            workflow = ev["payload"].get("workflow", "—")
                            branch = ev["payload"].get("branch") or "—"
                            started = ev["ts"]
                            status = "running"
                        elif ev["type"] == "run.complete":
                            status = ev["payload"].get("status", "—")
                            dur = ev["payload"].get("elapsed_seconds")
                            duration = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "—"
            except (OSError, _json.JSONDecodeError):
                pass
        rows.append((d.name, workflow, branch, status, started, duration))

    for r in rows:
        table.add_row(*r)
    if not rows:
        console.print(f"[yellow]no runs in {runs_dir}[/yellow]")
        return
    console.print(table)


@main.command("resume", context_settings=CONTEXT_SETTINGS)
@click.argument("run_id")
@click.option("--client", "client_name", default=None,
              help="Re-apply a client config when resuming (defaults to the original).")
def resume_cmd(run_id: str, client_name: str | None) -> None:
    """Resume a paused run from where it stopped.

    Looks up `.agentic/runs/<run-id>/` (8-char short prefix accepted),
    reads state.json, and continues with the next agent.
    """
    target = _target_repo()
    runs_dir = target / ".agentic" / "runs"
    chosen = _resolve_run(runs_dir, run_id)
    if chosen is None:
        console.print(f"[red]error:[/red] no run matching '{run_id}' in {runs_dir}")
        sys.exit(2)
    try:
        state = RunState.load(chosen)
    except FileNotFoundError:
        console.print(f"[red]error:[/red] no state.json in {chosen}")
        sys.exit(2)
    try:
        workflow = Workflow.find(state.workflow_name, target)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/red] {e}")
        sys.exit(2)

    client_cfg = None
    effective_client = client_name or state.client
    if effective_client:
        try:
            client_cfg = load_client(
                effective_client,
                search_roots=[target, Path(__file__).parent / "scaffold"],
            )
        except FileNotFoundError as e:
            console.print(f"[red]error:[/red] {e}")
            sys.exit(2)

    console.print(f"[cyan]▶[/cyan] resume [bold]{state.run_id}[/bold] "
                  f"from agent index {state.current_agent_index}")
    try:
        resume_run(chosen, workflow, client_config=client_cfg)
    except AgentFailure as e:
        console.print(f"[red]✗ run halted:[/red] {e}")
        sys.exit(1)

    final = RunState.load(chosen)
    if final.status == "paused":
        console.print(f"[yellow]⏸ paused again[/yellow] :: {state.run_id}")
    else:
        console.print(f"[green]✓ run complete[/green] :: {state.run_id}")


@main.command("abort", context_settings=CONTEXT_SETTINGS)
@click.argument("run_id")
def abort_cmd(run_id: str) -> None:
    """Mark a paused or running run as aborted. Idempotent for terminal runs."""
    target = _target_repo()
    runs_dir = target / ".agentic" / "runs"
    chosen = _resolve_run(runs_dir, run_id)
    if chosen is None:
        console.print(f"[red]error:[/red] no run matching '{run_id}' in {runs_dir}")
        sys.exit(2)
    state = abort_run(chosen)
    console.print(f"[yellow]✗ aborted[/yellow] :: {state.run_id}")


@main.command("logs", context_settings=CONTEXT_SETTINGS)
@click.argument("run_id")
def logs_cmd(run_id: str) -> None:
    """Print .agentic/runs/<run-id>/run.log for a past run.

    For a richer view of a run, prefer `agentic watch <run-id>`.
    """
    target = _target_repo()
    log_path = target / ".agentic" / "runs" / run_id / "run.log"
    if not log_path.exists():
        console.print(f"[red]error:[/red] no log at {log_path}")
        sys.exit(2)
    console.print(log_path.read_text(), highlight=False)


@main.command("init", context_settings=CONTEXT_SETTINGS)
def init_cmd() -> None:
    """Scaffold .agentic/ in the current directory.

    \b
    Creates:
      .agentic/workflows/feature.yaml    6-agent pipeline: spec→explore→implement→test→review→pr
      .agentic/workflows/bugfix.yaml     7-agent pipeline: adds repro (failing test) + fix
      .agentic/workflows/docs.yaml       5-agent pipeline: spec→explore→docs→review→pr
      .agentic/prompts/*.md              prompts for each agent (edit to taste)
      .agentic/.gitignore                ignores runs/ artifacts

    Existing files are not overwritten. Prints a numbered next-steps block.
    """
    target = _target_repo()
    workflows_dir = target / ".agentic" / "workflows"
    prompts_dir = target / ".agentic" / "prompts"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)

    gitignore = target / ".agentic" / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("runs/\n")

    src_root = Path(__file__).parent / "scaffold"
    clients_dir = target / ".agentic" / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    for src_file in (src_root / "workflows").glob("*.yaml"):
        dst = workflows_dir / src_file.name
        if not dst.exists():
            shutil.copy(src_file, dst)
            created.append(dst)
    for src_file in (src_root / "prompts").glob("*.md"):
        dst = prompts_dir / src_file.name
        if not dst.exists():
            shutil.copy(src_file, dst)
            created.append(dst)
    src_clients = src_root / "clients"
    if src_clients.exists():
        for src_file in src_clients.glob("*.yaml"):
            dst = clients_dir / src_file.name
            if not dst.exists():
                shutil.copy(src_file, dst)
                created.append(dst)

    console.print(f"[green]✓[/green] initialized .agentic/ in {target}")
    for p in created:
        console.print(f"  created {p.relative_to(target)}")
    if not created:
        console.print("  (everything already in place)")

    _print_next_steps(target)


def _print_next_steps(target: Path) -> None:
    """Tell the developer what to do after `agentic init`."""
    is_git = (target / ".git").exists()
    console.print()
    console.print("[bold]next steps:[/bold]")
    step = 1
    if is_git:
        console.print(f"  [bold]{step}.[/bold] commit the scaffold:")
        console.print('       [cyan]git add .agentic && git commit -m "add agentic workflows"[/cyan]')
        step += 1
        console.print(f"  [bold]{step}.[/bold] push so the PR base contains the scaffold:")
        console.print("       [cyan]git push[/cyan]")
        step += 1
        console.print(f"  [bold]{step}.[/bold] from the branch you want PRs to target (usually main):")
        console.print("       [cyan]git checkout main[/cyan]")
        step += 1
    else:
        console.print(f"  [bold]{step}.[/bold] [yellow]this isn't a git repo yet[/yellow] — initialise one first:")
        console.print('       [cyan]git init && git add . && git commit -m "init"[/cyan]')
        step += 1
    console.print(f"  [bold]{step}.[/bold] drive a workflow:")
    console.print('       [cyan]agentic run feature --task "..."[/cyan]')
    console.print(f"       [dim](creates an agentic/feature-<id> branch from HEAD, then 6 agents run)[/dim]")
    step += 1
    console.print(f"  [bold]{step}.[/bold] watch live in another terminal:")
    console.print("       [cyan]agentic watch[/cyan]")
    console.print()
    console.print("  [dim]each run creates its own branch — you don't manage branches yourself.[/dim]")
    console.print("  [dim]working tree must be clean when you run; agentic refuses dirty trees.[/dim]")


if __name__ == "__main__":  # pragma: no cover
    main()
