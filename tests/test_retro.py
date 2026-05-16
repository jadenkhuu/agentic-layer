"""Retrospective builder + the bundled `retrospective` agent template."""
import json
from pathlib import Path

import agentic
from agentic.context import RunContext
from agentic.retro import (
    RETRO_FILENAME,
    build_retro,
    collect_stats,
    write_retro,
)
from agentic.runner import run_workflow
from agentic.workflow import Workflow

SCAFFOLD = Path(agentic.__file__).parent / "scaffold"


def _write_run(
    run_dir: Path,
    events: list[dict],
    state: dict | None = None,
) -> Path:
    """Materialise a run dir with an events.jsonl (+ optional state.json)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run_dir


def _ev(etype: str, agent: str | None = None, **payload) -> dict:
    return {"ts": "2026-05-16T12:00:00.000Z", "type": etype, "agent": agent,
            "payload": payload}


# a representative feature-style run: spec ok, implement ok (slow + pricey),
# test failed after two failed tool calls.
SAMPLE_EVENTS = [
    _ev("run.start", workflow="feature", agent_count=3),
    _ev("agent.start", "spec", agent_id="spec"),
    _ev("cost", "spec", model="sonnet", input_tokens=1000, output_tokens=200,
        cost_usd=0.01),
    _ev("agent.complete", "spec", agent_id="spec", elapsed_seconds=2.0),
    _ev("agent.start", "implement", agent_id="implement"),
    _ev("tool.use", "implement", tool_name="Edit"),
    _ev("tool.result", "implement", success=True),
    _ev("tool.result", "implement", success=False),
    _ev("tool.result", "implement", success=False),
    _ev("tool.result", "implement", success=False),
    _ev("cost", "implement", model="opus", input_tokens=40000,
        output_tokens=8000, cost_usd=0.80),
    _ev("agent.complete", "implement", agent_id="implement",
        elapsed_seconds=40.0),
    _ev("agent.start", "test", agent_id="test"),
    _ev("agent.fail", "test", agent_id="test", error="pytest: 2 failed\nFAILED test_x"),
    _ev("run.complete", status="failed", elapsed_seconds=44.0,
        failed_agent="test"),
]
SAMPLE_STATE = {
    "run_id": "20260516-120000-abcd1234",
    "workflow_name": "feature",
    "status": "failed",
    "total_tokens": 49200,
    "total_cost_usd": 0.81,
    "per_agent_costs": {"spec": 0.01, "implement": 0.80},
}


# --------------------------------------------------------------- collect_stats


def test_collect_stats_rolls_up_each_agent():
    stats = collect_stats(SAMPLE_EVENTS)
    by_id = {s.agent: s for s in stats}
    assert [s.agent for s in stats] == ["spec", "implement", "test"]

    assert by_id["spec"].ok
    assert by_id["spec"].elapsed_seconds == 2.0
    assert by_id["spec"].cost_usd == 0.01

    impl = by_id["implement"]
    assert impl.ok
    assert impl.tool_calls == 1
    assert impl.tool_errors == 3
    assert impl.input_tokens == 40000 and impl.output_tokens == 8000

    test = by_id["test"]
    assert test.failed and not test.ok
    assert "pytest" in test.error


def test_collect_stats_marks_in_progress_agent():
    """An agent that started but never completed is neither ok nor failed."""
    stats = collect_stats([_ev("agent.start", "retro", agent_id="retro")])
    assert stats[0].in_progress
    assert not stats[0].ok and not stats[0].failed


# ----------------------------------------------------------------- build_retro


def test_build_retro_has_all_five_sections(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    md = build_retro(run_dir)
    for section in (
        "# Retrospective",
        "## What worked",
        "## What didn't",
        "## Time by agent",
        "## Cost by agent",
        "## Improvements",
    ):
        assert section in md, f"missing section: {section}"


def test_build_retro_reports_workflow_and_status(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    md = build_retro(run_dir)
    assert "**Workflow:** feature" in md
    assert "**Status:** failed" in md
    assert "20260516-120000-abcd1234" in md


def test_build_retro_what_worked_and_didnt(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    md = build_retro(run_dir)
    worked = md.split("## What worked")[1].split("## What didn't")[0]
    didnt = md.split("## What didn't")[1].split("## Time by agent")[0]
    # spec + implement completed cleanly
    assert "`spec`" in worked and "`implement`" in worked
    # test failed; implement had failed tool calls
    assert "`test` failed" in didnt
    assert "pytest" in didnt
    assert "failed tool call" in didnt


def test_build_retro_time_and_cost_tables(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    md = build_retro(run_dir)
    # implement is 40 of 42 agent-seconds -> dominant share
    assert "| `implement` | 40.0s |" in md
    # cost-by-agent uses the per-agent cost events
    assert "| `implement` | $0.80 |" in md
    assert "| `spec` | $0.01 |" in md


def test_build_retro_improvements_flag_failure_and_bottleneck(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    improvements = build_retro(run_dir).split("## Improvements")[1]
    # the failed agent earns a concrete next-action
    assert "`test` failed" in improvements
    assert "agentic fork" in improvements
    # implement dominated both time and cost
    assert "`implement`" in improvements
    # three failed tool calls trip the tool-error heuristic
    assert "failed tool calls" in improvements


def test_build_retro_clean_run_has_baseline_note(tmp_path: Path):
    clean = [
        _ev("run.start", workflow="docs", agent_count=1),
        _ev("agent.start", "writer", agent_id="writer"),
        _ev("cost", "writer", model="sonnet", input_tokens=10,
            output_tokens=5, cost_usd=0.001),
        _ev("agent.complete", "writer", agent_id="writer", elapsed_seconds=1.5),
        _ev("run.complete", status="success", elapsed_seconds=1.6),
    ]
    run_dir = _write_run(tmp_path / "run", clean,
                         {"run_id": "r", "workflow_name": "docs",
                          "status": "succeeded"})
    md = build_retro(run_dir)
    assert "Nothing — every agent completed without errors." in md
    assert "ran clean" in md.split("## Improvements")[1]


def test_build_retro_tolerates_missing_files_and_bad_lines(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    # no events.jsonl, no state.json — must not raise
    md = build_retro(empty)
    assert "# Retrospective" in md

    run_dir = tmp_path / "messy"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        "not json\n" + json.dumps(_ev("agent.start", "a", agent_id="a")) + "\n"
        "\n{bad\n",
        encoding="utf-8",
    )
    md = build_retro(run_dir)
    assert "`a`" in md  # the one valid line still counted


def test_write_retro_writes_file(tmp_path: Path):
    run_dir = _write_run(tmp_path / "run", SAMPLE_EVENTS, SAMPLE_STATE)
    path = write_retro(run_dir)
    assert path == run_dir / RETRO_FILENAME
    assert path.exists()
    assert "# Retrospective" in path.read_text(encoding="utf-8")


# ----------------------------------------------------- bundled agent template


def test_retrospective_template_is_valid():
    wf = Workflow.load(SCAFFOLD / "workflows" / "retrospective.yaml")
    assert wf.name == "retrospective"
    assert [a.id for a in wf.agents] == ["retrospective"]
    agent = wf.agents[0]
    assert agent.outputs == ["RETRO.md"]
    assert agent.prompt_file == "prompts/retro.md"
    # the prompt the template references must be bundled alongside it
    assert (SCAFFOLD / "prompts" / "retro.md").exists()


def test_run_with_retrospective_agent_writes_retro(tmp_path: Path):
    """Acceptance: a stub run whose workflow ends in a retrospective agent
    produces a genuine RETRO.md — not the generic `[stub]` placeholder."""
    wf_dir = tmp_path / ".agentic" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "with-retro.yaml").write_text(
        "name: with-retro\n"
        "description: a worker agent followed by a retrospective agent.\n"
        "agents:\n"
        "  - id: build\n"
        "    inputs: [task]\n"
        "    outputs: [OUT.md]\n"
        "    allowed_tools: [Write]\n"
        "  - id: retrospective\n"
        "    inputs: []\n"
        "    outputs: [RETRO.md]\n"
        "    allowed_tools: [Read, Write]\n",
        encoding="utf-8",
    )
    wf = Workflow.find("with-retro", tmp_path)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=tmp_path,
        inputs={"task": "ship a thing"},
        stub_mode=True,
    )
    run_workflow(wf, ctx)

    retro = ctx.working_dir / "RETRO.md"
    assert retro.exists(), "the retrospective agent must write RETRO.md"
    text = retro.read_text(encoding="utf-8")
    assert text.startswith("# Retrospective")
    assert "[stub]" not in text  # a real report, not the placeholder
    # the worker agent that ran before it shows up in the retro
    assert "`build`" in text
    assert "## Cost by agent" in text
