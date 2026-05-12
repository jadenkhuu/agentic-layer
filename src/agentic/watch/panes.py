"""TUI panes: agent list (left) and transcript (right)."""
from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Label, ListItem, ListView, RichLog

from agentic.watch.models import AgentState, RunState, TranscriptEntry


_STATUS_ICON = {
    "pending": " ",
    "running": "►",
    "success": "✓",
    "failed":  "✗",
}
_STATUS_STYLE = {
    "pending": "dim",
    "running": "yellow",
    "success": "green",
    "failed":  "red",
}


def _fmt_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "  —  "
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def render_agent_row(a: AgentState) -> str:
    """One row text for the agent list (rich markup)."""
    icon = _STATUS_ICON.get(a.status, " ")
    style = _STATUS_STYLE.get(a.status, "white")
    return f"[{style}]{icon}[/] {a.id:<12} {_fmt_elapsed(a.elapsed_seconds)}"


class AgentListPane(Vertical):
    """Left pane: ordered list of agents with status + elapsed."""

    DEFAULT_CSS = """
    AgentListPane {
        width: 32;
        border: solid $accent;
        padding: 0 1;
    }
    AgentListPane > Label { color: $accent; text-style: bold; }
    AgentListPane > ListView { height: 1fr; }
    """

    def compose(self):
        yield Label("Agents")
        yield ListView(id="agent-list")

    def refresh_from(self, run: RunState) -> None:
        """Incrementally sync the ListView to `run.agent_order`.

        `RunState.agent_order` is append-only during a run, so we only ever
        need to append new rows and update existing ones — never clear.
        Avoids ListView.clear()'s async-removal race that causes DuplicateIds.
        """
        lv: ListView = self.query_one("#agent-list", ListView)

        # 1. Append rows for any agents we haven't rendered yet.
        for idx in range(len(lv.children), len(run.agent_order)):
            aid = run.agent_order[idx]
            a = run.agents[aid]
            lv.append(ListItem(Label(render_agent_row(a))))

        # 2. Update labels on rows that already exist (status / elapsed changes).
        for idx, aid in enumerate(run.agent_order):
            if idx >= len(lv.children):
                break
            a = run.agents[aid]
            try:
                label = lv.children[idx].query_one(Label)
                label.update(render_agent_row(a))
            except Exception:
                # Row exists but its label query failed (mid-mount); next poll will fix it.
                pass

        # 3. Pick a sensible selection on first population.
        if lv.index is None and run.agent_order:
            lv.index = 0


class TranscriptPane(Vertical):
    """Right pane: chronological transcript for the selected agent."""

    DEFAULT_CSS = """
    TranscriptPane {
        width: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    TranscriptPane > Label { color: $accent; text-style: bold; }
    TranscriptPane > RichLog { height: 1fr; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_agent_id: str | None = None
        self._rendered_count = 0
        self._title = Label("Transcript")

    def compose(self):
        yield self._title
        yield RichLog(id="transcript", wrap=True, markup=True, highlight=False)

    def show_agent(self, agent: AgentState | None) -> None:
        log: RichLog = self.query_one("#transcript", RichLog)
        if agent is None:
            self._title.update("Transcript")
            log.clear()
            self._current_agent_id = None
            self._rendered_count = 0
            return

        if agent.id != self._current_agent_id:
            self._title.update(f"Transcript: {agent.id}")
            log.clear()
            self._current_agent_id = agent.id
            self._rendered_count = 0

        # append only new entries (incremental update)
        for entry in agent.transcript[self._rendered_count:]:
            log.write(_render_entry(entry))
        self._rendered_count = len(agent.transcript)

        if agent.status == "failed" and agent.error:
            log.write(f"[red]✗ agent failed:[/] {agent.error}")


def _render_entry(e: TranscriptEntry) -> str:
    if e.kind == "text":
        return f"[blue]assistant[/] {e.text}"
    if e.kind == "tool_use":
        inp = e.tool_input.replace("\n", " ")
        return f"[yellow]tool: {e.tool_name}[/] {inp}"
    if e.kind == "tool_result":
        tag = "[green]result: ok[/]" if e.success else "[red]result: err[/]"
        return f"{tag} {e.text}"
    return e.text  # pragma: no cover — unknown entry kind


def render_transcript_plain(agent: AgentState) -> str:
    """Plain-text rendering of an agent's transcript, suitable for the clipboard.

    No rich markup. Bracketed section headers so a reader can scan for
    [assistant] / [tool] / [result] blocks.
    """
    bits = [agent.status]
    if agent.elapsed_seconds is not None:
        bits.append(f"{agent.elapsed_seconds:.3f}s")
    lines = [f"=== agent: {agent.id} ({', '.join(bits)}) ===", ""]

    for e in agent.transcript:
        if e.kind == "text":
            lines.append("[assistant]")
            lines.append(e.text)
        elif e.kind == "tool_use":
            lines.append(f"[tool: {e.tool_name}]")
            lines.append(e.tool_input)
        elif e.kind == "tool_result":
            lines.append(f"[result: {'ok' if e.success else 'err'}]")
            lines.append(e.text)
        lines.append("")

    if agent.status == "failed" and agent.error:
        lines.append("[error]")
        lines.append(agent.error)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
