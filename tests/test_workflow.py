from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agentic.workflow import Workflow, list_workflows


def test_load_valid_workflow(fixtures_dir: Path):
    wf = Workflow.load(fixtures_dir / "test-workflow.yaml")
    assert wf.name == "test-workflow"
    assert [a.id for a in wf.agents] == ["spec", "explore", "plan"]
    assert wf.agents[0].inputs == ["task"]
    assert wf.agents[0].outputs == ["SPEC.md"]
    assert wf.agents[1].allowed_tools == ["Read", "Grep", "Glob"]


def test_rejects_empty_agents(tmp_path: Path):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({"name": "x", "agents": []}))
    with pytest.raises(ValidationError):
        Workflow.load(p)


def test_rejects_duplicate_agent_ids(tmp_path: Path):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump({
        "name": "x",
        "agents": [
            {"id": "a", "outputs": ["A.md"]},
            {"id": "a", "outputs": ["B.md"]},
        ],
    }))
    with pytest.raises(ValidationError):
        Workflow.load(p)


def test_rejects_non_mapping_top_level(tmp_path: Path):
    p = tmp_path / "wf.yaml"
    p.write_text("- not a mapping\n")
    with pytest.raises(ValueError):
        Workflow.load(p)


def test_find_via_target_repo(repo: Path):
    wf = Workflow.find("test-workflow", repo)
    assert wf.name == "test-workflow"


def test_find_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Workflow.find("nope", tmp_path)


def test_list_workflows(repo: Path):
    assert list_workflows(repo) == ["test-workflow"]


def test_list_workflows_empty(tmp_path: Path):
    assert list_workflows(tmp_path) == []
