from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentic.events import EventEmitter
from agentic.mcp import MCPServerSpec


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


class RunContext(BaseModel):
    """Per-run state. No globals — pass this everywhere."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str = Field(default_factory=_new_run_id)
    workflow_name: str
    target_repo_path: Path
    working_dir: Path
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    inputs: dict[str, Any] = Field(default_factory=dict)
    stub_mode: bool = False
    branch: str | None = None
    base_branch: str | None = None  # branch HEAD was on before the agentic branch was created
    events: EventEmitter = Field(default_factory=lambda: EventEmitter(None))
    # populated by the runner before agents fire — agent.py looks here to
    # resolve the MCP servers an agent opted into.
    workflow_mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    # populated when --client is in effect; agent.py prepends this string
    # (and the runner echoes the name into events for the UI).
    client_name: str | None = None
    client_prefix: str = ""
    # populated by `agentic run --context-file` — helm injects the project
    # briefing + studio patterns here. agent.py prepends it ahead of
    # client_prefix in every agent's effective system prompt.
    helm_context: str = ""
    # transient hand-off: the cost of the agent that just ran. agent.py
    # sets it, the runner folds it into RunState then clears it. Never
    # persisted — RunState carries the durable aggregates.
    last_agent_cost: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        workflow_name: str,
        target_repo_path: Path,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
        stub_mode: bool = False,
    ) -> "RunContext":
        rid = run_id or _new_run_id()
        wdir = target_repo_path / ".agentic" / "runs" / rid
        wdir.mkdir(parents=True, exist_ok=True)
        return cls(
            run_id=rid,
            workflow_name=workflow_name,
            target_repo_path=target_repo_path,
            working_dir=wdir,
            inputs=inputs or {},
            stub_mode=stub_mode,
        )

    @property
    def short_id(self) -> str:
        """Last hex chunk of run_id, suitable for branch suffixes."""
        return self.run_id.rsplit("-", 1)[-1]

    def resolve_input(self, name: str) -> str:
        """An input name is either a key in the kv store or a filename in working_dir."""
        if name in self.inputs:
            return str(self.inputs[name])
        path = self.working_dir / name
        if path.exists():
            return path.read_text()
        raise KeyError(
            f"input '{name}' not found in run context (kv keys: {list(self.inputs)}) "
            f"and no file at {path}"
        )
