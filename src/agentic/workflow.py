from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from agentic.agent import AgentSpec


class Workflow(BaseModel):
    name: str
    description: str = ""
    agents: list[AgentSpec] = Field(default_factory=list)

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
