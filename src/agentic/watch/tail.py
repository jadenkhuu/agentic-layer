"""JSONL tailer for events.jsonl.

`iter_events(path)` reads everything currently in the file once.
`Tailer(path)` keeps a file position and yields only new events on each call —
used by the textual app's poll worker.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def iter_events(path: Path) -> Iterator[dict]:
    """Yield every event currently in `path`. Empty/malformed lines are skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            ev = parse_line(line)
            if ev is not None:
                yield ev


class Tailer:
    """Stateful reader. Each `read_new()` returns events since the last call."""

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0

    def read_new(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        events = []
        for line in chunk.splitlines():
            ev = parse_line(line)
            if ev is not None:
                events.append(ev)
        return events
