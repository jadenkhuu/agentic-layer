from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from rich.console import Console

from agentic.context import RunContext
from agentic.events import serialize_tool_input, serialize_tool_result
from agentic.mcp import resolve_for_agent
from agentic.pricing import cost_usd

logger = logging.getLogger(__name__)
console = Console()


class AgentSpec(BaseModel):
    """Declarative agent definition loaded from workflow YAML."""

    id: str
    prompt_file: str | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)
    # human-in-the-loop checkpoint — runner halts after this agent and persists
    # state so an operator can review (and `agentic resume <run-id>`).
    pause_after: bool = False
    # optional shell commands the runner executes around this agent. failures
    # halt the run with a clear error and the script's tail in events.jsonl.
    pre: str | None = None
    post: str | None = None


def run_agent(spec: AgentSpec, ctx: RunContext) -> None:
    """Execute a single agent (sync wrapper)."""
    logger.info("agent %s start", spec.id)
    start_t = time.monotonic()
    # cleared here, set by _run_stub / _run_real, consumed by the runner.
    ctx.last_agent_cost = None
    ctx.events.emit(
        "agent.start",
        agent=spec.id,
        agent_id=spec.id,
        prompt_file=spec.prompt_file,
        allowed_tools=spec.allowed_tools,
        inputs=spec.inputs,
    )

    if ctx.stub_mode:
        _run_stub(spec, ctx)
    else:
        asyncio.run(_run_real(spec, ctx))

    ctx.events.emit(
        "agent.complete",
        agent=spec.id,
        agent_id=spec.id,
        status="success",
        outputs=spec.outputs,
        elapsed_seconds=round(time.monotonic() - start_t, 3),
    )
    logger.info("agent %s done", spec.id)


# ---------------------------------------------------------------------------
# Stub path (kept for tests and `agentic run --stub`)
# ---------------------------------------------------------------------------

def _run_stub(spec: AgentSpec, ctx: RunContext) -> None:
    for name in spec.inputs:
        ctx.resolve_input(name)  # surface missing-input errors early
    # Emit an assistant.text event in stub mode if a client prefix is set,
    # so tests can verify the client config reaches each agent's prompt.
    if ctx.client_prefix:
        ctx.events.emit(
            "assistant.text",
            agent=spec.id,
            agent_id=spec.id,
            text=f"[stub prompt prefix]\n{ctx.client_prefix.strip()}",
        )
    # Resolve MCP servers (raises if an agent names one the workflow
    # didn't declare) so stub-mode tests can verify the wiring without
    # making an SDK call. Returned dict is recorded as a tool.use event.
    mcp_dict = _resolve_mcp_for_agent(spec, ctx)
    if mcp_dict:
        ctx.events.emit(
            "tool.use",
            agent=spec.id,
            agent_id=spec.id,
            tool_name="mcp.wired",
            tool_input=", ".join(sorted(mcp_dict.keys())),
        )
    for out in spec.outputs:
        path = ctx.working_dir / out
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[stub] agent {spec.id} ran with inputs: {spec.inputs}\n")
        logger.info("agent %s wrote %s", spec.id, out)

    # Stub mode performs no SDK round-trip and therefore costs nothing — but
    # still emit a zero-cost event so the cost pipeline (events.jsonl ->
    # RunState -> helm sync) is exercisable end to end without a real run.
    ctx.events.emit_cost(
        agent=spec.id,
        model="stub",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
    )
    ctx.last_agent_cost = {
        "agent": spec.id,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }


# ---------------------------------------------------------------------------
# Real Claude-Agent-SDK path
# ---------------------------------------------------------------------------

async def _run_real(spec: AgentSpec, ctx: RunContext) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )

    prompt_text = _load_prompt(spec, ctx)
    if prompt_text is None:
        raise RuntimeError(
            f"agent {spec.id}: prompt_file is required for real (non-stub) runs"
        )
    prompt_text = _substitute(prompt_text, spec, ctx)
    if ctx.client_prefix:
        prompt_text = ctx.client_prefix + prompt_text

    mcp_dict = _resolve_mcp_for_agent(spec, ctx)

    if spec.sub_agents:
        raise NotImplementedError(
            f"agent {spec.id}: sub_agents wiring not yet implemented "
            "(workflow YAML uses list[str]; SDK expects dict[str, AgentDefinition])"
        )

    options = ClaudeAgentOptions(
        allowed_tools=spec.allowed_tools,
        cwd=str(ctx.target_repo_path),
        mcp_servers=mcp_dict if mcp_dict else None,
    )

    logger.info(
        "agent %s :: query start (tools=%s, cwd=%s)",
        spec.id, spec.allowed_tools, ctx.target_repo_path,
    )
    logger.debug("agent %s :: prompt:\n%s", spec.id, prompt_text)

    last_model = ""
    async for message in query(prompt=prompt_text, options=options):
        if isinstance(message, AssistantMessage):
            last_model = getattr(message, "model", "") or last_model
            for block in message.content:
                if isinstance(block, TextBlock):
                    logger.debug("agent %s :: assistant text: %s", spec.id, block.text)
                    ctx.events.emit("assistant.text", agent=spec.id,
                                    agent_id=spec.id, text=block.text)
                elif isinstance(block, ToolUseBlock):
                    _emit_tool_use(spec.id, block, ctx)
                elif isinstance(block, ToolResultBlock):
                    logger.debug(
                        "agent %s :: tool_result id=%s is_error=%s content=%r",
                        spec.id, block.tool_use_id, block.is_error, block.content,
                    )
                    ctx.events.emit(
                        "tool.result", agent=spec.id, agent_id=spec.id,
                        tool_use_id=block.tool_use_id,
                        success=not bool(block.is_error),
                        content=serialize_tool_result(block.content),
                    )
        elif isinstance(message, ResultMessage):
            logger.info(
                "agent %s :: result subtype=%s turns=%d cost=%s is_error=%s",
                spec.id, message.subtype, message.num_turns,
                message.total_cost_usd, message.is_error,
            )
            if message.is_error:
                raise RuntimeError(
                    f"agent {spec.id}: SDK reported error "
                    f"(stop_reason={message.stop_reason})"
                )
            _record_sdk_cost(spec, ctx, message, last_model)


def _record_sdk_cost(
    spec: AgentSpec, ctx: RunContext, message: Any, model: str
) -> None:
    """Translate an SDK ResultMessage's usage into a `cost` event + a
    `ctx.last_agent_cost` hand-off for the runner to fold into RunState.

    The SDK's own `total_cost_usd` is authoritative when present; otherwise
    we estimate from the price table. Never raises — a missing/odd usage
    payload degrades to a zero-cost event.
    """
    usage = getattr(message, "usage", None) or {}
    try:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        input_tokens = output_tokens = cache_read = cache_creation = 0

    model = model or "unknown"
    sdk_cost = getattr(message, "total_cost_usd", None)
    cost = (
        float(sdk_cost)
        if sdk_cost is not None
        else cost_usd(model, input_tokens, output_tokens, cache_read, cache_creation)
    )

    ctx.events.emit_cost(
        agent=spec.id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        cost_usd=cost,
    )
    ctx.last_agent_cost = {
        "agent": spec.id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
    }


def _resolve_mcp_for_agent(spec: AgentSpec, ctx: RunContext) -> dict[str, Any]:
    """Look up the workflow's MCP server specs (stashed on ctx by the runner)
    and build the SDK dict for the names this agent opted into.
    """
    if not spec.mcp_servers:
        return {}
    workflow_servers = getattr(ctx, "workflow_mcp_servers", None) or []
    return resolve_for_agent(spec.mcp_servers, workflow_servers)


def _emit_tool_use(agent_id: str, block: Any, ctx: RunContext) -> None:
    """One concise line per tool use, prefixed with agent id."""
    summary = _summarise_tool_call(block.name, block.input)
    console.print(f"[cyan]{agent_id}[/cyan]: {summary}")
    logger.debug(
        "agent %s :: tool_use name=%s id=%s input=%r",
        agent_id, block.name, block.id, block.input,
    )
    ctx.events.emit(
        "tool.use", agent=agent_id, agent_id=agent_id,
        tool_name=block.name,
        tool_input=serialize_tool_input(block.input),
    )


def _summarise_tool_call(name: str, params: dict[str, Any]) -> str:
    """Render a one-liner like 'Read README.md' or 'Edit src/foo.py'."""
    if name in {"Read", "Glob", "Grep"}:
        target = params.get("file_path") or params.get("pattern") or ""
        return f"{name} {target}".rstrip()
    if name in {"Write", "Edit"}:
        return f"{name} {params.get('file_path', '')}".rstrip()
    if name == "Bash":
        cmd = params.get("command", "")
        first = cmd.splitlines()[0] if cmd else ""
        if len(first) > 80:
            first = first[:77] + "..."
        return f"Bash $ {first}"
    return f"{name} {params!r}"


# ---------------------------------------------------------------------------
# Prompt loading + tag substitution
# ---------------------------------------------------------------------------

def _load_prompt(spec: AgentSpec, ctx: RunContext) -> str | None:
    if not spec.prompt_file:
        return None
    candidates = [
        ctx.target_repo_path / ".agentic" / spec.prompt_file,
        ctx.target_repo_path / spec.prompt_file,
        Path(spec.prompt_file),
    ]
    for c in candidates:
        if c.exists():
            return c.read_text()
    raise FileNotFoundError(
        f"agent {spec.id}: prompt_file {spec.prompt_file} not found (looked in "
        f"{[str(c) for c in candidates]})"
    )


def _tag(name: str) -> str:
    return "{{" + name.upper().replace(".", "_").replace("-", "_") + "}}"


def _substitute(prompt: str, spec: AgentSpec, ctx: RunContext) -> str:
    """Substitute {{TAG}} placeholders.

    - First declared input: substituted as file contents (or raw value for kv inputs)
    - Subsequent inputs: substituted as absolute paths
    - {{OUTPUT}}: absolute path to the (single) declared output
    - {{OUTPUT_<NAME>}}: absolute path to each declared output
    - {{WORKING_DIR}}, {{TARGET_REPO}}: convenience paths
    """
    out = prompt

    for i, name in enumerate(spec.inputs):
        tag = _tag(name)
        if i == 0:
            value = ctx.resolve_input(name)
        else:
            value = str((ctx.working_dir / name).resolve())
        out = out.replace(tag, value)

    if len(spec.outputs) == 1:
        out = out.replace("{{OUTPUT}}", str((ctx.working_dir / spec.outputs[0]).resolve()))
    for fname in spec.outputs:
        out = out.replace(_tag("output_" + fname), str((ctx.working_dir / fname).resolve()))

    out = out.replace("{{WORKING_DIR}}", str(ctx.working_dir.resolve()))
    out = out.replace("{{TARGET_REPO}}", str(ctx.target_repo_path.resolve()))
    return out
