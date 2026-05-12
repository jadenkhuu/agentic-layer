import json
from pathlib import Path

from agentic.events import (
    EventEmitter,
    serialize_tool_input,
    serialize_tool_result,
    truncate,
)


def _read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_emit_writes_jsonl_line_per_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    em = EventEmitter(path)
    em.emit("run.start", workflow="x", agent_count=2)
    em.emit("agent.start", agent="a", agent_id="a", inputs=["task"])

    events = _read(path)
    assert len(events) == 2
    assert events[0]["type"] == "run.start"
    assert events[0]["agent"] is None
    assert events[0]["payload"] == {"workflow": "x", "agent_count": 2}
    assert events[1]["type"] == "agent.start"
    assert events[1]["agent"] == "a"
    assert events[1]["payload"]["inputs"] == ["task"]


def test_emit_preserves_order(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    em = EventEmitter(path)
    for i in range(10):
        em.emit("tick", n=i)
    events = _read(path)
    assert [e["payload"]["n"] for e in events] == list(range(10))


def test_emit_includes_iso_timestamp(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    em = EventEmitter(path)
    em.emit("x")
    ev = _read(path)[0]
    assert ev["ts"].endswith("Z")
    assert "T" in ev["ts"]


def test_emit_with_none_path_is_noop(tmp_path: Path):
    em = EventEmitter(None)
    em.emit("anything", foo="bar")  # must not raise


def test_emit_swallows_write_errors(tmp_path: Path, caplog):
    # Point at a path whose parent we can create but then make read-only,
    # so the open-for-append fails.
    bad_dir = tmp_path / "ro"
    bad_dir.mkdir()
    path = bad_dir / "events.jsonl"
    em = EventEmitter(path)
    # Make the directory read-only so append fails.
    bad_dir.chmod(0o500)
    try:
        em.emit("test")  # must not raise
    finally:
        bad_dir.chmod(0o700)  # restore so cleanup works


def test_truncate():
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdefghij", 10) == "abcdefghij"
    # 11 chars + limit 10 → keep first 7 + "..." for a final length of 10.
    assert truncate("abcdefghijk", 10) == "abcdefg..."
    assert len(truncate("x" * 1000, 500)) == 500


def test_serialize_tool_input_compact_dict():
    s = serialize_tool_input({"file_path": "/tmp/foo.py"})
    assert "file_path" in s
    assert s.startswith("{")


def test_serialize_tool_input_long_truncated():
    big = {"command": "x" * 1000}
    s = serialize_tool_input(big)
    assert len(s) <= 500
    assert s.endswith("...")


def test_serialize_tool_result_string_passthrough():
    assert serialize_tool_result("ok") == "ok"


def test_serialize_tool_result_truncation():
    s = serialize_tool_result("y" * 2000)
    assert len(s) <= 1000


def test_serialize_tool_result_none():
    assert serialize_tool_result(None) == ""
