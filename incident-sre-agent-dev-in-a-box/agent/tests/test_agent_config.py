"""Tests for agent.yaml declarative configuration and agent_config loader.

Verifies:
1. Byte-identical equality between loaded YAML instructions and git golden fixtures.
2. Fail-fast error containment on missing or malformed agent.yaml.
3. Declarative ADK agent hierarchy, schemas, descriptions, and model resolution.
4. Backward-compatible load_prompt shim.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    "changes_specialist": "e59b7ba6410645ce8c3aceb9efacad8f08cd704fc79903a844ab309db7e43081",
    "logs_specialist": "1c6b10a3554defb9071103d8914ab249057f5d5cc476bc0a0eb1baa14f4dbf9b",
    "metrics_specialist": "a32edb36b3111fdc4cb0f402c92e2310f833ed2a1417de4d4a16aca884661c8e",
    "resources_specialist": "79e097a49b924d08a79d57a0a18ddfd07f265a0b78d8d04346b6746b9ac37a04",
}

EXPECTED_INSTRUCTION_LENGTHS: Dict[str, int] = {
    "triage": 706,
    "synthesizer": 6919,
    "changes_specialist": 2083,
    "logs_specialist": 1457,
    "metrics_specialist": 1446,
    "resources_specialist": 2364,
}


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_prompts"


def load_fixture(filename: str) -> str:
    """Load golden fixture text captured from base commit 8122eef, failing loudly if not found."""
    fixture_path = FIXTURES_DIR / filename
    if not fixture_path.exists():
        raise FileNotFoundError(f"Missing required golden fixture: {fixture_path}")
    return fixture_path.read_text(encoding="utf-8")


# Fixed sample data matching fixtures/golden_prompts assembled fixtures
SAMPLE_INCIDENT = {
    "id": "inc-sample-1234",
    "external_key": "INC-SAMPLE-1234",
    "title": "Checkout latency degradation and error spike",
    "description": (
        "P99 latency elevated to 4200ms with intermittent 500 errors across checkout workers."
    ),
    "severity": "P1",
    "service_hint": "checkoutservice",
    "opened_at": "2026-10-23T00:00:00+00:00",
}

SAMPLE_PLAN = {
    "affected_service": "checkoutservice",
    "symptoms": ["HTTP 500 spike", "Latency P99 > 4000ms"],
    "initial_hypotheses": [
        "Redis cache connection pool exhausted",
        "CPU throttling on checkout workers",
    ],
    "start_time": "2026-10-22T23:00:00+00:00",
    "end_time": "2026-10-23T00:00:00+00:00",
}

SAMPLE_PLAYBOOK_INSTRUCTIONS = (
    "1. Check cloud monitoring latency dashboards.\n2. Correlate with recent Cloud Run revisions."
)

SAMPLE_STYLE_GUIDELINES = (
    "Present an executive summary followed by chronological evidence and actionable mitigation."
)

SAMPLE_LOGS_SECTION = (
    "  - [Log] 502 Bad Gateway observed in checkoutservice proxy\n"
    "  - [Log] ConnectionTimeout to payment-gateway"
)

SAMPLE_METRICS_SECTION = (
    "  - [Metric] run.googleapis.com/request_latencies p99 reached 4500ms\n"
    "  - [Anomaly] CPU utilization at 98%"
)

SAMPLE_DEP_SECTION = (
    "  - Downstream dependency 'paymentservice': FAILING/ERRORS (4 errors, 2 anomalies). "
    "Probable root cause originates in this downstream dependency.\n"
    "  - Downstream dependency 'currencyservice': HEALTHY (0 errors, no anomalies). "
    "Delay originates in primary service request processing path, not downstream dependencies."
)

SAMPLE_FINDINGS_SUMMARY = {
    "logs": {"summary": "Observed 42 HTTP 500 errors in checkoutservice"},
    "changes": {"summary": "Revision rev-checkout-004 deployed 12 minutes before onset"},
}


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


def test_golden_fixtures_loaded_instructions_byte_identity() -> None:
    """Verify EVERY loaded instruction matches base 8122eef golden fixtures byte-for-byte."""
    config = get_agent_yaml_config()
    for agent_name in EXPECTED_INSTRUCTION_HASHES:
        expected = load_fixture(f"instruction_{agent_name}.txt")
        actual = config.get_instruction(agent_name)
        assert actual == expected, (
            f"Instruction mismatch for {agent_name}: loaded YAML differs from golden fixture"
        )


def test_golden_fixtures_loaded_templates_byte_identity() -> None:
    """Verify EVERY loaded prompt template matches base 8122eef golden fixtures byte-for-byte."""
    config = get_agent_yaml_config()

    # Root templates
    assert config.get_prompt_template("tool_prune_conclusive_instruction") == load_fixture(
        "template_tool_prune_conclusive_instruction.txt"
    )
    assert config.get_prompt_template("symptoms_entry") == load_fixture(
        "template_symptoms_entry.txt"
    )
    assert config.get_prompt_template("hypotheses_entry") == load_fixture(
        "template_hypotheses_entry.txt"
    )

    # Triage templates
    assert config.get_prompt_template("triage", "triage_plan_user_prompt") == load_fixture(
        "template_triage_plan_user_prompt.txt"
    )
    assert config.get_prompt_template("triage", "triage_playbook_prefix") == load_fixture(
        "template_triage_playbook_prefix.txt"
    )

    # Specialist investigation prompt templates
    for agent_name in [
        "changes_specialist",
        "logs_specialist",
        "metrics_specialist",
        "resources_specialist",
    ]:
        assert config.get_prompt_template(agent_name, "investigation_prompt") == load_fixture(
            f"template_{agent_name.replace('_specialist', '')}_investigation_prompt.txt"
        )

    # Synthesizer templates
    synth_templates = [
        "report_style_prefix",
        "dep_failing_template",
        "dep_healthy_template",
        "dep_none_recorded",
        "dep_none_inspected",
        "prior_incident_evidence_tag",
        "synthesis_report_user_prompt",
        "probable_root_cause_prompt",
    ]
    for tmpl in synth_templates:
        assert config.get_prompt_template("synthesizer", tmpl) == load_fixture(
            f"template_{tmpl}.txt"
        )


def test_golden_fixtures_assembled_prompts_byte_identity() -> None:
    """Verify assembled system_instruction & prompts match base 8122eef fixtures."""
    cfg = get_agent_yaml_config()

    # 1. Triage system instruction (with and without skill)
    triage_inst = cfg.get_instruction("triage")
    triage_playbook = cfg.format_prompt(
        "triage",
        "triage_playbook_prefix",
        instructions=SAMPLE_PLAYBOOK_INSTRUCTIONS,
    )
    assembled_triage_sys_with_skill = triage_inst + triage_playbook
    assembled_triage_sys_without_skill = triage_inst
    assert assembled_triage_sys_with_skill == load_fixture(
        "assembled_triage_system_instruction_with_skill.txt"
    )
    assert assembled_triage_sys_without_skill == load_fixture(
        "assembled_triage_system_instruction_without_skill.txt"
    )

    # 2. Triage user content
    triage_user = cfg.format_prompt(
        "triage",
        "triage_plan_user_prompt",
        id=SAMPLE_INCIDENT["id"],
        external_key=SAMPLE_INCIDENT["external_key"],
        title=SAMPLE_INCIDENT["title"],
        description=SAMPLE_INCIDENT["description"],
        severity=SAMPLE_INCIDENT["severity"],
        service_hint=SAMPLE_INCIDENT["service_hint"],
        opened_at=SAMPLE_INCIDENT["opened_at"],
    )
    assert triage_user == load_fixture("assembled_triage_user_content.txt")

    # 3. Specialists investigation prompts (with & without symptoms/hypotheses)
    symptoms_entry = "\n" + cfg.format_prompt(
        "symptoms_entry", symptoms=", ".join(SAMPLE_PLAN["symptoms"])
    )
    hypotheses_entry = "\n" + cfg.format_prompt(
        "hypotheses_entry", hypotheses=", ".join(SAMPLE_PLAN["initial_hypotheses"])
    )

    for mod in ["changes", "logs", "metrics", "resources"]:
        spec_name = f"{mod}_specialist"

        # With symptoms & hypotheses
        prompt_with = cfg.format_prompt(
            spec_name,
            "investigation_prompt",
            incident_id=SAMPLE_INCIDENT["id"],
            title=SAMPLE_INCIDENT["title"],
            description=SAMPLE_INCIDENT["description"],
            severity=SAMPLE_INCIDENT["severity"],
            target_service=SAMPLE_PLAN["affected_service"],
            window_start=SAMPLE_PLAN["start_time"],
            window_end=SAMPLE_PLAN["end_time"],
            symptoms_line=symptoms_entry,
            hypotheses_line=hypotheses_entry,
        )
        assert prompt_with == load_fixture(
            f"assembled_{mod}_investigation_prompt_with_symptoms_and_hypotheses.txt"
        )

        # Without symptoms & hypotheses
        prompt_without = cfg.format_prompt(
            spec_name,
            "investigation_prompt",
            incident_id=SAMPLE_INCIDENT["id"],
            title=SAMPLE_INCIDENT["title"],
            description=SAMPLE_INCIDENT["description"],
            severity=SAMPLE_INCIDENT["severity"],
            target_service=SAMPLE_PLAN["affected_service"],
            window_start=SAMPLE_PLAN["start_time"],
            window_end=SAMPLE_PLAN["end_time"],
            symptoms_line="",
            hypotheses_line="",
        )
        assert prompt_without == load_fixture(
            f"assembled_{mod}_investigation_prompt_without_symptoms_and_hypotheses.txt"
        )

    # 4. Synthesizer system instruction (with and without style)
    synth_inst = cfg.get_instruction("synthesizer")
    synth_style = cfg.format_prompt(
        "synthesizer",
        "report_style_prefix",
        instructions=SAMPLE_STYLE_GUIDELINES,
    )
    assembled_synth_sys_with_style = synth_inst + synth_style
    assembled_synth_sys_without_style = synth_inst
    assert assembled_synth_sys_with_style == load_fixture(
        "assembled_synthesizer_system_instruction_with_style.txt"
    )
    assert assembled_synth_sys_without_style == load_fixture(
        "assembled_synthesizer_system_instruction_without_style.txt"
    )

    # 5. Synthesizer user content
    synth_user = cfg.format_prompt(
        "synthesizer",
        "synthesis_report_user_prompt",
        external_key=SAMPLE_INCIDENT["external_key"],
        title=SAMPLE_INCIDENT["title"],
        severity=SAMPLE_INCIDENT["severity"],
        affected_service=SAMPLE_PLAN["affected_service"],
        symptoms=SAMPLE_PLAN["symptoms"],
        window_start=SAMPLE_PLAN["start_time"],
        window_end=SAMPLE_PLAN["end_time"],
        logs_section=SAMPLE_LOGS_SECTION,
        metrics_section=SAMPLE_METRICS_SECTION,
        dep_section=SAMPLE_DEP_SECTION,
        findings_summary=json.dumps(SAMPLE_FINDINGS_SUMMARY, default=str, indent=2),
    )
    assert synth_user == load_fixture("assembled_synthesizer_user_content.txt")

    # 6. Probable root cause prompt
    prc_prompt = cfg.format_prompt(
        "synthesizer",
        "probable_root_cause_prompt",
        affected_service=SAMPLE_PLAN["affected_service"],
        findings_summary=json.dumps(SAMPLE_FINDINGS_SUMMARY, default=str),
    )
    assert prc_prompt == load_fixture("assembled_probable_root_cause_prompt.txt")

    # 7. Tool prune conclusive instruction
    tool_prune = cfg.get_prompt_template("tool_prune_conclusive_instruction")
    assert tool_prune == load_fixture("assembled_tool_prune_conclusive_instruction.txt")


def test_golden_fixtures_fail_loudly_when_fixture_missing() -> None:
    """Verify that fixture loading fails loudly if a required fixture file is missing."""
    with pytest.raises(FileNotFoundError, match="Missing required golden fixture"):
        load_fixture("non_existent_fixture_file_12345.txt")


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


def test_prompt_templates_loading_and_formatting() -> None:
    """Verify that prompt templates are properly loaded and formatted from agent.yaml."""
    cfg = get_agent_yaml_config()

    # Root prompt templates
    conclusive = cfg.get_prompt_template("tool_prune_conclusive_instruction")
    assert "Evidence collection is finished" in conclusive
    assert "On Turn 3, you MUST invoke 'set_model_response'" in conclusive

    symptoms_entry = cfg.format_prompt("symptoms_entry", symptoms="5xx, latency")
    assert symptoms_entry == "Observed Symptoms: 5xx, latency"

    # Specialist investigation prompts
    logs_prompt = cfg.format_prompt(
        "logs_specialist",
        "investigation_prompt",
        incident_id="inc-123",
        title="Payment failures",
        description="High 500 error rate",
        severity="P1",
        target_service="payment-svc",
        window_start="2026-10-23T00:00:00Z",
        window_end="2026-10-23T01:00:00Z",
        symptoms_line="\nObserved Symptoms: 5xx errors",
        hypotheses_line="\nTriage Hypotheses: database timeout",
    )
    assert "Investigate incident inc-123: Payment failures" in logs_prompt
    assert "Target Service: payment-svc" in logs_prompt
    assert "Observed Symptoms: 5xx errors" in logs_prompt
    assert "cloud-logging-queries" in logs_prompt

    # Triage plan prompt
    triage_prompt = cfg.format_prompt(
        "triage",
        "triage_plan_user_prompt",
        id="inc-456",
        external_key="INC-456",
        title="Cart latency",
        description="Cart is slow",
        severity="P2",
        service_hint="cart-svc",
        opened_at="2026-10-23T00:00:00Z",
    )
    assert "Analyze incident ticket and generate an InvestigationPlan" in triage_prompt
    assert "ID: inc-456" in triage_prompt

    # Missing template error
    with pytest.raises(AgentConfigError, match="not found"):
        cfg.get_prompt_template("non_existent_template")

    # Missing placeholder error
    with pytest.raises(AgentConfigError, match="Missing required placeholder"):
        cfg.format_prompt("symptoms_entry")
