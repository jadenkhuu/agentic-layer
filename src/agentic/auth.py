"""Auth detection for non-stub runs.

The claude-agent-sdk picks its credential source automatically:
  - If ANTHROPIC_API_KEY is set, it uses the API key (billed to your API account).
  - Otherwise it falls back to the credentials written by `claude login`
    (billed to your Pro/Max plan).

We just *report* which path will be taken so the user isn't surprised — and we
warn loudly when the API-key path is active, since Max users typically don't
want to burn API credits unintentionally.
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class AuthMethod(str, Enum):
    API_KEY = "API_KEY"        # ANTHROPIC_API_KEY set — bills API account
    CLI_LOGIN = "CLI_LOGIN"    # ~/.claude/.credentials.json — bills Max/Pro plan


class NoAuthConfigured(RuntimeError):
    """Neither ANTHROPIC_API_KEY nor a Claude CLI login was found."""


def _config_dir(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def detect_auth(env: dict[str, str] | None = None) -> AuthMethod:
    """Return which auth path will be active, or raise NoAuthConfigured.

    `env` is injectable for testing; defaults to os.environ.
    """
    e = env if env is not None else os.environ
    if e.get("ANTHROPIC_API_KEY"):
        return AuthMethod.API_KEY
    if (_config_dir(e) / ".credentials.json").exists():
        return AuthMethod.CLI_LOGIN
    raise NoAuthConfigured(
        "no auth configured. Run `claude login` (uses Max/Pro plan) "
        "or set ANTHROPIC_API_KEY."
    )
