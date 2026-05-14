"""MCP server config plumbing.

Workflow YAML declares MCP servers at the workflow level:

    mcp_servers:
      - name: filesystem
        type: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
        env: {ALLOWED_DIRS: "/tmp"}

      - name: linear
        type: http
        url: https://mcp.linear.app/sse
        headers: {Authorization: "Bearer ${LINEAR_TOKEN}"}

Each agent opts into the servers it can use by listing names in
`AgentSpec.mcp_servers`. The runner resolves those names against the
workflow's declared servers, interpolates `${VAR}` from os.environ, and
hands the result to `ClaudeAgentOptions.mcp_servers` keyed by name.
"""
from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class MCPServerSpec(BaseModel):
    name: str
    type: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("mcp_servers[].name is required")
        return v

    def model_post_init(self, __context: Any) -> None:  # pydantic v2 hook
        if self.type == "stdio":
            if not self.command:
                raise ValueError(
                    f"mcp_servers[{self.name}]: command is required for stdio type"
                )
        else:
            if not self.url:
                raise ValueError(
                    f"mcp_servers[{self.name}]: url is required for http type"
                )

    def to_sdk_dict(self) -> dict[str, Any]:
        """Shape expected by ClaudeAgentOptions.mcp_servers values."""
        if self.type == "stdio":
            return {
                "type": "stdio",
                "command": self.command,
                "args": list(self.args),
                "env": {k: _interp(v) for k, v in self.env.items()},
            }
        return {
            "type": "http",
            "url": self.url,
            "headers": {k: _interp(v) for k, v in self.headers.items()},
        }


_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interp(value: str) -> str:
    """${VAR} substitution from os.environ. unknown vars become empty string."""
    if not isinstance(value, str):
        return value
    return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def resolve_for_agent(
    agent_mcp_names: list[str], workflow_servers: list[MCPServerSpec]
) -> dict[str, dict[str, Any]]:
    """Look up the named servers and build the SDK dict.

    Raises ValueError if an agent references a server name the workflow
    doesn't declare — fail loud, not silent.
    """
    by_name = {s.name: s for s in workflow_servers}
    out: dict[str, dict[str, Any]] = {}
    for name in agent_mcp_names:
        if name not in by_name:
            raise ValueError(
                f"agent references unknown mcp server: {name!r}. "
                f"workflow declares: {sorted(by_name)}"
            )
        out[name] = by_name[name].to_sdk_dict()
    return out
