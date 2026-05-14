"""CI-failure watch+fix loop. Polls `gh pr checks`, invokes a fix
callback on failure, capped by max_attempts.

The poll function is injectable for tests (so we don't shell out to `gh`
in CI). The default `gh_pr_checks` shells out and classifies the result
defensively: failure on non-zero exit, treat "no checks reported" as a
pass (we don't want to block when a repo has no CI configured).
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from agentic.events import EventEmitter


@dataclass
class CIPollResult:
    status: str  # "pass" | "fail" | "pending"
    raw: str


def gh_pr_checks(pr_number: int) -> CIPollResult:
    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "--json", "bucket,name,state"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return CIPollResult(status="fail", raw="gh CLI not installed")
    except subprocess.TimeoutExpired:
        return CIPollResult(status="pending", raw="gh pr checks timed out")

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        if "no checks reported" in raw.lower():
            return CIPollResult(status="pass", raw=raw)
        return CIPollResult(status="fail", raw=raw)
    try:
        checks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return CIPollResult(status="fail", raw=raw)
    if not checks:
        return CIPollResult(status="pass", raw=raw)
    states = {c.get("bucket") or c.get("state") for c in checks}
    if "fail" in states:
        return CIPollResult(status="fail", raw=raw)
    if "pending" in states:
        return CIPollResult(status="pending", raw=raw)
    return CIPollResult(status="pass", raw=raw)


def watch_and_fix(
    pr_number: int,
    events: EventEmitter,
    fix: Callable[[str, int], bool],
    max_attempts: int = 3,
    poll_fn: Callable[[int], CIPollResult] = gh_pr_checks,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = 15.0,
    max_polls_per_attempt: int = 30,
) -> bool:
    """Returns True if checks ended green within max_attempts, else False.

    Each attempt polls until the result is pass or fail (or until
    max_polls_per_attempt is exhausted, which returns False).
    """
    attempts = 0
    while True:
        result: CIPollResult | None = None
        for _ in range(max_polls_per_attempt):
            r = poll_fn(pr_number)
            events.emit(
                "ci.poll",
                pr_number=pr_number,
                attempt=attempts,
                status=r.status,
            )
            if r.status in ("pass", "fail"):
                result = r
                break
            sleep_fn(poll_interval)
        if result is None:
            return False
        if result.status == "pass":
            return True
        if attempts >= max_attempts:
            return False
        attempts += 1
        cont = fix(result.raw, attempts)
        if not cont:
            return False
