from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from agentic.agent import AgentSpec
from agentic.mcp import MCPServerSpec


class Workflow(BaseModel):
    name: str
    description: str = ""
    agents: list[AgentSpec] = Field(default_factory=list)
    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    pre_run: str | None = None
    post_run: str | None = None

    @field_validator("agents")
    @classmethod
    def _agents_non_empty_unique(cls, v: list[AgentSpec]) -> list[AgentSpec]:
        if not v:
            raise ValueError("workflow must declare at least one agent")
        ids = [a.id for a in v]
        dups = {x for x in ids if ids.count(x) > 1}
        if dups:
            raise ValueError(f"duplicate agent ids: {sorted(dups)}")
        return v

    @model_validator(mode="after")
    def _agent_mcp_refs_resolve(self) -> "Workflow":
        declared = {s.name for s in self.mcp_servers}
        for a in self.agents:
            for ref in a.mcp_servers:
                if ref not in declared:
                    raise ValueError(
                        f"agent '{a.id}' references mcp server {ref!r} which the "
                        f"workflow does not declare (declared: {sorted(declared)})"
                    )
        return self

    @classmethod
    def load(cls, path: Path) -> "Workflow":
        with Path(path).open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: workflow YAML must be a mapping at the top level")
        return cls.model_validate(data)

    @classmethod
    def find(cls, name: str, target_repo_path: Path) -> "Workflow":
        path = target_repo_path / ".agentic" / "workflows" / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"workflow '{name}' not found at {path}")
        return cls.load(path)


def list_workflows(target_repo_path: Path) -> list[str]:
    wdir = target_repo_path / ".agentic" / "workflows"
    if not wdir.exists():
        return []
    return sorted(p.stem for p in wdir.glob("*.yaml"))
