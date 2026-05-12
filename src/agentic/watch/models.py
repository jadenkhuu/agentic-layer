"""Parsed event types and aggregate run state.

`RunState.apply(event)` is the single place state changes — both the static
load path and the live tailing path go through it, so the UI never sees
inconsistent state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentStatus = Literal["pending", "running", "success", "failed"]
RunStatus = Literal["pending", "running", "success", "failed"]


@dataclass
class TranscriptEntry:
    ts: str
    kind: Literal["text", "tool_use", "tool_result"]
    text: str = ""                 # for kind=text or tool_result content
    tool_name: str = ""            # for kind in {tool_use, tool_result}
    tool_input: str = ""           # for kind=tool_use
    success: bool = True           # for kind=tool_result


@dataclass
class AgentState:
    id: str
    status: AgentStatus = "pending"
    prompt_file: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    started_ts: str | None = None
    finished_ts: str | None = None
    elapsed_seconds: float | None = None
    error: str | None = None
    transcript: list[TranscriptEntry] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    workflow: str = ""
    branch: str | None = None
    target_repo: str = ""
    stub_mode: bool = False
    started_ts: str | None = None
    finished_ts: str | None = None
    status: RunStatus = "pending"
    failed_agent: str | None = None
    elapsed_seconds: float | None = None
    agents: dict[str, AgentState] = field(default_factory=dict)
    agent_order: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("success", "failed")

    def _agent(self, agent_id: str) -> AgentState:
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentState(id=agent_id)
            self.agent_order.append(agent_id)
        return self.agents[agent_id]

    def apply(self, event: dict[str, Any]) -> None:
        t = event.get("type")
        p = event.get("payload", {})
        ts = event.get("ts", "")
        aid = event.get("agent")

        if t == "run.start":
            self.workflow = p.get("workflow", "")
            self.branch = p.get("branch")
            self.target_repo = p.get("target_repo", "")
            self.stub_mode = bool(p.get("stub_mode", False))
            self.started_ts = ts
            self.status = "running"

        elif t == "run.complete":
            self.status = p.get("status", "success")
            self.failed_agent = p.get("failed_agent")
            self.elapsed_seconds = p.get("elapsed_seconds")
            self.finished_ts = ts

        elif t == "agent.start":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.status = "running"
            a.prompt_file = p.get("prompt_file")
            a.allowed_tools = list(p.get("allowed_tools", []))
            a.inputs = list(p.get("inputs", []))
            a.started_ts = ts

        elif t == "agent.complete":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.status = p.get("status", "success")
            a.outputs = list(p.get("outputs", []))
            a.elapsed_seconds = p.get("elapsed_seconds")
            a.finished_ts = ts

        elif t == "agent.fail":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.status = "failed"
            a.error = p.get("error")
            a.finished_ts = ts

        elif t == "assistant.text":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.transcript.append(TranscriptEntry(
                ts=ts, kind="text", text=p.get("text", ""),
            ))

        elif t == "tool.use":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.transcript.append(TranscriptEntry(
                ts=ts, kind="tool_use",
                tool_name=p.get("tool_name", ""),
                tool_input=p.get("tool_input", ""),
            ))

        elif t == "tool.result":
            a = self._agent(p.get("agent_id") or aid or "?")
            a.transcript.append(TranscriptEntry(
                ts=ts, kind="tool_result",
                text=p.get("content", ""),
                success=bool(p.get("success", True)),
            ))
        # unknown event types are silently ignored — forward compatible
