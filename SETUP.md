# Setting up `agentic`

A CLI that takes a task description and runs it through a 6-agent Claude
pipeline: **spec → explore → implement → test → review → open-PR**. Each agent
is a focused Claude Code SDK invocation with its own prompt, scoped tools, and
declared inputs/outputs. The framework handles branch management, structured
event logging, and a TUI for watching runs.

You hand it a task. It opens a PR.

## Prerequisites

- **Python 3.11+**
- **git** and **`gh`** ([GitHub CLI](https://cli.github.com/) — run `gh auth login` once)
- **Claude auth** — one of:
  - `claude login` *(recommended)* — uses your Max/Pro plan; no API credits spent
  - `export ANTHROPIC_API_KEY=...` — bills your API account; for CI or if you don't have a Claude subscription

`agentic` will refuse to run if neither is configured, and warns at run start
if it's using the API-key path (so you don't accidentally burn credits when
you meant to use your subscription).

## Install

```bash
# 1. Get the code
git clone <this-repo-url> ~/code/agentic-layer
cd ~/code/agentic-layer

# 2. Create a venv and install in editable mode
python3 -m venv .venv
.venv/bin/pip install -e .

# 3. Make `agentic` callable from anywhere (one-time)
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/agentic" ~/.local/bin/agentic

# If ~/.local/bin isn't on your PATH, add to ~/.bashrc (or ~/.zshrc):
#   export PATH="$HOME/.local/bin:$PATH"

# 4. Verify
agentic --version
agentic -h        # full reference: quick-start, workflow, file layout
```

If you ever move the project, update the symlink:

```bash
rm ~/.local/bin/agentic
ln -s /new/path/to/agentic-layer/.venv/bin/agentic ~/.local/bin/agentic
```

## First run

Go to any git repo where you want to try it:

```bash
cd ~/code/some-project
git checkout main                # be on the branch you want PRs to target

# Scaffold workflows + prompts into .agentic/
agentic init
git add .agentic && git commit -m "scaffold agentic workflows"

# Try the wiring first with --stub (no SDK calls, no tokens spent)
agentic run feature --task "add a --dry-run flag to deploy" --stub

# Watch it in another terminal (or after it finishes)
agentic watch

# Then for real:
agentic run feature --task "..."
```

## What happens during a real run

1. Refuses if the working tree is dirty (`git status` must be clean).
2. Creates `agentic/feature-<short-id>` branch from your current HEAD.
3. Six agents run sequentially, each reading the previous one's output:

   ```
   spec  →  explore  →  implement  →  test  →  review  →  pr
   ```

4. The final agent pushes the branch and opens a PR via `gh pr create`.

If any agent fails, the branch and `.agentic/runs/<id>/` are left intact for
inspection. Use `agentic watch <id>` for the TUI or `agentic logs <id>` for
the raw log.

## Multi-line tasks

Shell quoting gets fiddly for prose-style tasks (quotes, apostrophes, parens).
For anything longer than a sentence, use a heredoc into a variable:

```bash
TASK=$(cat <<'EOF'
build a pomodoro timer app with session log. Start/pause/skip a 25/5
cycle, log completed sessions with optional tags ("deep work", "email"),
show today's count and a weekly chart. Has timing logic, persistence,
charts — meaty enough that the agents have to think.
EOF
)
agentic run feature --task "$TASK"
```

The `<<'EOF'` (quoted heredoc) disables variable and quote interpretation, so
any characters work.

## Common workflows

| Command | What it does |
|---|---|
| `agentic init` | Scaffold `.agentic/` in the current repo with the `feature` workflow + prompts |
| `agentic run feature --task "..."` | Drive the 6-agent pipeline on a task |
| `agentic run feature --issue 142` | Same, but pull the task from a GitHub issue |
| `agentic run feature --task "..." --stub` | Same wiring without SDK calls — for testing |
| `agentic watch` | Open the TUI on the most recent run |
| `agentic watch <id>` | Open the TUI on a specific run (8-char prefix is fine) |
| `agentic watch --list` | Table of recent runs in this repo |
| `agentic logs <id>` | Print the raw `run.log` for a past run |
| `agentic list` | List workflows defined in `.agentic/workflows/` |
| `agentic -h` | Full reference, quick-start, workflow, tips |
| `agentic <cmd> -h` | Per-command help with examples |

## File layout in your repo (after `init`)

```
.agentic/
├── workflows/
│   └── feature.yaml          # the 6-agent pipeline definition
├── prompts/
│   ├── spec.md               # one prompt per agent — edit to taste
│   ├── explore.md
│   ├── implement.md
│   ├── test.md
│   ├── review.md
│   └── pr.md
├── runs/                     # gitignored; one subdir per run
│   └── <run-id>/
│       ├── events.jsonl      # structured event stream (feeds `watch`)
│       ├── run.log           # human-readable log
│       └── SPEC.md, CONTEXT.md, CHANGES.md, ...  (per-agent outputs)
└── .gitignore                # ignores runs/
```

The prompts are yours to customise — they ship as a sensible default, but
you'll want to tune them for your codebase's conventions.

## Defining your own workflows

The `feature` workflow is just one example. You can drop more YAML files into
`.agentic/workflows/` for different kinds of tasks — a lightweight `quick`
workflow for single-file changes, a `bugfix` workflow with a diagnose step
first, whatever fits your work. See `feature.yaml` for the schema; the README
has more.

## Tips

- `--stub` is your friend. Run any new workflow with `--stub` first to confirm
  the pipeline executes end-to-end before spending tokens.
- Each `agentic run` creates its own branch — you never manage branches yourself.
  After a successful run you're left on the agentic branch with a PR open;
  switch back to your base branch (`git checkout main`) to start the next task.
- If your repo's default branch isn't `main`, the auto-PR still targets whatever
  `gh repo view` resolves as the default branch — no config needed.
- Watch the TUI in a second terminal during your first real run. It makes the
  abstract "agents" concept concrete fast.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: agentic` | `~/.local/bin` not on PATH; add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc |
| `refused to run: working tree has uncommitted changes` | `git commit` or `git stash` first; agentic won't touch dirty trees |
| `refused to run: no auth configured` | `claude login` (recommended) or `export ANTHROPIC_API_KEY=...` |
| `WARNING auth: ANTHROPIC_API_KEY (billing: API account)` | You probably wanted `claude login` — unset the env var with `unset ANTHROPIC_API_KEY` |
| Run halted mid-pipeline | The branch and `.agentic/runs/<id>/` are intact. `agentic watch <id>` or `agentic logs <id>` to diagnose; delete the branch with `git branch -D agentic/feature-<id>` to start fresh |
| `gh pr create` fails | Make sure `gh auth login` worked and the repo has a GitHub remote (`git remote -v`) |
