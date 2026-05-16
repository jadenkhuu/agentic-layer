"""Run archival — tarball stale run dirs into `.agentic/runs/_archive/`.

`agentic archive --older-than 30d` walks `.agentic/runs/`, finds every run
directory older than the threshold, compresses each into a zstd tarball at
`.agentic/runs/_archive/<run-id>.tar.zst`, then collapses the original run
dir down to a stub `state.json` (the run's status is preserved and an
`archived_at` timestamp is added). The full run is always recoverable from
the tarball — archival is lossless, just compact.

zstd compression shells out to the `zstd` CLI: Python's stdlib `tarfile`
gained a zstd codec only in 3.14, and the project pins no `zstandard`
dependency, so the CLI is the portable path.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Subdirectory of `.agentic/runs/` that holds the compressed tarballs.
ARCHIVE_DIRNAME = "_archive"

# run id prefix: 20260516-180312-<hex8>
_RUN_ID_TS = re.compile(r"^(\d{8})-(\d{6})-")
# --older-than value: an integer followed by a unit (s/m/h/d/w).
_DURATION = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


class ArchiveError(RuntimeError):
    """Unrecoverable archive failure (bad input, missing zstd, clobbered file)."""


def parse_duration(text: str) -> timedelta:
    """Parse an `--older-than` value like ``30d``, ``12h`` or ``2w``.

    Units: ``s`` seconds, ``m`` minutes, ``h`` hours, ``d`` days, ``w`` weeks.
    """
    m = _DURATION.match(text or "")
    if not m:
        raise ArchiveError(
            f"invalid duration {text!r}; expected e.g. 30d, 12h, 2w (units s/m/h/d/w)"
        )
    return int(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]


def run_id_timestamp(run_id: str) -> datetime | None:
    """Recover the UTC start time encoded in a run id's `YYYYMMDD-HHMMSS-` prefix."""
    m = _RUN_ID_TS.match(run_id)
    if not m:
        return None
    try:
        return datetime.strptime(
            m.group(1) + m.group(2), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def run_age(run_dir: Path, now: datetime) -> timedelta:
    """Age of a run, from its run-id timestamp (falling back to dir mtime)."""
    ts = run_id_timestamp(run_dir.name)
    if ts is None:
        ts = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
    return now - ts


def is_archived(run_dir: Path) -> bool:
    """True if this run dir has already been collapsed to an archive stub."""
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return False
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("archived_at"))


def dir_size(path: Path) -> int:
    """Total size in bytes of every file under `path`."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@dataclass(frozen=True)
class ArchiveCandidate:
    """A run dir eligible for archival."""

    run_id: str
    path: Path
    age: timedelta
    size_bytes: int


def find_archivable(
    runs_dir: Path,
    older_than: timedelta,
    now: datetime | None = None,
) -> list[ArchiveCandidate]:
    """Return run dirs older than `older_than` that are not already archived.

    The `_archive/` directory itself is skipped, oldest run first.
    """
    now = now or datetime.now(timezone.utc)
    if not runs_dir.exists():
        return []
    out: list[ArchiveCandidate] = []
    for d in runs_dir.iterdir():
        if not d.is_dir() or d.name == ARCHIVE_DIRNAME:
            continue
        if is_archived(d):
            continue
        age = run_age(d, now)
        if age >= older_than:
            out.append(ArchiveCandidate(d.name, d, age, dir_size(d)))
    out.sort(key=lambda c: c.run_id)
    return out


def _ensure_zstd() -> str:
    exe = shutil.which("zstd")
    if exe is None:
        raise ArchiveError(
            "the `zstd` CLI is required to write .tar.zst archives but was not "
            "found on PATH — install it (e.g. `brew install zstd`) and retry"
        )
    return exe


def _collapse_to_stub(run_dir: Path, now: datetime) -> None:
    """Delete every artifact in `run_dir`, leaving only a stub `state.json`.

    The stub keeps whatever the prior state.json held (run id, status, …) and
    adds an `archived_at` timestamp. Safe to call only once the tarball that
    holds the full run has been written.
    """
    state_path = run_dir / "state.json"
    stub: dict[str, object]
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            stub = loaded if isinstance(loaded, dict) else {"run_id": run_dir.name}
        except (OSError, json.JSONDecodeError):
            stub = {"run_id": run_dir.name}
    else:
        stub = {"run_id": run_dir.name}
    stub["archived_at"] = now.isoformat()

    for child in run_dir.iterdir():
        if child.name == "state.json":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    state_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")


def archive_run(
    run_dir: Path,
    archive_dir: Path,
    now: datetime | None = None,
) -> Path:
    """Tarball + zstd-compress one run dir, then collapse it to a stub.

    Returns the path of the written ``<run-id>.tar.zst``. Raises
    `ArchiveError` if zstd is unavailable or the archive already exists.
    """
    now = now or datetime.now(timezone.utc)
    zstd = _ensure_zstd()
    run_id = run_dir.name
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_id}.tar.zst"
    if dest.exists():
        raise ArchiveError(f"archive already exists, refusing to clobber: {dest}")

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / f"{run_id}.tar"
        with tarfile.open(tar_path, "w") as tar:
            tar.add(run_dir, arcname=run_id)
        proc = subprocess.run(
            [zstd, "-q", "-T0", str(tar_path), "-o", str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise ArchiveError(f"zstd compression failed: {proc.stderr.strip()}")

    _collapse_to_stub(run_dir, now)
    return dest


def humanize_age(age: timedelta) -> str:
    """Compact age label: ``45d``, ``6h``, ``12m``."""
    if age.days >= 1:
        return f"{age.days}d"
    hours = age.seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return f"{age.seconds // 60}m"


def humanize_size(num_bytes: int) -> str:
    """Compact byte-size label: ``512B``, ``3.4KB``, ``1.2MB``."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
