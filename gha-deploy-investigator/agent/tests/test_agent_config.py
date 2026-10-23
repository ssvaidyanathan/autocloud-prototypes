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

GOLDEN_PIPELINE_LOGS_RUNNER_TEMPLATE = (
    "Investigate pipeline failure for incident {incident_id}: {incident_title}\n"
    "Job: {job_name} (ID: {job_id}), Step: {step_name}\n"
    "Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}\n"
    "Failing Step Sub-Stage: {sub_stage}\n"
    "Error log excerpt:\n"
    "{log_excerpt}\n\n"
    "CRITICAL INSTRUCTIONS:\n"
    "1. Turn 1: Invoke load_skill exactly once with skill_name='{skill_name}'.\n"
    "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect logs or diff.\n"
    "3. Turn 3: Conclude immediately by invoking set_model_response with Findings."
)

GOLDEN_RUNTIME_HEALTH_RUNNER_TEMPLATE = (
    "Investigate Cloud Run deploy failure for incident {incident_id}: {incident_title}\n"
    "Job: {job_name} (ID: {job_id}), Step: {step_name}\n"
    "Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}\n"
    "Target Service: {service_name}, Region: {region}\n"
    "Revision: {revision_name}\n"
    "Runner Log Excerpt:\n"
    "{log_excerpt}\n\n"
    "CRITICAL INSTRUCTIONS:\n"
    "1. Turn 1: Invoke load_skill exactly once with "
    "skill_name='cloudrun-deploy-troubleshooting'.\n"
    "2. Turn 2: Invoke get_commit_diff(repo, head_sha) to inspect commit changes, "
    "fetch_job_logs(repo, job_id) to inspect deployment logs, and/or describe_service / "
    "describe_cloud_run_revision for runtime telemetry.\n"
    "3. Turn 3: Conclude immediately by invoking set_model_response with your Findings report."
)

GOLDEN_IAC_CONFIG_RUNNER_TEMPLATE = (
    "Investigate IaC failure for incident {incident_id}: {incident_title}\n"
    "Job: {job_name}, Step: {step_name}\n"
    "Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}\n"
    "Log Excerpt:\n"
    "{log_excerpt}\n\n"
    "CRITICAL INSTRUCTIONS:\n"
    "1. Turn 1: Invoke load_skill exactly once with "
    "skill_name='cloudrun-deploy-troubleshooting'.\n"
    "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect IaC changes.\n"
    "3. Turn 3: Conclude immediately by invoking set_model_response with your Findings report."
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

        # 8. Pipeline logs runner prompt template
        pls_runner = cfg.get_prompt_template("pipeline_logs_runner")
        assert pls_runner.encode("utf-8") == GOLDEN_PIPELINE_LOGS_RUNNER_TEMPLATE.encode("utf-8")

        # 9. Runtime health runner prompt template
        rhs_runner = cfg.get_prompt_template("runtime_health_runner")
        assert rhs_runner.encode("utf-8") == GOLDEN_RUNTIME_HEALTH_RUNNER_TEMPLATE.encode("utf-8")

        # 10. IaC config runner prompt template
        iac_runner = cfg.get_prompt_template("iac_config_runner")
        assert iac_runner.encode("utf-8") == GOLDEN_IAC_CONFIG_RUNNER_TEMPLATE.encode("utf-8")

    def test_assembled_prompt_byte_identity(self) -> None:
        """Verify that final assembled prompts formatted with runtime arguments
        match byte-for-byte against base commit (8122eef) expected prompt strings."""
        cfg = get_agent_config()

        # 1. Pipeline logs runner assembled prompt
        pls_tmpl = cfg.get_prompt_template("pipeline_logs_runner")
        pls_kwargs = {
            "incident_id": 101,
            "incident_title": "Lint failure in CI",
            "job_name": "lint-job",
            "job_id": 12345,
            "step_name": "ruff check",
            "repo": "owner/repo",
            "run_id": 67890,
            "head_sha": "abc1234def5678",
            "sub_stage": "lint",
            "log_excerpt": "error: expected token\nline 42",
            "skill_name": "lint-troubleshooting",
        }
        assembled_pls = pls_tmpl.format(**pls_kwargs)
        expected_pls = "\n".join([
            (
                f"Investigate pipeline failure for incident {pls_kwargs['incident_id']}: "
                f"{pls_kwargs['incident_title']}"
            ),
            (
                f"Job: {pls_kwargs['job_name']} (ID: {pls_kwargs['job_id']}), "
                f"Step: {pls_kwargs['step_name']}"
            ),
            (
                f"Repository: {pls_kwargs['repo']}, Run ID: {pls_kwargs['run_id']}, "
                f"Head SHA: {pls_kwargs['head_sha']}"
            ),
            f"Failing Step Sub-Stage: {pls_kwargs['sub_stage']}",
            f"Error log excerpt:\n{pls_kwargs['log_excerpt']}",
            "",
            "CRITICAL INSTRUCTIONS:",
            (
                f"1. Turn 1: Invoke load_skill exactly once with "
                f"skill_name='{pls_kwargs['skill_name']}'."
            ),
            "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect logs or diff.",
            "3. Turn 3: Conclude immediately by invoking set_model_response with Findings.",
        ])
        assert assembled_pls.encode("utf-8") == expected_pls.encode("utf-8")

        # 2. Runtime health runner assembled prompt
        rhs_tmpl = cfg.get_prompt_template("runtime_health_runner")
        rhs_kwargs = {
            "incident_id": 202,
            "incident_title": "Cloud Run container OOM",
            "job_name": "deploy",
            "job_id": 54321,
            "step_name": "deploy-to-cloud-run",
            "repo": "owner/repo",
            "run_id": 98765,
            "head_sha": "def5678abc1234",
            "service_name": "order-service",
            "region": "us-central1",
            "revision_name": "order-service-00002",
            "log_excerpt": "Memory limit of 512MiB exceeded with 513MiB used.",
        }
        assembled_rhs = rhs_tmpl.format(**rhs_kwargs)
        expected_rhs = "\n".join([
            (
                f"Investigate Cloud Run deploy failure for incident {rhs_kwargs['incident_id']}: "
                f"{rhs_kwargs['incident_title']}"
            ),
            (
                f"Job: {rhs_kwargs['job_name']} (ID: {rhs_kwargs['job_id']}), "
                f"Step: {rhs_kwargs['step_name']}"
            ),
            (
                f"Repository: {rhs_kwargs['repo']}, Run ID: {rhs_kwargs['run_id']}, "
                f"Head SHA: {rhs_kwargs['head_sha']}"
            ),
            f"Target Service: {rhs_kwargs['service_name']}, Region: {rhs_kwargs['region']}",
            f"Revision: {rhs_kwargs['revision_name']}",
            f"Runner Log Excerpt:\n{rhs_kwargs['log_excerpt']}",
            "",
            "CRITICAL INSTRUCTIONS:",
            (
                "1. Turn 1: Invoke load_skill exactly once with "
                "skill_name='cloudrun-deploy-troubleshooting'."
            ),
            (
                "2. Turn 2: Invoke get_commit_diff(repo, head_sha) to inspect commit changes, "
                "fetch_job_logs(repo, job_id) to inspect deployment logs, "
                "and/or describe_service / describe_cloud_run_revision for runtime telemetry."
            ),
            (
                "3. Turn 3: Conclude immediately by invoking set_model_response "
                "with your Findings report."
            ),
        ])
        assert assembled_rhs.encode("utf-8") == expected_rhs.encode("utf-8")

        # 3. IaC config runner assembled prompt
        iac_tmpl = cfg.get_prompt_template("iac_config_runner")
        iac_kwargs = {
            "incident_id": 303,
            "incident_title": "Terraform syntax error",
            "job_name": "iac",
            "step_name": "terraform apply",
            "repo": "owner/repo",
            "run_id": 11223,
            "head_sha": "7890abcdef1234",
            "log_excerpt": "Error: Invalid reference in main.tf line 15",
        }
        assembled_iac = iac_tmpl.format(**iac_kwargs)
        expected_iac = "\n".join([
            (
                f"Investigate IaC failure for incident {iac_kwargs['incident_id']}: "
                f"{iac_kwargs['incident_title']}"
            ),
            f"Job: {iac_kwargs['job_name']}, Step: {iac_kwargs['step_name']}",
            (
                f"Repository: {iac_kwargs['repo']}, Run ID: {iac_kwargs['run_id']}, "
                f"Head SHA: {iac_kwargs['head_sha']}"
            ),
            f"Log Excerpt:\n{iac_kwargs['log_excerpt']}",
            "",
            "CRITICAL INSTRUCTIONS:",
            (
                "1. Turn 1: Invoke load_skill exactly once with "
                "skill_name='cloudrun-deploy-troubleshooting'."
            ),
            "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect IaC changes.",
            (
                "3. Turn 3: Conclude immediately by invoking set_model_response "
                "with your Findings report."
            ),
        ])
        assert assembled_iac.encode("utf-8") == expected_iac.encode("utf-8")

        # 4. SRE synthesis assembled prompt
        sre_tmpl = cfg.get_prompt_template("sre_synthesis")
        sre_kwargs = {
            "job_name": "test-suite",
            "step_name": "pytest",
            "findings_context": (
                "\nSpecialist Analysis & Findings:\n- Probable cause: test assertion\n"
            ),
            "log_excerpt": "FAILED tests/test_math.py::test_add - assert 1 + 1 == 3",
        }
        assembled_sre = sre_tmpl.format(**sre_kwargs)
        expected_sre = GOLDEN_SRE_SYNTHESIS_TEMPLATE.format(**sre_kwargs)
        assert assembled_sre.encode("utf-8") == expected_sre.encode("utf-8")

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
        base_yaml = """
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
prompt_templates:
  sre_synthesis: "Valid"
  pipeline_logs_runner: "Valid"
  runtime_health_runner: "Valid"
  iac_config_runner: "Valid"
"""
        for missing_tmpl in [
            "sre_synthesis",
            "pipeline_logs_runner",
            "runtime_health_runner",
            "iac_config_runner",
        ]:
            bad_yaml = base_yaml.replace(f"  {missing_tmpl}: \"Valid\"\n", "")
            bad_file = tmp_path / f"no_{missing_tmpl}.yaml"
            bad_file.write_text(bad_yaml, encoding="utf-8")
            with pytest.raises(
                ValueError,
                match=f"Missing required prompt template '{missing_tmpl}'",
            ):
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
  pipeline_logs_runner: "Template"
  runtime_health_runner: "Template"
  iac_config_runner: "Template"
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


class TestAstPromptGuard:
    """Verifies that the AST static check enforces zero hardcoded instructions and prompts."""

    @pytest.fixture
    def guard_module(self) -> object:
        import importlib.util

        script_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "check-no-hardcoded-prompts.py"
        )
        spec = importlib.util.spec_from_file_location("check_no_hardcoded_prompts", script_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_zero_hardcoded_prompts_in_agent_app(self, guard_module: object) -> None:
        """Verify that check_file_ast finds zero violations across all agent/app Python files."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        app_dir = repo_root / "gha-deploy-investigator" / "agent" / "app"
        if not app_dir.exists():
            app_dir = repo_root / "agent" / "app"

        files = guard_module.find_agent_python_files(app_dir)  # type: ignore[attr-defined]
        assert len(files) > 0, f"Found no files to scan in {app_dir}"

        all_violations = []
        for f in files:
            violations = guard_module.check_file_ast(f)  # type: ignore[attr-defined]
            for line_no, msg in violations:
                all_violations.append(f"{f.name}:{line_no}: {msg}")

        assert not all_violations, (
            "Hardcoded instructions or prompt templates found:\n"
            + "\n".join(all_violations)
        )

    def test_guard_catches_hardcoded_instruction_in_call(
        self, guard_module: object, tmp_path: Path
    ) -> None:
        """Verify that passing string literal as instruction= triggers a violation."""
        snippet = tmp_path / "bad_kw.py"
        snippet.write_text(
            'agent = LlmAgent(name="test", instruction="You are an agent.")\n',
            encoding="utf-8",
        )
        violations = guard_module.check_file_ast(snippet)  # type: ignore[attr-defined]
        assert len(violations) == 1
        assert "instruction" in violations[0][1]

    def test_guard_catches_hardcoded_prompt_in_runner_call(
        self, guard_module: object, tmp_path: Path
    ) -> None:
        """Verify that passing string literal / f-string to _execute_adk_runner triggers."""
        snippet = tmp_path / "bad_runner.py"
        snippet.write_text(
            'run_sync(self._execute_adk_runner(agent, f"Investigate {incident}"))\n',
            encoding="utf-8",
        )
        violations = guard_module.check_file_ast(snippet)  # type: ignore[attr-defined]
        assert len(violations) == 1
        assert "_execute_adk_runner" in violations[0][1]

    def test_guard_catches_hardcoded_prompt_variable_assignment(
        self, guard_module: object, tmp_path: Path
    ) -> None:
        """Verify that assigning string literal or f-string to prompt variable triggers."""
        snippet = tmp_path / "bad_assign.py"
        snippet.write_text(
            'prompt_lines = [\n    f"Investigate {incident}",\n    "CRITICAL INSTRUCTIONS:",\n]\n',
            encoding="utf-8",
        )
        violations = guard_module.check_file_ast(snippet)  # type: ignore[attr-defined]
        assert len(violations) == 1
        assert "prompt_lines" in violations[0][1]
