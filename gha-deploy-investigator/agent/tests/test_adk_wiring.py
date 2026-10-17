"""Unit tests verifying Google ADK multi-agent wiring, tool segregation, and schemas."""

from datetime import datetime, timezone

import pytest
from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import Report
from agent.app.dispatcher import Dispatcher
from agent.app.skills_loader import get_skill_toolset_for_target
from agent.app.specialists.iac_config import IaCConfigSpecialist
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.tools.registry import default_registry
from agent.app.triage import InvestigationPlan, TriageAgent
from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.skill_toolset import SkillToolset


def _make_sample_incident() -> Incident:
    return Incident(
        id="inc-sample-01",
        source="github",
        external_id="1849201948",
        external_key="1849201948",
        title="Workflow failure in my-org/sample-app",
        description="Job test failed with exit code 1",
        severity="P2",
        service_hint="sample-app",
        opened_at=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
    )


def test_dispatcher_adk_base_agent_hierarchy() -> None:
    dispatcher = Dispatcher()
    assert isinstance(dispatcher, BaseAgent)
    assert dispatcher.name == "dispatcher"

    sub_agent_names = [agent.name for agent in dispatcher.sub_agents]
    assert "triage" in sub_agent_names
    assert "specialists_parallel" in sub_agent_names
    assert "synthesizer" in sub_agent_names

    # Verify parallel agent
    assert isinstance(dispatcher.parallel_agent, ParallelAgent)
    parallel_sub_names = [sub.name for sub in dispatcher.parallel_agent.sub_agents]
    assert set(parallel_sub_names) == {
        "pipeline_logs_specialist",
        "runtime_health_specialist",
        "iac_config_specialist",
    }


def test_triage_llm_agent_schema_and_prompt() -> None:
    triage = TriageAgent()
    assert isinstance(triage.llm_agent, LlmAgent)
    assert triage.llm_agent.name == "triage"
    assert triage.llm_agent.output_schema == InvestigationPlan
    assert "InvestigationPlan" in str(triage.llm_agent.instruction)


def test_specialists_tool_segregation_and_readonly_allowlist() -> None:
    logs_spec = PipelineLogsSpecialist()
    runtime_spec = RuntimeHealthSpecialist()
    iac_spec = IaCConfigSpecialist()

    # Check each specialist has an LlmAgent with output_schema Findings
    for spec in [logs_spec, runtime_spec, iac_spec]:
        assert isinstance(spec.llm_agent, LlmAgent)
        assert spec.llm_agent.output_schema == Findings

    # 1. Pipeline Logs Specialist tools
    logs_tool_names = [t.name for t in logs_spec.get_tools()]
    assert set(logs_tool_names) == {
        "fetch_job_logs",
        "fetch_workflow_run_jobs",
        "get_commit_diff",
    }
    for t in logs_spec.get_tools():
        assert isinstance(t, FunctionTool)

    # 2. Runtime Health Specialist tools
    runtime_tool_names = [t.name for t in runtime_spec.get_tools()]
    assert set(runtime_tool_names) == {
        "fetch_job_logs",
        "fetch_workflow_run_jobs",
        "get_commit_diff",
        "describe_cloud_run_revision",
        "query_cloud_logging",
        "describe_service",
    }
    for t in runtime_spec.get_tools():
        assert isinstance(t, FunctionTool)

    # 3. IaC Config Specialist tools
    iac_tool_names = [t.name for t in iac_spec.get_tools()]
    assert set(iac_tool_names) == {
        "fetch_job_logs",
        "fetch_workflow_run_jobs",
        "get_commit_diff",
    }
    for t in iac_spec.get_tools():
        assert isinstance(t, FunctionTool)

    # Strictly enforce zero mutating tools
    default_registry.assert_all_readonly()


def test_synthesizer_llm_agent_schema_and_prompt() -> None:
    synthesizer = SynthesizerAgent()
    assert isinstance(synthesizer.llm_agent, LlmAgent)
    assert synthesizer.llm_agent.name == "synthesizer"
    assert synthesizer.llm_agent.output_schema == Report
    assert "Report" in str(synthesizer.llm_agent.instruction)


def test_fallback_heuristic_mode_recorded_in_report(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.app.llm
    monkeypatch.setattr(agent.app.llm, "generate_gemini_synthesis", lambda **kwargs: None)

    incident = _make_sample_incident()
    triage = TriageAgent()
    plan = triage.plan(incident)
    assert triage.triage_mode == "heuristic_fallback"

    synthesizer = SynthesizerAgent()
    report = synthesizer.synthesize(incident=incident, plan=plan)
    assert synthesizer.synthesizer_mode == "heuristic_fallback"
    assert report.metadata.get("synthesizer_mode") == "heuristic_fallback"



def test_skill_toolset_integration_and_filtering() -> None:
    # 1. Dispatcher toolset
    disp_toolset = get_skill_toolset_for_target("dispatcher")
    assert isinstance(disp_toolset, SkillToolset)
    disp_skill_names = [s.name for s in disp_toolset.skills]
    assert "lint-troubleshooting" in disp_skill_names
    assert "cloudrun-deploy-troubleshooting" in disp_skill_names

    # 2. Pipeline logs specialist toolset
    logs_toolset = get_skill_toolset_for_target("pipeline_logs")
    assert isinstance(logs_toolset, SkillToolset)
    logs_skill_names = [s.name for s in logs_toolset.skills]
    assert "lint-troubleshooting" in logs_skill_names
    assert "test-failure-triage" in logs_skill_names
    assert "container-build-troubleshooting" in logs_skill_names

    # 3. Runtime health specialist toolset
    runtime_toolset = get_skill_toolset_for_target("runtime_health")
    assert isinstance(runtime_toolset, SkillToolset)
    assert [s.name for s in runtime_toolset.skills] == ["cloudrun-deploy-troubleshooting"]

    # 4. IaC config specialist toolset
    iac_toolset = get_skill_toolset_for_target("iac_config")
    assert isinstance(iac_toolset, SkillToolset)
    assert [s.name for s in iac_toolset.skills] == ["cloudrun-deploy-troubleshooting"]
