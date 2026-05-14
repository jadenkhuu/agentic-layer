"""CI loop tests with injected poll function (no `gh` calls)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic.ci_loop import CIPollResult, watch_and_fix
from agentic.events import EventEmitter


def _events_from(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _emitter(tmp_path: Path) -> EventEmitter:
    return EventEmitter(tmp_path / "events.jsonl")


def test_passes_on_first_poll(tmp_path: Path) -> None:
    em = _emitter(tmp_path)

    def poll(pr: int) -> CIPollResult:
        return CIPollResult(status="pass", raw="")

    def fix(out: str, attempt: int) -> bool:
        raise AssertionError("fix should not be called when CI is already green")

    ok = watch_and_fix(99, em, fix=fix, poll_fn=poll, sleep_fn=lambda s: None)
    assert ok is True
    types = [e["type"] for e in _events_from(em.path)]  # type: ignore[arg-type]
    assert "ci.poll" in types


def test_retries_then_passes(tmp_path: Path) -> None:
    em = _emitter(tmp_path)
    seq = ["fail", "pass"]
    calls: dict[str, int] = {"fix": 0}

    def poll(pr: int) -> CIPollResult:
        return CIPollResult(status=seq.pop(0), raw="boom")

    def fix(out: str, attempt: int) -> bool:
        calls["fix"] += 1
        return True

    ok = watch_and_fix(99, em, fix=fix, poll_fn=poll, sleep_fn=lambda s: None,
                       max_attempts=3)
    assert ok is True
    assert calls["fix"] == 1


def test_halts_at_max_attempts(tmp_path: Path) -> None:
    em = _emitter(tmp_path)
    calls: dict[str, int] = {"fix": 0}

    def poll(pr: int) -> CIPollResult:
        return CIPollResult(status="fail", raw="boom")

    def fix(out: str, attempt: int) -> bool:
        calls["fix"] += 1
        return True

    ok = watch_and_fix(99, em, fix=fix, poll_fn=poll, sleep_fn=lambda s: None,
                       max_attempts=2)
    assert ok is False
    assert calls["fix"] == 2


def test_pending_polls_then_settles(tmp_path: Path) -> None:
    em = _emitter(tmp_path)
    seq = ["pending", "pending", "pass"]

    def poll(pr: int) -> CIPollResult:
        return CIPollResult(status=seq.pop(0), raw="")

    def fix(out: str, attempt: int) -> bool:
        raise AssertionError("no fix needed")

    sleeps: list[float] = []
    ok = watch_and_fix(
        99, em, fix=fix, poll_fn=poll,
        sleep_fn=lambda s: sleeps.append(s),
        max_attempts=3,
    )
    assert ok is True
    # 2 pending polls -> 2 sleeps
    assert len(sleeps) == 2


def test_fix_callback_returning_false_halts(tmp_path: Path) -> None:
    em = _emitter(tmp_path)

    def poll(pr: int) -> CIPollResult:
        return CIPollResult(status="fail", raw="")

    def fix(out: str, attempt: int) -> bool:
        return False  # giving up

    ok = watch_and_fix(99, em, fix=fix, poll_fn=poll, sleep_fn=lambda s: None,
                       max_attempts=5)
    assert ok is False
