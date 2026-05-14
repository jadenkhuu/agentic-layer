"""Run state persistence — <run-dir>/state.json.

Captures what the resume command needs to pick up after a pause: the
workflow name, the index of the next agent to run, branch, client, and
the inputs the original run was started with. Read with `RunState.load`,
written incrementally by the runner.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["running", "paused", "succeeded", "failed", "aborted"]


class RunState(BaseModel):
    run_id: str
    workflow_name: str
    target_repo_path: str
    status: RunStatus = "running"
    # index of the *next* agent to run. equals len(workflow.agents) when done.
    current_agent_index: int = 0
    branch: str | None = None
    base_branch: str | None = None
    client: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    pr_url: str | None = None
    pr_number: int | None = None
    fix_attempts: int = 0
    completed_agents: list[str] = Field(default_factory=list)
    stub_mode: bool = False

    @classmethod
    def path_for(cls, run_dir: Path) -> Path:
        return run_dir / "state.json"

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path_for(run_dir).write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        return cls.model_validate_json(cls.path_for(run_dir).read_text(encoding="utf-8"))
