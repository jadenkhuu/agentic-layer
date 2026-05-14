"""--client config: loading, formatting, prompt-prefix injection."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from agentic.client_config import ClientConfig, find_client_config, load_client
from agentic.context import RunContext
from agentic.runner import run_workflow
from agentic.workflow import Workflow


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(l)
        for l in (run_dir / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]


def test_loads_and_formats() -> None:
    cfg = ClientConfig(
        name="purpl",
        stack="ts",
        conventions=["no inline styles"],
        do=["reuse Card"],
        do_not=["new design systems"],
    )
    prefix = cfg.as_system_prefix()
    assert "client context — purpl" in prefix
    assert "stack: ts" in prefix
    assert "  - reuse Card" in prefix
    assert "  - new design systems" in prefix


def test_find_in_target_repo(tmp_path: Path) -> None:
    p = tmp_path / ".agentic" / "clients" / "ghost.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("name: ghost\n")
    assert find_client_config("ghost", [tmp_path]) == p


def test_find_returns_none_for_missing(tmp_path: Path) -> None:
    assert find_client_config("ghost", [tmp_path]) is None


def test_load_client_raises_with_search_path_hint(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="ghost"):
        load_client("ghost", [tmp_path])


def test_load_yaml(tmp_path: Path) -> None:
    p = tmp_path / "clients" / "purpl.yaml"
    p.parent.mkdir()
    p.write_text(textwrap.dedent("""
        name: purpl
        stack: ts
        conventions: [no inline]
        do: [reuse]
        do_not: [forbid]
    """))
    cfg = ClientConfig.load(p)
    assert cfg.name == "purpl"
    assert cfg.conventions == ["no inline"]


def test_client_prefix_reaches_agent_prompt(tmp_path: Path) -> None:
    """In stub mode the runner emits an `assistant.text` event mirroring the
    prefix so we can verify the client config reaches every agent without an
    SDK call.
    """
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "x",
        "agents": [{"id": "a", "outputs": ["A.md"]}],
    }))
    wf = Workflow.load(p)
    ctx = RunContext.create(workflow_name=wf.name, target_repo_path=tmp_path,
                            inputs={"task": "t"}, stub_mode=True)
    cfg = ClientConfig(name="purpl", conventions=["marker-XYZ"])
    run_workflow(wf, ctx, client_config=cfg)
    events = _events(ctx.working_dir)
    texts = [e for e in events if e["type"] == "assistant.text"]
    assert texts
    assert "marker-XYZ" in texts[0]["payload"]["text"]
    # client name is recorded on run.start
    start = [e for e in events if e["type"] == "run.start"][0]
    assert start["payload"]["client"] == "purpl"


def test_bundled_purpl_client_loads() -> None:
    """Sanity check: the scaffold client config we ship is valid."""
    pkg_root = Path(__file__).resolve().parent.parent
    cfg = load_client("purpl", [pkg_root / "src" / "agentic" / "scaffold"])
    assert cfg.name == "purpl"
    assert any("tailwind" in c.lower() for c in cfg.conventions)
