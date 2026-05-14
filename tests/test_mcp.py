"""MCP server schema + resolution tests."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentic.context import RunContext
from agentic.agent import _resolve_mcp_for_agent, AgentSpec
from agentic.mcp import MCPServerSpec, resolve_for_agent
from agentic.workflow import Workflow


def test_stdio_sdk_dict_shape() -> None:
    s = MCPServerSpec(name="fs", type="stdio", command="npx", args=["-y", "x"], env={"K": "V"})
    d = s.to_sdk_dict()
    assert d == {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"K": "V"}}


def test_http_sdk_dict_shape() -> None:
    s = MCPServerSpec(
        name="linear",
        type="http",
        url="https://mcp.linear.app/sse",
        headers={"Authorization": "Bearer t"},
    )
    d = s.to_sdk_dict()
    assert d == {
        "type": "http",
        "url": "https://mcp.linear.app/sse",
        "headers": {"Authorization": "Bearer t"},
    }


def test_env_interpolation_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_TOKEN", "tok-xyz")
    s = MCPServerSpec(
        name="linear",
        type="http",
        url="https://x",
        headers={"Authorization": "Bearer ${LINEAR_TOKEN}"},
    )
    d = s.to_sdk_dict()
    assert d["headers"]["Authorization"] == "Bearer tok-xyz"


def test_stdio_requires_command() -> None:
    with pytest.raises(Exception, match="command"):
        MCPServerSpec(name="fs", type="stdio")


def test_http_requires_url() -> None:
    with pytest.raises(Exception, match="url"):
        MCPServerSpec(name="x", type="http")


def test_workflow_mcp_schema(tmp_path: Path) -> None:
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "x",
        "mcp_servers": [
            {"name": "fs", "type": "stdio", "command": "echo"},
            {"name": "lnr", "type": "http", "url": "https://x"},
        ],
        "agents": [
            {"id": "a", "prompt_file": "p.md", "outputs": ["A.md"], "mcp_servers": ["fs"]},
        ],
    }))
    wf = Workflow.load(p)
    assert len(wf.mcp_servers) == 2
    assert wf.agents[0].mcp_servers == ["fs"]


def test_agent_referencing_undeclared_mcp_raises(tmp_path: Path) -> None:
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "x",
        "mcp_servers": [{"name": "fs", "type": "stdio", "command": "echo"}],
        "agents": [
            {"id": "a", "prompt_file": "p.md", "outputs": ["A.md"], "mcp_servers": ["ghost"]},
        ],
    }))
    with pytest.raises(Exception, match="ghost"):
        Workflow.load(p)


def test_resolve_for_agent_builds_named_dict() -> None:
    servers = [
        MCPServerSpec(name="fs", type="stdio", command="echo"),
        MCPServerSpec(name="lnr", type="http", url="https://x"),
    ]
    out = resolve_for_agent(["fs"], servers)
    assert set(out.keys()) == {"fs"}
    assert out["fs"]["type"] == "stdio"


def test_resolve_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown mcp server"):
        resolve_for_agent(["ghost"], [])


def test_resolve_for_agent_via_ctx_reaches_sdk_dict() -> None:
    """The real wiring path: AgentSpec.mcp_servers names + ctx.workflow_mcp_servers
    -> dict that would be passed to ClaudeAgentOptions.mcp_servers.
    """
    ctx = RunContext.create(
        workflow_name="x",
        target_repo_path=Path("/tmp"),
    )
    ctx.workflow_mcp_servers = [MCPServerSpec(name="fs", type="stdio", command="echo")]
    spec = AgentSpec(id="a", mcp_servers=["fs"], outputs=["A.md"])
    out = _resolve_mcp_for_agent(spec, ctx)
    assert set(out.keys()) == {"fs"}
    assert out["fs"]["command"] == "echo"


def test_no_mcp_returns_empty() -> None:
    ctx = RunContext.create(workflow_name="x", target_repo_path=Path("/tmp"))
    spec = AgentSpec(id="a", outputs=["A.md"])
    assert _resolve_mcp_for_agent(spec, ctx) == {}


def test_scaffold_filesystem_mcp_parses() -> None:
    """Sanity-check: the bundled scaffold workflow loads as a valid Workflow."""
    src = Path(__file__).resolve().parent.parent / "src" / "agentic" / "scaffold" / "workflows" / "filesystem-mcp.yaml"
    wf = Workflow.load(src)
    assert wf.name == "filesystem-mcp"
    assert len(wf.mcp_servers) == 1
    assert wf.mcp_servers[0].name == "filesystem"
    assert wf.agents[0].mcp_servers == ["filesystem"]
