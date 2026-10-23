"""Tests for agent.yaml declarative configuration and agent_config loader.

Verifies:
1. Byte-identical equality between loaded YAML instructions and git golden fixtures.
2. Fail-fast error containment on missing or malformed agent.yaml.
3. Declarative ADK agent hierarchy, schemas, descriptions, and model resolution.
4. Backward-compatible load_prompt shim.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterator
from unittest.mock import patch

import pytest
import yaml
from agent.app.agent_config import (
    AgentConfigError,
    clear_agent_yaml_cache,
    get_agent_yaml_config,
    load_agent_yaml,
)
from agent.app.dispatcher import Dispatcher
from agent.app.prompts import load_prompt
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import TriageAgent

# Golden SHA256 checksums of original instructions before migration
EXPECTED_INSTRUCTION_HASHES: Dict[str, str] = {
    "triage": "8406f1bdc06a9251d2211954446d8b905ac2f2bf9848f4d1b9b65b633a42f959",
    "synthesizer": "c915b3e0a2a184a0096eaea5cabbc88cd6d9fbf1ddc8ea155facb48dc4b6765d",
    "changes_specialist": "58bcafc4cf67704e0f8a443ee7d0083c1d9dc2139d9ba6c1c5543107441e2eff",
    "logs_specialist": "3622c753910b367def5fc77ece1d6cf77e4e92f90d7bb2453a0af0d7cfaf1c0b",
    "metrics_specialist": "dc83ec50ed0e3d8b2a745bc720ece540d73c4ce369e5e6e6cad4e82e089ce318",
    "resources_specialist": "a98a669b76d6819dd296f42b5f96d7fce1e4d16e684865969b4370b1495b6095",
}

EXPECTED_INSTRUCTION_LENGTHS: Dict[str, int] = {
    "triage": 706,
    "synthesizer": 6919,
    "changes_specialist": 2008,
    "logs_specialist": 1301,
    "metrics_specialist": 1287,
    "resources_specialist": 2037,
}


def _get_git_historical_prompt(filename: str) -> str:
    """Retrieve historical prompt string from git main or HEAD."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    rel_path = f"incident-sre-agent/agent/app/prompts/{filename}"
    try:
        cmd = ["git", "show", f"HEAD~1:{rel_path}"]
        res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        try:
            cmd = ["git", "show", f"origin/main:{rel_path}"]
            res = subprocess.run(
                cmd, cwd=str(repo_root), capture_output=True, text=True, check=True
            )
            return res.stdout.strip()
        except Exception:
            return ""


@pytest.fixture(autouse=True)
def reset_yaml_cache() -> Iterator[None]:
    clear_agent_yaml_cache()
    yield
    clear_agent_yaml_cache()


def test_agent_yaml_golden_byte_identity() -> None:
    """Assert every loaded instruction is byte-identical to golden constants and checksums."""
    config = get_agent_yaml_config()

    for agent_name, expected_hash in EXPECTED_INSTRUCTION_HASHES.items():
        instruction = config.get_instruction(agent_name)
        actual_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        assert actual_hash == expected_hash, (
            f"Instruction mismatch for {agent_name}: expected {expected_hash}, got {actual_hash}"
        )
        assert len(instruction) == EXPECTED_INSTRUCTION_LENGTHS[agent_name]


def test_git_historical_prompts_match_golden_base() -> None:
    """Verify that git historical text files match base text in YAML if git is available."""
    git_triage = _get_git_historical_prompt("triage.txt")
    if git_triage:
        config = get_agent_yaml_config()
        assert config.get_instruction("triage") == git_triage

    git_synth = _get_git_historical_prompt("synthesizer.txt")
    if git_synth:
        config = get_agent_yaml_config()
        assert config.get_instruction("synthesizer") == git_synth


def test_load_prompt_backward_compatibility() -> None:
    """Verify legacy load_prompt shim returns byte-identical instructions."""
    cfg = get_agent_yaml_config()
    assert load_prompt("triage") == cfg.get_instruction("triage")
    assert load_prompt("synthesizer") == cfg.get_instruction("synthesizer")
    assert load_prompt("changes") == cfg.get_instruction("changes_specialist")
    assert load_prompt("logs") == cfg.get_instruction("logs_specialist")
    assert load_prompt("metrics") == cfg.get_instruction("metrics_specialist")
    assert load_prompt("resources") == cfg.get_instruction("resources_specialist")


def test_agent_yaml_hierarchy_and_metadata() -> None:
    """Verify the ADK agent tree structure and schemas."""
    config = get_agent_yaml_config()
    assert config.agent_class == "BaseAgent"
    assert config.name == "dispatcher"
    assert config.description == "Multi-agent SRE investigation orchestrator"

    top_agents = {sub.name: sub for sub in config.sub_agents}
    assert set(top_agents.keys()) == {"triage", "specialists_parallel", "synthesizer"}
    assert top_agents["triage"].agent_class == "LlmAgent"
    assert top_agents["triage"].output_schema == "app.contracts.triage.InvestigationPlan"

    assert top_agents["specialists_parallel"].agent_class == "ParallelAgent"
    specialists = {s.name: s for s in top_agents["specialists_parallel"].sub_agents or []}
    assert set(specialists.keys()) == {
        "changes_specialist",
        "logs_specialist",
        "metrics_specialist",
        "resources_specialist",
    }
    for spec in specialists.values():
        assert spec.agent_class == "LlmAgent"
        assert spec.output_schema == "app.contracts.findings.Findings"

    assert top_agents["synthesizer"].agent_class == "LlmAgent"
    assert top_agents["synthesizer"].output_schema == "app.contracts.report.Report"


def test_model_resolution_and_no_model_literals_in_yaml() -> None:
    """Verify that agent.yaml contains zero gemini model literals and resolves via config."""
    config_path = Path(__file__).resolve().parent.parent / "app" / "agent.yaml"
    raw_content = config_path.read_text(encoding="utf-8")
    assert "gemini-" not in raw_content, (
        "agent.yaml must not contain hardcoded gemini model literals"
    )

    config = get_agent_yaml_config()
    with patch.dict(os.environ, {"MODEL_SPECIALIST": "custom-specialist-model"}):
        resolved_model = config.get_model("changes_specialist")
        assert resolved_model == "custom-specialist-model"


def test_missing_agent_yaml_raises_agent_config_error(tmp_path: Path) -> None:
    """Verify missing agent.yaml fails fast with AgentConfigError."""
    non_existent = tmp_path / "missing_agent.yaml"
    with pytest.raises(AgentConfigError, match="Required agent configuration file not found"):
        load_agent_yaml(non_existent)


def test_empty_agent_yaml_raises_agent_config_error(tmp_path: Path) -> None:
    """Verify empty agent.yaml fails fast."""
    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="is empty"):
        load_agent_yaml(empty_file)


def test_malformed_agent_yaml_syntax_raises_agent_config_error(tmp_path: Path) -> None:
    """Verify invalid YAML syntax fails fast."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("agent_class: BaseAgent\nname: [unclosed list\n", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="contains invalid YAML"):
        load_agent_yaml(bad_yaml)


def test_malformed_agent_yaml_missing_specialist_raises(tmp_path: Path) -> None:
    """Verify missing required specialist fails schema validation."""
    invalid_structure = {
        "agent_class": "BaseAgent",
        "name": "dispatcher",
        "description": "Orchestrator",
        "sub_agents": [
            {
                "agent_class": "LlmAgent",
                "name": "triage",
                "instruction": "Triage instruction",
            },
            {
                "agent_class": "ParallelAgent",
                "name": "specialists_parallel",
                "sub_agents": [
                    {
                        "agent_class": "LlmAgent",
                        "name": "logs_specialist",
                        "instruction": "Logs instruction",
                    }
                    # Missing changes, metrics, resources
                ],
            },
            {
                "agent_class": "LlmAgent",
                "name": "synthesizer",
                "instruction": "Synth instruction",
            },
        ],
    }
    bad_file = tmp_path / "missing_spec.yaml"
    bad_file.write_text(yaml.dump(invalid_structure), encoding="utf-8")
    with pytest.raises(AgentConfigError, match="missing required specialists"):
        load_agent_yaml(bad_file)


def test_agent_classes_instantiation_fails_when_yaml_missing(tmp_path: Path) -> None:
    """Verify Dispatcher and Specialists fail at startup when agent.yaml is missing."""
    clear_agent_yaml_cache()
    fake_path = tmp_path / "non_existent.yaml"
    with patch("agent.app.agent_config.DEFAULT_YAML_PATH", fake_path):
        with pytest.raises(AgentConfigError):
            Dispatcher()
        with pytest.raises(AgentConfigError):
            TriageAgent()
        with pytest.raises(AgentConfigError):
            SynthesizerAgent()
        with pytest.raises(AgentConfigError):
            ChangesSpecialist()
        with pytest.raises(AgentConfigError):
            LogsSpecialist()
        with pytest.raises(AgentConfigError):
            MetricsSpecialist()
        with pytest.raises(AgentConfigError):
            ResourcesSpecialist()
