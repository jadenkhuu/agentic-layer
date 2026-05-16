"""Tests for `agentic schema` — the JSON-Schema export consumed by tooling."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from agentic.cli import main
from agentic.client_config import ClientConfig
from agentic.workflow import Workflow


def test_schema_workflow_emits_pydantic_schema():
    result = CliRunner().invoke(main, ["schema", "--workflow"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema == Workflow.model_json_schema()
    # the schema names the fields the runner actually reads
    for field in ("name", "description", "agents", "mcp_servers"):
        assert field in schema["properties"]


def test_schema_client_config_emits_pydantic_schema():
    result = CliRunner().invoke(main, ["schema", "--client-config"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema == ClientConfig.model_json_schema()
    for field in ("name", "stack", "conventions", "do", "do_not"):
        assert field in schema["properties"]


def test_schema_defaults_to_workflow():
    result = CliRunner().invoke(main, ["schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == Workflow.model_json_schema()


def test_schema_output_is_valid_json():
    """stdout must be machine-parsable — helm pipes it straight into Monaco."""
    for args in (["schema", "--workflow"], ["schema", "--client-config"]):
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        json.loads(result.output)  # raises if not valid JSON


def test_workflow_schema_roundtrips_a_real_workflow(fixtures_dir: Path):
    """A workflow that loads cleanly must dump to data the model re-accepts —
    i.e. the schema describes a faithful, lossless representation.
    """
    wf = Workflow.load(fixtures_dir / "test-workflow.yaml")
    dumped = wf.model_dump(mode="json")
    again = Workflow.model_validate(dumped)
    assert again == wf
    # every dumped key is a property the exported schema declares
    schema_props = set(Workflow.model_json_schema()["properties"])
    assert set(dumped).issubset(schema_props)


def test_client_config_schema_roundtrips():
    cfg = ClientConfig(
        name="acme",
        stack="next.js + postgres",
        conventions=["kebab-case files"],
        do=["write tests"],
        do_not=["use any"],
    )
    dumped = cfg.model_dump(mode="json")
    again = ClientConfig.model_validate(dumped)
    assert again == cfg
    schema_props = set(ClientConfig.model_json_schema()["properties"])
    assert set(dumped).issubset(schema_props)
