from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway target repo with the test-workflow installed."""
    (tmp_path / ".agentic" / "workflows").mkdir(parents=True)
    src = FIXTURES / "test-workflow.yaml"
    dst = tmp_path / ".agentic" / "workflows" / "test-workflow.yaml"
    dst.write_text(src.read_text())
    return tmp_path
