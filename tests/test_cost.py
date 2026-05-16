"""Cost capture: price table, `cost` event emission, RunState aggregation."""
import json
from pathlib import Path

import pytest

from agentic import pricing
from agentic.context import RunContext
from agentic.events import AgenticEventType, EventEmitter
from agentic.runner import run_workflow
from agentic.state import RunState
from agentic.workflow import Workflow


def _read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --------------------------------------------------------------------------
# price table sanity
# --------------------------------------------------------------------------

def test_price_table_covers_three_families():
    assert set(pricing.MODEL_PRICING) == {"opus", "sonnet", "haiku"}


def test_price_table_ordering_is_sane():
    opus = pricing.MODEL_PRICING["opus"]
    sonnet = pricing.MODEL_PRICING["sonnet"]
    haiku = pricing.MODEL_PRICING["haiku"]
    # opus is the priciest tier, haiku the cheapest
    assert opus.input > sonnet.input > haiku.input
    assert opus.output > sonnet.output > haiku.output
    # within a family: output dearer than input; cache read cheapest channel
    for p in (opus, sonnet, haiku):
        assert p.output > p.input
        assert p.cache_read < p.input < p.cache_write


def test_model_family_matching():
    assert pricing.model_family("claude-opus-4-7") == "opus"
    assert pricing.model_family("claude-sonnet-4-6") == "sonnet"
    assert pricing.model_family("claude-3-5-haiku-20241022") == "haiku"
    # unknown / empty ids fall back to the default tier
    assert pricing.model_family("stub") == pricing.DEFAULT_FAMILY
    assert pricing.model_family("") == pricing.DEFAULT_FAMILY


def test_cost_usd_zero_for_zero_tokens():
    assert pricing.cost_usd("claude-sonnet-4-6", 0, 0) == 0.0


def test_cost_usd_one_mtok_equals_rate():
    sonnet = pricing.MODEL_PRICING["sonnet"]
    assert pricing.cost_usd("claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(sonnet.input)
    assert pricing.cost_usd("claude-sonnet-4-6", 0, 1_000_000) == pytest.approx(sonnet.output)


def test_cost_usd_counts_every_token_channel():
    cost = pricing.cost_usd(
        "claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    p = pricing.MODEL_PRICING["opus"]
    assert cost == pytest.approx(p.input + p.output + p.cache_read + p.cache_write)


def test_cost_usd_opus_dearer_than_haiku():
    args = dict(input_tokens=500_000, output_tokens=500_000)
    assert pricing.cost_usd("claude-opus-4-7", **args) > pricing.cost_usd(
        "claude-haiku-4-5", **args
    )


# --------------------------------------------------------------------------
# cost event roundtrip
# --------------------------------------------------------------------------

def test_event_type_enum_includes_cost():
    assert AgenticEventType.COST.value == "cost"
    # str-enum: JSON-serialises as the bare string
    assert isinstance(AgenticEventType.COST, str)
    assert json.dumps({"type": AgenticEventType.COST}) == '{"type": "cost"}'


def test_emit_accepts_enum_and_raw_string(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    em = EventEmitter(path)
    em.emit(AgenticEventType.RUN_START, workflow="w")
    em.emit("agent.start", agent="a")
    assert [e["type"] for e in _read(path)] == ["run.start", "agent.start"]


def test_emit_cost_writes_a_cost_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    em = EventEmitter(path)
    em.emit_cost(
        agent="planner",
        model="claude-sonnet-4-6",
        input_tokens=1200,
        output_tokens=300,
        cache_read=800,
        cache_creation=100,
        cost_usd=0.0123456789,
    )
    events = _read(path)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "cost"
    assert ev["agent"] == "planner"
    p = ev["payload"]
    assert p["model"] == "claude-sonnet-4-6"
    assert p["input_tokens"] == 1200
    assert p["output_tokens"] == 300
    assert p["cache_read"] == 800
    assert p["cache_creation"] == 100
    # cost is rounded to 6dp on the way out
    assert p["cost_usd"] == 0.012346


# --------------------------------------------------------------------------
# RunState aggregation
# --------------------------------------------------------------------------

def test_runstate_starts_with_zero_cost():
    st = RunState(run_id="r", workflow_name="w", target_repo_path="/tmp/x")
    assert st.total_tokens == 0
    assert st.total_cost_usd == 0.0
    assert st.per_agent_costs == {}


def test_runstate_add_cost_aggregates_per_agent_and_total():
    st = RunState(run_id="r", workflow_name="w", target_repo_path="/tmp/x")
    st.add_cost(agent="planner", input_tokens=1000, output_tokens=200, cost_usd=0.05)
    st.add_cost(agent="builder", input_tokens=3000, output_tokens=900, cost_usd=0.20)
    st.add_cost(agent="planner", input_tokens=500, output_tokens=100, cost_usd=0.03)
    assert st.total_tokens == 1000 + 200 + 3000 + 900 + 500 + 100
    assert st.total_cost_usd == pytest.approx(0.28)
    assert st.per_agent_costs["planner"] == pytest.approx(0.08)
    assert st.per_agent_costs["builder"] == pytest.approx(0.20)


def test_runstate_cost_survives_save_and_load(tmp_path: Path):
    st = RunState(run_id="r", workflow_name="w", target_repo_path="/tmp/x")
    st.add_cost(agent="planner", input_tokens=1000, output_tokens=200, cost_usd=0.05)
    st.save(tmp_path)
    reloaded = RunState.load(tmp_path)
    assert reloaded.total_tokens == 1200
    assert reloaded.total_cost_usd == pytest.approx(0.05)
    assert reloaded.per_agent_costs == {"planner": 0.05}


def test_runstate_loads_legacy_state_without_cost_fields(tmp_path: Path):
    """state.json written before Phase 1 has no cost keys — must still load."""
    legacy = {
        "run_id": "r",
        "workflow_name": "w",
        "target_repo_path": "/tmp/x",
        "status": "succeeded",
        "current_agent_index": 2,
    }
    (tmp_path / "state.json").write_text(json.dumps(legacy))
    st = RunState.load(tmp_path)
    assert st.total_tokens == 0
    assert st.total_cost_usd == 0.0
    assert st.per_agent_costs == {}


# --------------------------------------------------------------------------
# end-to-end aggregation under stub mode
# --------------------------------------------------------------------------

def test_stub_run_emits_cost_event_per_agent(repo: Path):
    """A full stub run emits one `cost` event per agent and aggregates them
    onto RunState — exercising the events.jsonl -> RunState path with no SDK.
    """
    wf = Workflow.find("test-workflow", repo)
    ctx = RunContext.create(
        workflow_name=wf.name,
        target_repo_path=repo,
        inputs={"task": "build a thing"},
        stub_mode=True,
    )
    run_workflow(wf, ctx)

    events = _read(ctx.working_dir / "events.jsonl")
    cost_events = [e for e in events if e["type"] == "cost"]
    # test-workflow has three agents: spec, explore, plan
    assert {e["agent"] for e in cost_events} == {"spec", "explore", "plan"}
    for e in cost_events:
        assert e["payload"]["model"] == "stub"
        assert e["payload"]["cost_usd"] == 0.0

    state = RunState.load(ctx.working_dir)
    assert set(state.per_agent_costs) == {"spec", "explore", "plan"}
    assert state.total_cost_usd == 0.0
    assert state.total_tokens == 0
