# agentic

Orchestrator for **Claude Agent SDK** workflows. A workflow is an ordered list
of *agents* (each a `claude_agent_sdk.query()` invocation) that hand off to one
another via files in a run-scoped working directory.

Four workflows ship out of the box — `feature`, `bugfix`, `docs`, `designer` — and you
can author your own (see [Authoring a workflow](#authoring-a-workflow)).

## Install

Pick one. Both install the `agentic` CLI and `claude-agent-sdk` as a dependency.

### Option A — Global install (recommended: `agentic` available everywhere)

Use [`pipx`](https://pipx.pypa.io/) to install into an isolated environment and expose `agentic` on your `PATH`. After this, `agentic` works in any directory without activating a venv.

```bash
# Install pipx if you don't have it:
# macOS:   brew install pipx && pipx ensurepath
# Debian:  sudo apt install pipx && pipx ensurepath
# Other:   python -m pip install --user pipx && python -m pipx ensurepath

pipx install -e .          # from the agentic-layer repo root
```

Verify:

```bash
which agentic              # should resolve outside any venv
agentic --help
```

To upgrade after pulling new changes, re-run `pipx install -e .` (the `-e` flag means subsequent `git pull`s in the repo are picked up automatically — no reinstall needed).

### Option B — Project-local venv

Keeps `agentic` scoped to a single virtualenv. Use this if you'd rather not install globally.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

`agentic` is only on `PATH` while the venv is activated.

## Setup

### 1. Authenticate with Claude

**Recommended — `claude login` (uses your Max/Pro plan):**

```bash
claude login    # opens a browser; bills runs to your subscription
```

This is the default path. `agentic` will pick up the credentials stored by
`claude login` under `~/.claude/.credentials.json` and route all SDK calls
through your plan — no API credits consumed.

**Alternative — `ANTHROPIC_API_KEY` (for CI / headless contexts):**

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # bills your API account
```

Set this only when you can't do an interactive `claude login` (CI runners,
build servers, etc.). When the env var is set, `agentic` logs a `WARN` line
at run start so you don't accidentally burn API credits on a machine where
you meant to use your Max plan.

`agentic run` logs which auth method is active before any agent fires —
look for a line like `auth: claude CLI login (billing: Max/Pro plan)` or
`auth: ANTHROPIC_API_KEY (billing: API account)`. If neither is configured,
the run is refused before any tokens are spent.

Other auth providers (Bedrock, Vertex, Azure) are supported by the SDK —
set the appropriate `CLAUDE_CODE_USE_*` environment variable. See the
[SDK docs](https://code.claude.com/docs/en/agent-sdk/overview).

### 2. GitHub CLI (needed for the `pr` agent and `--issue` flag)

```bash
# macOS:   brew install gh
# Debian:  sudo apt install gh
gh auth login
```

The `pr` agent uses `gh pr create` to open the pull request. The `--issue`
flag uses `gh issue view` to pull the task description from a GitHub issue.

## Walkthrough

```bash
# In a git repo where you want to use agentic:
agentic init                              # scaffold .agentic/

git add .agentic && git commit -m "scaffold agentic"

# Run the feature workflow against a task.
agentic run feature --task "add a --dry-run flag to the deploy command"

# Or feed it a GitHub issue:
agentic run feature --issue 142

# List workflows / inspect a past run.
agentic list
agentic logs <run-id>
```

### What happens when you run

1. Runner checks the working tree is clean. **Refuses to run** if dirty.
2. Creates a branch `agentic/feature-<short-run-id>` from your current HEAD.
3. Walks the six agents in order. Each agent's outputs are written to
   `.agentic/runs/<run-id>/`, and the next agent reads them.
4. The `pr` agent pushes the branch and opens a PR via `gh`.

If any agent fails, the runner halts. The branch and run directory are
left intact for inspection: `agentic logs <run-id>`.

### Stub mode (`--stub`)

Skips all SDK calls; each agent just writes a marker file to its declared
outputs. The branch is still created. Use this to verify the wiring without
spending tokens:

```bash
agentic run feature --task "anything" --stub
```

This is the mode the integration tests run in.

## How workflows work

A workflow is a YAML file in `.agentic/workflows/` describing an ordered list
of agents. Each agent has:

- a **prompt file** at `.agentic/prompts/<id>.md` (the system prompt for that
  agent's SDK call),
- **inputs** — names of upstream artifacts it reads,
- **outputs** — names of files it MUST write to the run's working directory,
- **allowed_tools** — the subset of SDK tools the agent may invoke
  (`Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`).

When you run a workflow, the runner:

1. Creates a per-run working directory at `.agentic/runs/<run-id>/`.
2. For each agent, in order:
   - Renders the prompt by substituting `{{TAG}}` placeholders for inputs and
     outputs (see [Input semantics](#input-semantics)).
   - Invokes the Claude Agent SDK with that prompt and the agent's
     `allowed_tools`.
   - Verifies every declared output file exists; halts if not.
3. The next agent runs with the previous agent's output files available
   as inputs.

Agents talk to each other **only through files** in the working directory.
That's the entire handoff mechanism — no shared memory, no in-process state.
Each agent is a fresh `claude_agent_sdk.query()` invocation.

## Built-in workflows

`agentic init` scaffolds these four into `.agentic/workflows/`. Edit or
delete any of them — they version with your project.

### `feature` — spec → explore → implement → test → review → PR

| Agent | Reads | Writes | What it does |
|---|---|---|---|
| `spec` | `task` (string) | `SPEC.md` | Turn the task into precise acceptance criteria; flag ambiguity. |
| `explore` | `SPEC.md` | `CONTEXT.md` | Map relevant files, conventions, integration points. Read-only. |
| `implement` | `SPEC.md`, `CONTEXT.md` | `CHANGES.md` | Make the code changes. No tests. |
| `test` | `CHANGES.md` | `TEST_NOTES.md` | Write + run tests for the new behaviour. 5-attempt budget. |
| `review` | `CHANGES.md` + `git diff` | `REVIEW.md` | Severity-ordered concerns. Read-only. |
| `pr` | `SPEC.md`, `CHANGES.md`, `TEST_NOTES.md`, `REVIEW.md` | `PR_BODY.md` | Compose PR body, `git push`, `gh pr create`. |

### `bugfix` — adds a `repro` stage before the fix

Seven agents: `spec → explore → repro → fix → test → review → pr`. The
`repro` agent writes a failing test that captures the bug *before* `fix`
touches code, so the fix is anchored to a concrete regression test.

```bash
agentic run bugfix --task "duplicate emails sent when retry kicks in"
agentic run bugfix --issue 481
```

### `docs` — no code, no tests

Five agents: `spec → explore → docs → review → pr`. The `review` stage
checks clarity and accuracy instead of code correctness.

```bash
agentic run docs --task "document the new --stub flag in SETUP.md"
```

### `designer` — visual/UX work on an existing UI surface

Five agents: `brief → audit → direction → implement → review`. Aimed at
restyling or refining any existing UI surface (page, component, screen,
view) rather than building a feature from scratch.

| Agent | Reads | Writes | What it does |
|---|---|---|---|
| `brief` | `task` (string) | `BRIEF.md` | Capture intent, scope, mood, must-preserve guardrails. No solutions. |
| `audit` | `BRIEF.md` | `AUDIT.md` | Inventory current type/color/spacing/tokens for the in-scope surface. Read-only. |
| `direction` | `BRIEF.md`, `AUDIT.md` | `DIRECTIONS.md` | Propose 2–3 distinct design directions with tradeoffs and a recommendation. |
| `implement` | `BRIEF.md`, `AUDIT.md`, `DIRECTIONS.md` | `CHANGES.md` | Apply the chosen direction to the codebase. |
| `review` | `BRIEF.md`, `CHANGES.md` + `git diff` | `REVIEW.md` | Severity-ordered design review against brief + UX heuristics. Read-only. |

```bash
agentic run designer --task "refresh the dashboard header — calmer, more data-forward"
```

Note: `direction` is intended as a human checkpoint — the YAML marks it
`human_checkpoint: true`, but the runner does not yet pause on that flag,
so today the workflow runs straight through. To enforce the pause, halt
the run after `direction`, mark a `[CHOSEN]` direction in `DIRECTIONS.md`,
then resume.

## Watching a run

`agentic watch` opens a terminal UI to observe a run — live for in-progress
ones, static for completed ones. Same UI for both.

```bash
agentic watch                  # most recent run in this repo
agentic watch <run-id>         # specific run; full id or 8-char short prefix
agentic watch --list           # print a table of recent runs without opening the TUI
```

`agentic run` prints a `watch this run: agentic watch <short-id>` hint at
start-up so you can drop into the TUI in another terminal.

### What it shows

```
┌─ run <id> · workflow: feature · branch: agentic/feature-... · status: running ─┐
│ ┌─ Agents ────────────┐ ┌─ Transcript: implement ────────────────────────────┐ │
│ │ ✓ spec       00:00:34│ │ assistant I'll start by reading the spec...        │ │
│ │ ✓ explore    00:02:11│ │ tool: Read {"file_path": "src/lib/theme.ts"}      │ │
│ │ ► implement  00:04:52│ │ result: ok export const theme = ...                │ │
│ │   test         —     │ │ tool: Edit {"file_path": "src/components/..."}    │ │
│ │   review       —     │ │ result: ok                                         │ │
│ │   pr           —     │ │ ...                                                │ │
│ └──────────────────────┘ └────────────────────────────────────────────────────┘ │
│ q quit · ↑/↓ select agent · r refresh                                          │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **Left pane:** each agent in workflow order with a status icon (` ` pending, `►` running, `✓` complete, `✗` failed) and elapsed time.
- **Right pane:** chronological transcript for the selected agent — assistant text, tool calls, tool results. Auto-appends as new events arrive (200 ms poll).
- **Keys:** `↑`/`↓` to select an agent, `q` to quit, `r` to force-refresh, `Ctrl+C` to quit.
- **Live vs static:** detected from the `events.jsonl` file. If a `run.complete` event is present at mount, polling never starts.

### Where the data comes from

Every run writes a structured event stream to
`.agentic/runs/<run-id>/events.jsonl`. The TUI reads only this file — it
doesn't shell out, doesn't talk to a daemon, doesn't open a socket.

If you want to consume the stream from another tool, the schema is:

```jsonl
{"ts": "2026-05-12T09:32:15.123Z", "type": "<type>", "agent": "<id|null>", "payload": {...}}
```

Event types (the `AgenticEventType` enum in `src/agentic/events.py`):
`run.start`, `run.complete`, `run.resume`, `agent.start`, `agent.complete`,
`agent.fail`, `agent.pause`, `assistant.text`, `tool.use`, `tool.result`,
`script.start`, `script.complete`, `ci.poll`, `ci.fix.start`,
`ci.fix.complete`, `ci.fix.fail`, `cost`. See `events.py` for the payload
of each.

#### `cost` events

One `cost` event is written after every SDK round-trip — i.e. once per
agent. Its payload reports token usage and the round-trip's USD cost:

```jsonl
{"ts": "...", "type": "cost", "agent": "planner", "payload": {
  "model": "claude-sonnet-4-6",
  "input_tokens": 1840, "output_tokens": 612,
  "cache_read": 12030, "cache_creation": 410,
  "cost_usd": 0.021734}}
```

`cost_usd` is the SDK's own `total_cost_usd` when it reports one, else an
estimate from the price table in `src/agentic/pricing.py`. The runner folds
each `cost` event into `state.json` as `total_tokens`, `total_cost_usd`, and
`per_agent_costs`. In `--stub` mode a zero-cost event is still emitted (no
SDK call is made) so downstream consumers can be exercised offline.

## CLI

| Command | Description |
|---|---|
| `agentic run <name> [--task ...] [--issue N] [--input k=v] [--stub]` | Run a workflow. `--task` and `--issue` are mutually exclusive. |
| `agentic list` | List workflows in `.agentic/workflows/`. |
| `agentic watch [<run-id>] [--list]` | Open the run viewer TUI, or print the run table with `--list`. |
| `agentic logs <run-id>` | Print `.agentic/runs/<run-id>/run.log`. |
| `agentic init` | Scaffold `.agentic/workflows/`, `.agentic/prompts/`, and `.agentic/.gitignore`. |

## Authoring a workflow

A workflow is two things: a YAML file in `.agentic/workflows/` and one prompt
file per agent in `.agentic/prompts/`. To add one:

1. **Drop a YAML in `.agentic/workflows/<name>.yaml`** describing the agents
   (schema below).
2. **Write each prompt** at the `prompt_file` path declared by the agent.
   Use `{{TAG}}` placeholders for inputs and outputs.
3. **Run it:** `agentic run <name> --task "..."`.
4. **Iterate with `--stub`:** `agentic run <name> --task "x" --stub` validates
   the wiring (substitution, output declarations, ordering) without any SDK
   calls.

### Example: a `refactor` workflow

`.agentic/workflows/refactor.yaml`:

```yaml
name: refactor
description: Identify a refactor target → propose a plan → apply → verify tests still pass.

agents:
  - id: target
    prompt_file: prompts/refactor_target.md
    inputs: [task]
    outputs: [TARGET.md]
    allowed_tools: [Read, Grep, Glob, Bash, Write]
    mcp_servers: []
    sub_agents: []

  - id: plan
    prompt_file: prompts/refactor_plan.md
    inputs: [TARGET.md]
    outputs: [PLAN.md]
    allowed_tools: [Read, Write]
    mcp_servers: []
    sub_agents: []

  - id: apply
    prompt_file: prompts/refactor_apply.md
    inputs: [PLAN.md, TARGET.md]
    outputs: [CHANGES.md]
    allowed_tools: [Read, Write, Edit, Bash]
    mcp_servers: []
    sub_agents: []

  - id: verify
    prompt_file: prompts/refactor_verify.md
    inputs: [CHANGES.md]
    outputs: [VERIFY.md]
    allowed_tools: [Read, Bash, Write]
    mcp_servers: []
    sub_agents: []
```

`.agentic/prompts/refactor_target.md` (sketch):

```markdown
You are the `target` agent in a refactor workflow.

The task from the user:

{{TASK}}

Locate the smallest concrete refactor target that satisfies the task. Read
files freely. Do NOT modify code.

Write your output to {{OUTPUT}} with sections: Target, Why, Risks, Out of scope.
```

Then:

```bash
agentic run refactor --task "extract the auth env-var parsing into a helper" --stub  # wiring check
agentic run refactor --task "extract the auth env-var parsing into a helper"         # real run
```

### Design tips

- **Keep each agent narrow.** One artifact in, one artifact out. If an agent's
  job spans two distinct outputs, split it.
- **Read-only agents** (explore, review) should not list `Edit` or `Write` on
  source — restrict `allowed_tools` to enforce it.
- **Branch + PR stages** require `Bash` (for `git`/`gh`). Most other agents
  don't.
- The first input is loaded *inline* into the prompt; later inputs are passed
  as paths the agent reads on demand. Put the most context-critical artifact
  first.

## Workflow YAML schema

```yaml
name: feature
description: ...
agents:
  - id: spec                            # required, unique within the workflow
    prompt_file: prompts/spec.md        # resolved against .agentic/ in the target repo
    inputs: [task]                      # named inputs — see substitution below
    outputs: [SPEC.md]                  # files this agent MUST write to the working dir
    allowed_tools: [Read, Write]        # passed to ClaudeAgentOptions.allowed_tools
    mcp_servers: []                     # reserved for future use; must be [] for now
    sub_agents: []                      # reserved for future use; must be [] for now
```

### Input semantics

Each prompt file uses `{{TAG}}` placeholders. The runner substitutes them
based on declared `inputs`:

- **First input** → substituted as **file contents** (or raw value for kv inputs like `task`).
- **Subsequent inputs** → substituted as **absolute paths** (the agent reads them via the Read tool as needed).
- Tag format: `<INPUT_NAME>` uppercased, dots/dashes → underscores. So
  `task` → `{{TASK}}`, `SPEC.md` → `{{SPEC_MD}}`.

This keeps the first input (the immediate context the agent must act on)
compact and pre-loaded, while later inputs (reference material) stay on disk
so the agent can re-read sections without bloating its context.

### Additional tags

The runner also substitutes:

- `{{OUTPUT}}` → absolute path to the agent's single declared output (when there's only one).
- `{{OUTPUT_<NAME>}}` → absolute path to each declared output.
- `{{WORKING_DIR}}` → run-scoped working directory.
- `{{TARGET_REPO}}` → target repo root.

## Architecture

```
src/agentic/
  cli.py        # click: run / list / logs / init
  workflow.py   # Workflow pydantic model + YAML loader
  agent.py      # AgentSpec model + real SDK path + stub path
  runner.py     # run_workflow(): branch management + per-agent execute + halt
  context.py    # RunContext (no globals — threads through all operations)
  logging.py    # per-run file handler + rich console
  scaffold/     # files copied by `agentic init`
```

No globals — every public function takes a `RunContext`. This is what lets
multiple workflow runs share a process safely.

## Tests

```bash
pip install -e ".[dev]"
pytest -v
```

18 tests covering: workflow loading and schema validation, stub-mode happy
path, failure halting, output verification, full feature-workflow integration
against a real git repo (branch creation, dirty-tree refusal, branch-from-HEAD,
working dir persists after failure). No SDK calls in CI.
