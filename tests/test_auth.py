from pathlib import Path

import pytest

from agentic.auth import AuthMethod, NoAuthConfigured, detect_auth


def test_picks_api_key_when_env_set(tmp_path: Path):
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert detect_auth(env=env) is AuthMethod.API_KEY


def test_api_key_wins_even_when_login_present(tmp_path: Path):
    """An explicit API key in env always wins — that's the SDK's own behaviour."""
    (tmp_path / ".credentials.json").write_text('{"fake": true}')
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert detect_auth(env=env) is AuthMethod.API_KEY


def test_picks_cli_login_when_key_absent_and_creds_present(tmp_path: Path):
    (tmp_path / ".credentials.json").write_text('{"fake": true}')
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert detect_auth(env=env) is AuthMethod.CLI_LOGIN


def test_raises_when_neither_configured(tmp_path: Path):
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    with pytest.raises(NoAuthConfigured, match="claude login"):
        detect_auth(env=env)


def test_empty_api_key_is_treated_as_absent(tmp_path: Path):
    """An empty-string key shouldn't accidentally claim API auth."""
    (tmp_path / ".credentials.json").write_text('{"fake": true}')
    env = {"ANTHROPIC_API_KEY": "", "CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert detect_auth(env=env) is AuthMethod.CLI_LOGIN
