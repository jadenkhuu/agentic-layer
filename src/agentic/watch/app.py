"""Watch app — textual TUI for one run.

Loads existing events from `events.jsonl` on mount, then (if the run hasn't
finished) starts a 200ms poll worker that appends new events as they arrive.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, ListView

from agentic.watch.models import RunState
from agentic.watch.panes import AgentListPane, TranscriptPane, render_transcript_plain
from agentic.watch.tail import Tailer, iter_events

POLL_INTERVAL = 0.2


class NewEvents(Message):
    def __init__(self, events: list[dict]) -> None:
        super().__init__()
        self.events = events


class WatchApp(App):
    """Observe one workflow run."""

    CSS = """
    #main { layout: horizontal; height: 1fr; }
    Header { background: $primary; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "copy_transcript", "copy transcript"),
        Binding("up", "select_prev", "prev agent"),
        Binding("down", "select_next", "next agent"),
        Binding("r", "force_refresh", "refresh"),
    ]

    def __init__(self, events_path: Path, run_id: str) -> None:
        super().__init__()
        self.events_path = events_path
        self.run_id = run_id
        self.state = RunState(run_id=run_id)
        self._tailer = Tailer(events_path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            AgentListPane(id="agents"),
            TranscriptPane(id="transcript"),
            id="main",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self._update_title()
        # initial load
        for event in iter_events(self.events_path):
            self.state.apply(event)
        self._advance_tailer_past_current_file()
        self._refresh_ui(select_first=True)
        if not self.state.is_terminal:
            self.run_worker(self._poll_loop, exclusive=True)

    def _advance_tailer_past_current_file(self) -> None:
        # Tailer.read_new() will return only events appended after this point.
        self._tailer.read_new()

    async def _poll_loop(self) -> None:
        while not self.state.is_terminal:
            new = self._tailer.read_new()
            if new:
                self.post_message(NewEvents(new))
            await asyncio.sleep(POLL_INTERVAL)

    def on_new_events(self, message: NewEvents) -> None:
        for event in message.events:
            self.state.apply(event)
        self._refresh_ui()

    def _update_title(self) -> None:
        s = self.state
        bits = [f"run {self.run_id}"]
        if s.workflow:
            bits.append(f"workflow: {s.workflow}")
        if s.branch:
            bits.append(f"branch: {s.branch}")
        bits.append(f"status: {s.status}")
        if s.elapsed_seconds is not None:
            bits.append(f"elapsed: {s.elapsed_seconds:.1f}s")
        self.title = " · ".join(bits)

    def _refresh_ui(self, select_first: bool = False) -> None:
        self._update_title()
        agents_pane = self.query_one("#agents", AgentListPane)
        agents_pane.refresh_from(self.state)

        lv = self.query_one("#agent-list", ListView)
        if select_first and lv.index is None and self.state.agent_order:
            lv.index = 0

        self._refresh_transcript()

    def _refresh_transcript(self) -> None:
        transcript = self.query_one("#transcript", TranscriptPane)
        lv = self.query_one("#agent-list", ListView)
        if lv.index is None or not self.state.agent_order:
            transcript.show_agent(None)
            return
        if lv.index >= len(self.state.agent_order):
            transcript.show_agent(None)
            return
        agent_id = self.state.agent_order[lv.index]
        transcript.show_agent(self.state.agents[agent_id])

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        self._refresh_transcript()

    def action_force_refresh(self) -> None:
        """Re-render labels + transcript without clearing widgets (avoids
        ListView.clear's async-removal race). agent_order is append-only,
        so a full rebuild was never necessary."""
        transcript = self.query_one("#transcript", TranscriptPane)
        transcript._current_agent_id = None   # force title + transcript repaint
        transcript._rendered_count = 0
        self._refresh_ui()

    def action_select_prev(self) -> None:
        lv = self.query_one("#agent-list", ListView)
        lv.action_cursor_up()

    def action_select_next(self) -> None:
        lv = self.query_one("#agent-list", ListView)
        lv.action_cursor_down()

    def action_copy_transcript(self) -> None:
        """Copy the currently-selected agent's transcript to the clipboard."""
        lv = self.query_one("#agent-list", ListView)
        if lv.index is None or lv.index >= len(self.state.agent_order):
            self.notify("nothing to copy yet", severity="warning", timeout=2)
            return
        agent_id = self.state.agent_order[lv.index]
        agent = self.state.agents[agent_id]
        text = render_transcript_plain(agent)
        try:
            self.copy_to_clipboard(text)
        except Exception as e:  # pragma: no cover — clipboard support varies
            self.notify(f"clipboard error: {e}", severity="error", timeout=4)
            return
        line_count = text.count("\n")
        self.notify(
            f"copied {agent_id} transcript ({line_count} lines)",
            severity="information", timeout=2,
        )


def run_watch(events_path: Path, run_id: str) -> None:
    app = WatchApp(events_path=events_path, run_id=run_id)
    app.run()
