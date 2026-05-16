"""`agentic archive` — duration parsing, candidate discovery, tarballing, CLI."""
import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentic.archive import (
    ARCHIVE_DIRNAME,
    ArchiveError,
    archive_run,
    find_archivable,
    humanize_age,
    humanize_size,
    is_archived,
    parse_duration,
    run_age,
    run_id_timestamp,
)
from agentic.cli import main
from agentic.state import RunState

# fixed "now" so age maths is deterministic
NOW = datetime(2026, 5, 16, 18, 0, 0, tzinfo=timezone.utc)
OLD_RUN = "20260401-120000-aaaa1111"   # 45 days before NOW
RECENT_RUN = "20260515-120000-bbbb2222"  # ~1 day before NOW


def _make_run(runs_dir: Path, run_id: str, *, with_state: bool = True,
              archived: bool = False) -> Path:
    """Create a fake run dir with an events.jsonl + a couple of agent docs."""
    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text(
        '{"ts": "2026-04-01T12:00:00Z", "type": "run.start", "agent": null, "payload": {}}\n'
    )
    (d / "run.log").write_text("INFO run started\nINFO run complete\n")
    (d / "SPEC.md").write_text("# spec\nbuild the thing\n")
    if with_state:
        state = {
            "run_id": run_id,
            "workflow_name": "feature",
            "target_repo_path": str(runs_dir.parent.parent),
            "status": "succeeded",
        }
        if archived:
            state["archived_at"] = "2026-05-10T00:00:00+00:00"
        (d / "state.json").write_text(json.dumps(state, indent=2))
    return d


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".agentic" / "runs"
    d.mkdir(parents=True)
    return d


# --------------------------------------------------------------------------
# duration parsing
# --------------------------------------------------------------------------

def test_parse_duration_units():
    assert parse_duration("30d") == timedelta(days=30)
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("2w") == timedelta(weeks=2)
    assert parse_duration("90s") == timedelta(seconds=90)
    assert parse_duration("15m") == timedelta(minutes=15)


def test_parse_duration_is_case_insensitive_and_trims():
    assert parse_duration(" 7D ") == timedelta(days=7)


@pytest.mark.parametrize("bad", ["", "30", "d", "30days", "-5d", "abc"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ArchiveError):
        parse_duration(bad)


# --------------------------------------------------------------------------
# run-id timestamp + age
# --------------------------------------------------------------------------

def test_run_id_timestamp_parses_prefix():
    ts = run_id_timestamp(OLD_RUN)
    assert ts == datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_run_id_timestamp_returns_none_for_unprefixed():
    assert run_id_timestamp("not-a-timestamp") is None


def test_run_age_from_run_id(runs_dir: Path):
    d = _make_run(runs_dir, OLD_RUN)
    assert run_age(d, NOW) == timedelta(days=45, hours=6)


# --------------------------------------------------------------------------
# candidate discovery
# --------------------------------------------------------------------------

def test_find_archivable_picks_only_old_runs(runs_dir: Path):
    _make_run(runs_dir, OLD_RUN)
    _make_run(runs_dir, RECENT_RUN)
    found = find_archivable(runs_dir, timedelta(days=30), NOW)
    assert [c.run_id for c in found] == [OLD_RUN]
    assert found[0].size_bytes > 0


def test_find_archivable_skips_archive_dir(runs_dir: Path):
    (runs_dir / ARCHIVE_DIRNAME).mkdir()
    _make_run(runs_dir, OLD_RUN)
    found = find_archivable(runs_dir, timedelta(days=30), NOW)
    assert [c.run_id for c in found] == [OLD_RUN]


def test_find_archivable_skips_already_archived(runs_dir: Path):
    _make_run(runs_dir, OLD_RUN, archived=True)
    assert find_archivable(runs_dir, timedelta(days=30), NOW) == []


def test_find_archivable_empty_when_no_runs_dir(tmp_path: Path):
    assert find_archivable(tmp_path / "nope", timedelta(days=1), NOW) == []


def test_is_archived(runs_dir: Path):
    plain = _make_run(runs_dir, RECENT_RUN)
    archived = _make_run(runs_dir, OLD_RUN, archived=True)
    assert not is_archived(plain)
    assert is_archived(archived)


# --------------------------------------------------------------------------
# archive_run
# --------------------------------------------------------------------------

def test_archive_run_creates_tarball(runs_dir: Path):
    d = _make_run(runs_dir, OLD_RUN)
    dest = archive_run(d, runs_dir / ARCHIVE_DIRNAME, NOW)
    assert dest == runs_dir / ARCHIVE_DIRNAME / f"{OLD_RUN}.tar.zst"
    assert dest.exists() and dest.stat().st_size > 0
    # zstd integrity check passes
    assert subprocess.run(["zstd", "-t", str(dest)]).returncode == 0


def test_archive_run_collapses_to_stub(runs_dir: Path):
    d = _make_run(runs_dir, OLD_RUN)
    archive_run(d, runs_dir / ARCHIVE_DIRNAME, NOW)
    # only state.json survives in the run dir
    assert sorted(p.name for p in d.iterdir()) == ["state.json"]
    stub = json.loads((d / "state.json").read_text())
    assert stub["archived_at"] == NOW.isoformat()
    assert stub["status"] == "succeeded"  # original state preserved
    assert is_archived(d)


def test_archive_run_tarball_roundtrips(runs_dir: Path, tmp_path: Path):
    d = _make_run(runs_dir, OLD_RUN)
    dest = archive_run(d, runs_dir / ARCHIVE_DIRNAME, NOW)
    out = tmp_path / "extracted"
    out.mkdir()
    tar_path = out / "run.tar"
    subprocess.run(["zstd", "-q", "-d", str(dest), "-o", str(tar_path)], check=True)
    with tarfile.open(tar_path) as tar:
        tar.extractall(out, filter="data")
    restored = out / OLD_RUN
    assert (restored / "events.jsonl").exists()
    assert (restored / "SPEC.md").read_text() == "# spec\nbuild the thing\n"


def test_archive_run_refuses_to_clobber(runs_dir: Path):
    d = _make_run(runs_dir, OLD_RUN)
    archive_dir = runs_dir / ARCHIVE_DIRNAME
    archive_run(d, archive_dir, NOW)
    # the run dir survives as a stub; a second pass finds the .tar.zst present
    with pytest.raises(ArchiveError, match="already exists"):
        archive_run(d, archive_dir, NOW)


def test_archive_run_handles_missing_state(runs_dir: Path):
    d = _make_run(runs_dir, OLD_RUN, with_state=False)
    archive_run(d, runs_dir / ARCHIVE_DIRNAME, NOW)
    stub = json.loads((d / "state.json").read_text())
    assert stub == {"run_id": OLD_RUN, "archived_at": NOW.isoformat()}


# --------------------------------------------------------------------------
# humanizers
# --------------------------------------------------------------------------

def test_humanize_age():
    assert humanize_age(timedelta(days=45, hours=6)) == "45d"
    assert humanize_age(timedelta(hours=6)) == "6h"
    assert humanize_age(timedelta(minutes=12)) == "12m"


def test_humanize_size():
    assert humanize_size(512) == "512B"
    assert humanize_size(2048) == "2.0KB"
    assert humanize_size(5 * 1024 * 1024) == "5.0MB"


# --------------------------------------------------------------------------
# RunState.archived_at
# --------------------------------------------------------------------------

def test_runstate_archived_at_defaults_none_and_roundtrips(tmp_path: Path):
    st = RunState(run_id="r1", workflow_name="feature", target_repo_path=str(tmp_path))
    assert st.archived_at is None
    st.archived_at = NOW.isoformat()
    st.save(tmp_path)
    assert RunState.load(tmp_path).archived_at == NOW.isoformat()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_dry_run_lists_without_writing(runs_dir: Path, monkeypatch):
    old = _make_run(runs_dir, OLD_RUN)
    _make_run(runs_dir, RECENT_RUN)
    monkeypatch.chdir(runs_dir.parent.parent)
    result = CliRunner().invoke(main, ["archive", "--older-than", "30d", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert OLD_RUN in result.output
    assert RECENT_RUN not in result.output
    assert "dry run" in result.output
    # nothing written, nothing collapsed
    assert not (runs_dir / ARCHIVE_DIRNAME).exists()
    assert (old / "events.jsonl").exists()


def test_cli_archives_old_runs(runs_dir: Path, monkeypatch):
    old = _make_run(runs_dir, OLD_RUN)
    recent = _make_run(runs_dir, RECENT_RUN)
    monkeypatch.chdir(runs_dir.parent.parent)
    result = CliRunner().invoke(main, ["archive", "--older-than", "30d"])
    assert result.exit_code == 0, result.output
    assert (runs_dir / ARCHIVE_DIRNAME / f"{OLD_RUN}.tar.zst").exists()
    assert sorted(p.name for p in old.iterdir()) == ["state.json"]
    # the recent run is untouched
    assert (recent / "events.jsonl").exists()


def test_cli_nothing_to_archive(runs_dir: Path, monkeypatch):
    _make_run(runs_dir, RECENT_RUN)
    monkeypatch.chdir(runs_dir.parent.parent)
    result = CliRunner().invoke(main, ["archive", "--older-than", "30d"])
    assert result.exit_code == 0, result.output
    assert "nothing to archive" in result.output


def test_cli_rejects_bad_duration(runs_dir: Path, monkeypatch):
    _make_run(runs_dir, OLD_RUN)
    monkeypatch.chdir(runs_dir.parent.parent)
    result = CliRunner().invoke(main, ["archive", "--older-than", "garbage"])
    assert result.exit_code == 2
    assert "invalid duration" in result.output


def test_cli_errors_when_no_runs_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["archive", "--older-than", "30d"])
    assert result.exit_code == 2
    assert "no runs directory" in result.output
