"""Unit tests verifying externalized agent instructions in agent.yaml.

Guarantees:
1. Byte-level equality between loaded instructions and original golden constants.
2. Startup failure (FileNotFoundError / ValueError) when agent.yaml is missing or malformed.
3. Live instantiated agents use byte-identical instructions.
"""

from pathlib import Path
from typing import Generator

import pytest
from agent.app.agent_config import (
    get_agent_config,
    load_agent_config,
    reset_agent_config,
)
from agent.app.dispatcher import Dispatcher
from agent.app.specialists.iac_config import IaCConfigSpecialist
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import TriageAgent

# ==============================================================================
# Golden Fixtures: byte-for-byte captures of previous inline Python constants
# ==============================================================================

GOLDEN_DISPATCHER_DESCRIPTION = (
    "Multi-agent CI/CD pipeline failure investigation orchestrator"
)

GOLDEN_TRIAGE_INSTRUCTION = (
    "You are a CI/CD Triage Agent. Analyze failure context and logs to formulate "
    "an InvestigationPlan selecting specialized agents and runbook skills."
)

GOLDEN_PIPELINE_LOGS_DEFAULT = (
    "You are a specialist investigating CI pipeline logs (lint, test, build)."
)

GOLDEN_PIPELINE_LOGS_DRILLDOWN = (
    "You are a CI/CD pipeline logs diagnostic specialist.\n\n"
    "CRITICAL WORKFLOW REQUIREMENT:\n"
    "1. On Turn 1, invoke 'load_skill' exactly once with skill_name='{skill_name}'. "
    "Do NOT invoke list_skills or load other skills.\n"
    "2. On Turn 2, invoke at least one read-only data tool ('get_commit_diff' or "
    "'fetch_job_logs') to inspect repository changes or execution logs.\n"
    "3. On Turn 3, conclude immediately by invoking 'set_model_response' with your "
    "structured Findings report. Incorporate the data tool output directly into your "
    "findings."
)

GOLDEN_RUNTIME_HEALTH_DEFAULT = (
    "You are a specialist investigating Cloud Run deployment and runtime health."
)

GOLDEN_RUNTIME_HEALTH_DRILLDOWN = (
    "You are a Cloud Run deployment and runtime health diagnostic specialist.\n\n"
    "CRITICAL WORKFLOW REQUIREMENT:\n"
    "1. On Turn 1, invoke 'load_skill' exactly once with "
    "skill_name='cloudrun-deploy-troubleshooting'. Do NOT invoke other skills.\n"
    "2. On Turn 2, invoke decisive read-only diagnostic data tools for the failure:\n"
    "   - Invoke 'get_commit_diff' (repo, head_sha) to inspect repository or deployment "
    "configuration changes introduced by the commit (e.g. resource limits, "
    "environment variables, configuration changes).\n"
    "   - Invoke 'fetch_job_logs' (repo, job_id) to inspect the deployment step runner "
    "logs containing the decisive failure error.\n"
    "   - And/or invoke 'describe_service', 'list_service_revisions', or "
    "'describe_cloud_run_revision' to inspect runtime telemetry and revision history.\n"
    "   NOTE ON LIVE STATE: 'describe_service' reports live cluster state. "
    "Compare revision creation timestamps from 'list_service_revisions' against the "
    "run failure window. If no revision existed at failure time, note that no revision "
    "existed at failure time and current status reflects revisions created subsequent "
    "to the failure event. If a prior revision existed at failure time, dynamically note "
    "the active revision name at failure time. Do NOT present current health as success "
    "of the failed deployment.\n"
    "3. On Turn 3, conclude immediately by invoking 'set_model_response' with your "
    "structured Findings report citing the tool outputs."
)

GOLDEN_IAC_CONFIG_DEFAULT = (
    "You are a specialist diagnosing Infrastructure-as-Code "
    "(Terraform, YAML manifests) errors."
)

GOLDEN_IAC_CONFIG_DRILLDOWN = (
    "You are an Infrastructure-as-Code (Terraform, YAML manifests) diagnostic "
    "specialist.\n\n"
    "CRITICAL WORKFLOW REQUIREMENT:\n"
    "1. On Turn 1, invoke 'load_skill' exactly once with "
    "skill_name='cloudrun-deploy-troubleshooting'.\n"
    "2. On Turn 2, invoke at least one read-only data tool ('get_commit_diff' or "
    "'fetch_job_logs') to inspect commit changes and configuration files.\n"
    "3. On Turn 3, conclude immediately by invoking 'set_model_response' with your "
    "structured Findings report."
)

GOLDEN_SYNTHESIZER_DEFAULT = (
    "You are the Incident Report Synthesizer Agent. Compile specialist findings, "
    "telemetry evidence, and causal root cause into a cohesive Report "
    "adhering to the schema."
)

GOLDEN_SRE_SYNTHESIS_TEMPLATE = (
    "You are an automated Site Reliability Engineering diagnostic assistant.\n"
    "Analyze the following failed GitHub Actions CI/CD pipeline step and raw runner logs:\n\n"
    "Job: {job_name}\n"
    "Step: {step_name}\n"
    "{findings_context}\n"
    "Runner Logs:\n```\n{log_excerpt}\n```\n\n"
    "Instructions:\n"
    "1. Identify the exact root cause grounded strictly in the provided runner log text and "
    "specialist findings.\n"
    "2. Incorporate the specialist's structured findings and exact error counts/codes. "
    "List each distinct error code with its count and message. "
    "Do NOT guess, approximate, or misattribute error counts, codes, or files. "
    "Cite all codes and counts accurately.\n"
    "3. Correlate findings with git diff hunks when available, explicitly distinguishing "
    "violations introduced directly by modified lines from consequential violations "
    "caused by line movements or cascading errors.\n"
    "4. Diff-grounded test failure remediation: When Git Diff Context indicates the failure "
    "was introduced in test code (e.g. failing assertion compares hardcoded literals or test "
    "was modified in diff without app changes), the primary remediation MUST lead directly "
    "with test code correction (fixing the test logic or correcting the assertion), NOT "
    "advise investigating application business logic when the diff proves the test itself "
    "is defective. Avoid hedging ('likely introduced by') when the diff is conclusive. "
    "When the commit diff touches only test files and no application code changed, causal "
    "remediation must target the test and must not recommend changing application code unless "
    "tool evidence shows an application defect.\n"
    "5. Warning segregation: Put non-causal warnings (e.g. deprecation or runtime warnings "
    "not on the fatal path) into unrelated_observations, NEVER into causal_remediation. "
    "Do not treat un-linked warnings as the primary root cause.\n"
    "6. Deploy & runtime failure correlation: When container logs or runtime "
    "telemetry indicate termination (e.g. out of memory / exit code 137, signal 9, or "
    "health probe failure), the root cause MUST quote the exact decisive log line(s) before "
    "termination (e.g. 'Memory limit of ... exceeded with ... used' or probe error) and "
    "correlate directly with specific parameters modified in git diff hunks (e.g. memory "
    "limits reduced or buffer allocations added). Primary remediation must lead directly "
    "with reverting or adjusting the specific modified configuration in the diff.\n"
    "7. Do NOT invent or hallucinate revision numbers, resource names, or "
    "projects not in the logs.\n"
    "8. Provide actionable remediation steps addressing each distinct violation directly.\n"
    "9. Respond with valid JSON with keys: 'root_cause' (string), "
    "'causal_remediation' (list of strings), 'unrelated_observations' (list of strings), "
    "'confidence' (float between 0.0 and 1.0).\n"
)


@pytest.fixture(autouse=True)
def clean_config_cache() -> Generator[None, None, None]:
    """Ensure cache is reset before and after each test."""
    reset_agent_config()
    yield
    reset_agent_config()


class TestAgentYamlByteIdentity:
    """Zero-behaviour-change test suite asserting byte-level identity against golden constants."""

    def test_golden_byte_identity_loaded_from_yaml(self) -> None:
        cfg = get_agent_config()

        # 1. Dispatcher description
        assert cfg.description.encode("utf-8") == GOLDEN_DISPATCHER_DESCRIPTION.encode("utf-8")

        # 2. Triage instruction
        triage_inst = cfg.get_instruction("triage")
        assert triage_inst.encode("utf-8") == GOLDEN_TRIAGE_INSTRUCTION.encode("utf-8")

        # 3. Pipeline logs specialist instructions
        pls_default = cfg.get_instruction("pipeline_logs_specialist")
        assert pls_default.encode("utf-8") == GOLDEN_PIPELINE_LOGS_DEFAULT.encode("utf-8")

        pls_drilldown = cfg.get_drilldown_instruction("pipeline_logs_specialist")
        assert pls_drilldown.encode("utf-8") == GOLDEN_PIPELINE_LOGS_DRILLDOWN.encode("utf-8")

        # 4. Runtime health specialist instructions
        rhs_default = cfg.get_instruction("runtime_health_specialist")
        assert rhs_default.encode("utf-8") == GOLDEN_RUNTIME_HEALTH_DEFAULT.encode("utf-8")

        rhs_drilldown = cfg.get_drilldown_instruction("runtime_health_specialist")
        assert rhs_drilldown.encode("utf-8") == GOLDEN_RUNTIME_HEALTH_DRILLDOWN.encode("utf-8")

        # 5. IaC config specialist instructions
        iac_default = cfg.get_instruction("iac_config_specialist")
        assert iac_default.encode("utf-8") == GOLDEN_IAC_CONFIG_DEFAULT.encode("utf-8")

        iac_drilldown = cfg.get_drilldown_instruction("iac_config_specialist")
        assert iac_drilldown.encode("utf-8") == GOLDEN_IAC_CONFIG_DRILLDOWN.encode("utf-8")

        # 6. Synthesizer instruction
        synth_inst = cfg.get_instruction("synthesizer")
        assert synth_inst.encode("utf-8") == GOLDEN_SYNTHESIZER_DEFAULT.encode("utf-8")

        # 7. SRE synthesis prompt template
        sre_template = cfg.get_prompt_template("sre_synthesis")
        assert sre_template.encode("utf-8") == GOLDEN_SRE_SYNTHESIS_TEMPLATE.encode("utf-8")

    def test_live_agent_instances_byte_identity(self) -> None:
        """Verify that instantiated agent instances receive exact byte-identical instructions."""
        # Dispatcher
        disp = Dispatcher()
        assert disp.description.encode("utf-8") == GOLDEN_DISPATCHER_DESCRIPTION.encode("utf-8")

        # Triage
        triage = TriageAgent()
        assert (
            str(triage.llm_agent.instruction).encode("utf-8")
            == GOLDEN_TRIAGE_INSTRUCTION.encode("utf-8")
        )

        # Pipeline logs
        pls = PipelineLogsSpecialist()
        assert (
            str(pls.llm_agent.instruction).encode("utf-8")
            == GOLDEN_PIPELINE_LOGS_DEFAULT.encode("utf-8")
        )
        pls_bound = pls._build_agent(skill_name="test-failure-triage")
        expected_pls_bound = GOLDEN_PIPELINE_LOGS_DRILLDOWN.format(
            skill_name="test-failure-triage"
        )
        assert (
            str(pls_bound.instruction).encode("utf-8")
            == expected_pls_bound.encode("utf-8")
        )

        # Runtime health
        rhs = RuntimeHealthSpecialist()
        assert (
            str(rhs.llm_agent.instruction).encode("utf-8")
            == GOLDEN_RUNTIME_HEALTH_DEFAULT.encode("utf-8")
        )
        rhs_bound = rhs._build_agent()
        assert (
            str(rhs_bound.instruction).encode("utf-8")
            == GOLDEN_RUNTIME_HEALTH_DRILLDOWN.encode("utf-8")
        )

        # IaC config
        iac = IaCConfigSpecialist()
        assert (
            str(iac.llm_agent.instruction).encode("utf-8")
            == GOLDEN_IAC_CONFIG_DEFAULT.encode("utf-8")
        )
        iac_bound = iac._build_agent()
        assert (
            str(iac_bound.instruction).encode("utf-8")
            == GOLDEN_IAC_CONFIG_DRILLDOWN.encode("utf-8")
        )

        # Synthesizer
        synth = SynthesizerAgent()
        assert (
            str(synth.llm_agent.instruction).encode("utf-8")
            == GOLDEN_SYNTHESIZER_DEFAULT.encode("utf-8")
        )


class TestAgentYamlStartupFailures:
    """Verifies that missing or malformed YAML fails startup with clear exceptions."""

    def test_missing_yaml_raises_file_not_found(self, tmp_path: Path) -> None:
        missing_file = tmp_path / "nonexistent-agent.yaml"
        with pytest.raises(FileNotFoundError, match="agent.yaml not found"):
            load_agent_config(missing_file)

    def test_missing_yaml_via_env_var_fails_agent_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing_file = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("AGENT_YAML_PATH", str(missing_file))
        reset_agent_config()

        with pytest.raises(FileNotFoundError, match="agent.yaml not found"):
            TriageAgent()

    def test_empty_yaml_raises_value_error(self, tmp_path: Path) -> None:
        empty_file = tmp_path / "empty-agent.yaml"
        empty_file.write_text("   \n  ", encoding="utf-8")
        with pytest.raises(ValueError, match="is empty"):
            load_agent_config(empty_file)

    def test_non_mapping_yaml_raises_value_error(self, tmp_path: Path) -> None:
        list_file = tmp_path / "list-agent.yaml"
        list_file.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected mapping"):
            load_agent_config(list_file)

    def test_syntax_error_yaml_raises_value_error(self, tmp_path: Path) -> None:
        bad_syntax_file = tmp_path / "bad-syntax.yaml"
        bad_syntax_file.write_text("dispatcher:\n  [unbalanced", encoding="utf-8")
        with pytest.raises(ValueError, match="Malformed YAML"):
            load_agent_config(bad_syntax_file)

    def test_missing_required_agent_raises_value_error(self, tmp_path: Path) -> None:
        incomplete_yaml = """
agent_class: BaseAgent
name: dispatcher
description: "Orchestrator"
sub_agents:
  - agent_class: LlmAgent
    name: triage
    instruction: "Triage instruction"
prompt_templates:
  sre_synthesis: "Prompt template"
"""
        incomplete_file = tmp_path / "incomplete.yaml"
        incomplete_file.write_text(incomplete_yaml, encoding="utf-8")
        with pytest.raises(ValueError, match="Missing required agent 'synthesizer'"):
            load_agent_config(incomplete_file)

    def test_empty_instruction_raises_value_error(self, tmp_path: Path) -> None:
        bad_yaml = """
agent_class: BaseAgent
name: dispatcher
description: "Orchestrator"
sub_agents:
  - agent_class: LlmAgent
    name: triage
    instruction: "   "
  - agent_class: LlmAgent
    name: synthesizer
    instruction: "Valid instruction"
prompt_templates:
  sre_synthesis: "Valid template"
"""
        bad_file = tmp_path / "bad_instruction.yaml"
        bad_file.write_text(bad_yaml, encoding="utf-8")
        with pytest.raises(ValueError, match="Agent 'triage' must have a non-empty 'instruction'"):
            load_agent_config(bad_file)

    def test_missing_prompt_template_raises_value_error(self, tmp_path: Path) -> None:
        bad_yaml = """
agent_class: BaseAgent
name: dispatcher
description: "Orchestrator"
sub_agents:
  - agent_class: LlmAgent
    name: triage
    instruction: "Valid"
  - agent_class: LlmAgent
    name: synthesizer
    instruction: "Valid"
  - agent_class: LlmAgent
    name: pipeline_logs_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
  - agent_class: LlmAgent
    name: runtime_health_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
  - agent_class: LlmAgent
    name: iac_config_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
prompt_templates: {}
"""
        bad_file = tmp_path / "no_template.yaml"
        bad_file.write_text(bad_yaml, encoding="utf-8")
        with pytest.raises(ValueError, match="Missing required prompt template 'sre_synthesis'"):
            load_agent_config(bad_file)


class TestAgentYamlEnvExpansion:
    """Verifies environment variable substitution in agent.yaml."""

    def test_env_var_default_and_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_yaml = """
agent_class: BaseAgent
name: dispatcher
description: "Orchestrator"
sub_agents:
  - agent_class: LlmAgent
    name: triage
    model: ${TEST_MODEL:-default-model}
    instruction: "Triage instruction"
  - agent_class: LlmAgent
    name: synthesizer
    instruction: "Synth instruction"
  - agent_class: LlmAgent
    name: pipeline_logs_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
  - agent_class: LlmAgent
    name: runtime_health_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
  - agent_class: LlmAgent
    name: iac_config_specialist
    instruction: "Valid"
    drilldown_instruction: "Valid"
prompt_templates:
  sre_synthesis: "Template"
"""
        yaml_file = tmp_path / "test_env.yaml"
        yaml_file.write_text(raw_yaml, encoding="utf-8")

        # Unset env var -> uses default
        monkeypatch.delenv("TEST_MODEL", raising=False)
        cfg1 = load_agent_config(yaml_file, reload=True)
        assert cfg1.get_agent("triage").model == "default-model"

        # Set env var -> uses override
        monkeypatch.setenv("TEST_MODEL", "custom-model")
        cfg2 = load_agent_config(yaml_file, reload=True)
        assert cfg2.get_agent("triage").model == "custom-model"
