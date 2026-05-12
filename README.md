# agentic

Orchestrator for **Claude Agent SDK** workflows. A workflow is an ordered list
of *agents* (each a `claude_agent_sdk.query()` invocation) that hand off to one
another via files in a run-scoped working directory.

The stage-2 release ships a working **`feature` workflow** — six agents that
take a task description through spec → explore → implement → test → review → PR.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `agentic` CLI and `claude-agent-sdk` as a dependency.

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

## The `feature` workflow

| Agent | Reads | Writes | What it does |
|---|---|---|---|
| `spec` | `task` (string) | `SPEC.md` | Turn the task into precise acceptance criteria; flag ambiguity. |
| `explore` | `SPEC.md` | `CONTEXT.md` | Map relevant files, conventions, integration points. Read-only. |
| `implement` | `SPEC.md`, `CONTEXT.md` | `CHANGES.md` | Make the code changes. No tests. |
| `test` | `CHANGES.md` | `TEST_NOTES.md` | Write + run tests for the new behaviour. 5-attempt budget. |
| `review` | `CHANGES.md` + `git diff` | `REVIEW.md` | Severity-ordered concerns. Read-only. |
| `pr` | `SPEC.md`, `CHANGES.md`, `TEST_NOTES.md`, `REVIEW.md` | `PR_BODY.md` | Compose PR body, `git push`, `gh pr create`. |

Each agent's prompt lives at `.agentic/prompts/<id>.md`. Edit them to taste —
they're scaffolded into your repo so they version with your project.

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

Event types: `run.start`, `run.complete`, `agent.start`, `agent.complete`,
`agent.fail`, `tool.use`, `tool.result`, `assistant.text`. See
`src/agentic/events.py` for the payload of each.

## CLI

| Command | Description |
|---|---|
| `agentic run <name> [--task ...] [--issue N] [--input k=v] [--stub]` | Run a workflow. `--task` and `--issue` are mutually exclusive. |
| `agentic list` | List workflows in `.agentic/workflows/`. |
| `agentic watch [<run-id>] [--list]` | Open the run viewer TUI, or print the run table with `--list`. |
| `agentic logs <run-id>` | Print `.agentic/runs/<run-id>/run.log`. |
| `agentic init` | Scaffold `.agentic/workflows/`, `.agentic/prompts/`, and `.agentic/.gitignore`. |

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
# agentic-layer
