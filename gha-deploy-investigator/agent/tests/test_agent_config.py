"""Unit tests verifying externalized agent instructions in agent.yaml.

Guarantees:
1. Byte-level equality between loaded instructions, assembled prompts, and golden fixtures.
2. Startup failure when agent.yaml is missing, malformed, or references unset GEMINI_MODEL.
3. Proof that golden fixtures catch accidental edits and whitespace / .strip() mutations.
4. Live instantiated agents use byte-identical instructions.
"""

from pathlib import Path
from typing import Generator

import pytest
from agent.app.agent_config import (
    get_agent_config,
    load_agent_config,
    reset_agent_config,
)
from agent.app.config import AgentConfig as SettingsConfig
from agent.app.dispatcher import Dispatcher
from agent.app.specialists.iac_config import IaCConfigSpecialist
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import TriageAgent

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "golden_prompts"


def load_fixture(filename: str) -> str:
    """Load golden fixture text captured from base commit 8122eef, failing loudly if not found."""
    fixture_path = FIXTURES_DIR / filename
    if not fixture_path.exists():
        raise FileNotFoundError(f"Missing required golden fixture: {fixture_path}")
    return fixture_path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clean_config_cache() -> Generator[None, None, None]:
    """Ensure cache is reset before and after each test."""
    reset_agent_config()
    yield
    reset_agent_config()


class TestAgentYamlByteIdentity:
    """Zero-behaviour-change test suite asserting byte-level identity against golden fixtures."""

    def test_golden_byte_identity_loaded_from_yaml(self) -> None:
        cfg = get_agent_config()

        # 1. Dispatcher description
        assert cfg.description.encode("utf-8") == load_fixture(
            "dispatcher_description.txt"
        ).encode("utf-8")

        # 2. Triage instruction
        triage_inst = cfg.get_instruction("triage")
        assert triage_inst.encode("utf-8") == load_fixture(
            "instruction_triage.txt"
        ).encode("utf-8")

        # 3. Pipeline logs specialist instructions
        pls_default = cfg.get_instruction("pipeline_logs_specialist")
        assert pls_default.encode("utf-8") == load_fixture(
            "instruction_pipeline_logs_specialist.txt"
        ).encode("utf-8")

        pls_drilldown = cfg.get_drilldown_instruction("pipeline_logs_specialist")
        assert pls_drilldown.encode("utf-8") == load_fixture(
            "drilldown_pipeline_logs_specialist.txt"
        ).encode("utf-8")

        # 4. Runtime health specialist instructions
        rhs_default = cfg.get_instruction("runtime_health_specialist")
        assert rhs_default.encode("utf-8") == load_fixture(
            "instruction_runtime_health_specialist.txt"
        ).encode("utf-8")

        rhs_drilldown = cfg.get_drilldown_instruction("runtime_health_specialist")
        assert rhs_drilldown.encode("utf-8") == load_fixture(
            "drilldown_runtime_health_specialist.txt"
        ).encode("utf-8")

        # 5. IaC config specialist instructions
        iac_default = cfg.get_instruction("iac_config_specialist")
        assert iac_default.encode("utf-8") == load_fixture(
            "instruction_iac_config_specialist.txt"
        ).encode("utf-8")

        iac_drilldown = cfg.get_drilldown_instruction("iac_config_specialist")
        assert iac_drilldown.encode("utf-8") == load_fixture(
            "drilldown_iac_config_specialist.txt"
        ).encode("utf-8")

        # 6. Synthesizer instruction
        synth_inst = cfg.get_instruction("synthesizer")
        assert synth_inst.encode("utf-8") == load_fixture(
            "instruction_synthesizer.txt"
        ).encode("utf-8")

        # 7. SRE synthesis prompt template
        sre_template = cfg.get_prompt_template("sre_synthesis")
        assert sre_template.encode("utf-8") == load_fixture(
            "template_sre_synthesis.txt"
        ).encode("utf-8")

        # 8. Pipeline logs runner prompt template
        pls_runner = cfg.get_prompt_template("pipeline_logs_runner")
        assert pls_runner.encode("utf-8") == load_fixture(
            "template_pipeline_logs_runner.txt"
        ).encode("utf-8")

        # 9. Runtime health runner prompt template
        rhs_runner = cfg.get_prompt_template("runtime_health_runner")
        assert rhs_runner.encode("utf-8") == load_fixture(
            "template_runtime_health_runner.txt"
        ).encode("utf-8")

        # 10. IaC config runner prompt template
        iac_runner = cfg.get_prompt_template("iac_config_runner")
        assert iac_runner.encode("utf-8") == load_fixture(
            "template_iac_config_runner.txt"
        ).encode("utf-8")

    def test_assembled_prompt_byte_identity(self) -> None:
        """Verify that final assembled prompts formatted with runtime arguments
        match byte-for-byte against base commit (8122eef) fixture files."""
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
        expected_pls = load_fixture("assembled_pipeline_logs_runner.txt")
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
        expected_rhs = load_fixture("assembled_runtime_health_runner.txt")
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
        expected_iac = load_fixture("assembled_iac_config_runner.txt")
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
        expected_sre = load_fixture("assembled_sre_synthesis.txt")
        assert assembled_sre.encode("utf-8") == expected_sre.encode("utf-8")

        # 5. Pipeline logs drilldown assembled instruction
        assembled_pls_drill = cfg.get_drilldown_instruction("pipeline_logs_specialist").format(
            skill_name="test-failure-triage"
        )
        assert assembled_pls_drill.encode("utf-8") == load_fixture(
            "assembled_pipeline_logs_drilldown.txt"
        ).encode("utf-8")

        # 6. Runtime health drilldown assembled instruction
        assembled_rhs_drill = cfg.get_drilldown_instruction("runtime_health_specialist")
        assert assembled_rhs_drill.encode("utf-8") == load_fixture(
            "assembled_runtime_health_drilldown.txt"
        ).encode("utf-8")

        # 7. IaC config drilldown assembled instruction
        assembled_iac_drill = cfg.get_drilldown_instruction("iac_config_specialist")
        assert assembled_iac_drill.encode("utf-8") == load_fixture(
            "assembled_iac_config_drilldown.txt"
        ).encode("utf-8")

    def test_live_agent_instances_byte_identity(self) -> None:
        """Verify that instantiated agent instances receive exact byte-identical instructions."""
        # Dispatcher
        disp = Dispatcher()
        assert disp.description.encode("utf-8") == load_fixture(
            "dispatcher_description.txt"
        ).encode("utf-8")

        # Triage
        triage = TriageAgent()
        assert (
            str(triage.llm_agent.instruction).encode("utf-8")
            == load_fixture("instruction_triage.txt").encode("utf-8")
        )

        # Pipeline logs
        pls = PipelineLogsSpecialist()
        assert (
            str(pls.llm_agent.instruction).encode("utf-8")
            == load_fixture("instruction_pipeline_logs_specialist.txt").encode("utf-8")
        )
        pls_bound = pls._build_agent(skill_name="test-failure-triage")
        expected_pls_bound = load_fixture("assembled_pipeline_logs_drilldown.txt")
        assert (
            str(pls_bound.instruction).encode("utf-8")
            == expected_pls_bound.encode("utf-8")
        )

        # Runtime health
        rhs = RuntimeHealthSpecialist()
        assert (
            str(rhs.llm_agent.instruction).encode("utf-8")
            == load_fixture("instruction_runtime_health_specialist.txt").encode("utf-8")
        )
        rhs_bound = rhs._build_agent()
        assert (
            str(rhs_bound.instruction).encode("utf-8")
            == load_fixture("assembled_runtime_health_drilldown.txt").encode("utf-8")
        )

        # IaC config
        iac = IaCConfigSpecialist()
        assert (
            str(iac.llm_agent.instruction).encode("utf-8")
            == load_fixture("instruction_iac_config_specialist.txt").encode("utf-8")
        )
        iac_bound = iac._build_agent()
        assert (
            str(iac_bound.instruction).encode("utf-8")
            == load_fixture("assembled_iac_config_drilldown.txt").encode("utf-8")
        )

        # Synthesizer
        synth = SynthesizerAgent()
        assert (
            str(synth.llm_agent.instruction).encode("utf-8")
            == load_fixture("instruction_synthesizer.txt").encode("utf-8")
        )


class TestGoldenFixturesCatchAccidentalEditAndStrip:
    """Proves that golden fixture comparison catches accidental text edits
    and whitespace / .strip() bugs."""

    def test_golden_fixtures_catch_accidental_text_edit(self, tmp_path: Path) -> None:
        """Verify that a single character modification in instructions/prompts fails assertion."""
        # 1. Modify 1 character in triage instruction
        golden_triage = load_fixture("instruction_triage.txt")
        tampered_triage = golden_triage.replace("CI/CD Triage Agent", "CI/CD Triage Specialist")

        with pytest.raises(AssertionError):
            assert tampered_triage == golden_triage

        # 2. Modify in a temporary agent.yaml in tmp_path
        repo_root = Path(__file__).resolve().parent.parent.parent
        real_yaml = (repo_root / "agent" / "app" / "agent.yaml").read_text(encoding="utf-8")
        tampered_yaml = real_yaml.replace(
            "Analyze failure context and logs",
            "Analyze failure context, diffs, and logs",
        )
        bad_yaml_file = tmp_path / "tampered_agent.yaml"
        bad_yaml_file.write_text(tampered_yaml, encoding="utf-8")

        cfg = load_agent_config(bad_yaml_file, reload=True)
        with pytest.raises(AssertionError):
            assert cfg.get_instruction("triage") == load_fixture("instruction_triage.txt")

    def test_golden_fixtures_catch_whitespace_and_strip_regressions(
        self, tmp_path: Path
    ) -> None:
        """Verify that stripping newlines or modifying whitespace fails golden comparison."""
        # 1. Trailing newline check
        sre_fixture = load_fixture("template_sre_synthesis.txt")
        stripped_sre = sre_fixture.strip()

        # Notice that stripping removed the trailing newline!
        assert sre_fixture.endswith("\n")
        assert not stripped_sre.endswith("\n")
        with pytest.raises(AssertionError):
            assert stripped_sre == sre_fixture

        # 2. Block scalar newline preservation in agent.yaml
        repo_root = Path(__file__).resolve().parent.parent.parent
        real_yaml = (repo_root / "agent" / "app" / "agent.yaml").read_text(encoding="utf-8")
        # Replace block scalar | with |- (strip ending newline)
        tampered_yaml = real_yaml.replace(
            "  sre_synthesis: |\n",
            "  sre_synthesis: |-\n",
        )
        bad_yaml_file = tmp_path / "stripped_agent.yaml"
        bad_yaml_file.write_text(tampered_yaml, encoding="utf-8")

        cfg = load_agent_config(bad_yaml_file, reload=True)
        with pytest.raises(AssertionError):
            assert (
                cfg.get_prompt_template("sre_synthesis")
                == load_fixture("template_sre_synthesis.txt")
            )


class TestAgentYamlStartupFailures:
    """Verifies that missing or malformed YAML or missing GEMINI_MODEL fails startup."""

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

    def test_unset_gemini_model_fails_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify startup fails clearly when GEMINI_MODEL is unset."""
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        reset_agent_config()

        # 1. SettingsConfig validation fails
        with pytest.raises(Exception):
            SettingsConfig()

        # 2. agent.yaml loader fails with clear message
        with pytest.raises(
            ValueError,
            match="Referenced environment variable 'GEMINI_MODEL' is unset",
        ):
            load_agent_config(reload=True)


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
