"""Pre/post shell-script execution with capture and halt-on-failure.

Two phases at the workflow level:
  - pre_run:  runs once before any agent
  - post_run: runs once after the last agent (only on success)

Two phases at the agent level:
  - pre:  before this agent
  - post: after this agent (only if the agent succeeded)

A failing script halts the run. Stdout/stderr (last 40 lines) are
captured into events.jsonl via script.start/script.end so the watcher
shows what blew up. Full output goes to <run-dir>/scripts/<phase>[-i].log.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ScriptPhase = Literal["pre_run", "post_run", "pre", "post"]


@dataclass
class ScriptResult:
    phase: ScriptPhase
    cmd: str
    exit_code: int
    stdout: str
    stderr: str

    def ok(self) -> bool:
        return self.exit_code == 0

    def stdout_tail(self, n: int = 40) -> str:
        return "\n".join(self.stdout.splitlines()[-n:])

    def stderr_tail(self, n: int = 40) -> str:
        return "\n".join(self.stderr.splitlines()[-n:])


def run_script(
    cmd: str,
    phase: ScriptPhase,
    cwd: Path,
    log_dir: Path,
    agent_index: int | None = None,
    timeout: float | None = None,
) -> ScriptResult:
    log_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{agent_index}" if agent_index is not None else ""
    log_file = log_dir / f"{phase}{suffix}.log"
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    log_file.write_text(
        f"$ {cmd}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
        f"--- exit: {proc.returncode} ---\n",
        encoding="utf-8",
    )
    return ScriptResult(
        phase=phase,
        cmd=cmd,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


class ScriptFailure(RuntimeError):
    def __init__(self, result: ScriptResult) -> None:
        super().__init__(
            f"script halted run in {result.phase}: {result.cmd!r} "
            f"exit={result.exit_code}\n--- stderr tail ---\n{result.stderr_tail()}"
        )
        self.result = result
