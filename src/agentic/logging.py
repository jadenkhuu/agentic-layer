from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

from agentic.context import RunContext

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"


def setup_run_logging(ctx: RunContext, level: int = logging.INFO) -> Path:
    """Attach a file handler scoped to this run plus a rich console handler.

    Returns the path to the run's log file. Handlers are tagged with the run_id
    so concurrent runs can be torn down independently via `teardown_run_logging`.
    """
    ctx.working_dir.mkdir(parents=True, exist_ok=True)
    log_path = ctx.working_dir / "run.log"
    root = logging.getLogger()
    root.setLevel(level)

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    file_handler.set_name(f"agentic-file-{ctx.run_id}")
    root.addHandler(file_handler)

    if not any(getattr(h, "_agentic_console", False) for h in root.handlers):
        console_handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        console_handler._agentic_console = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)

    return log_path


def teardown_run_logging(ctx: RunContext) -> None:
    root = logging.getLogger()
    target = f"agentic-file-{ctx.run_id}"
    for h in list(root.handlers):
        if h.get_name() == target:
            h.close()
            root.removeHandler(h)
