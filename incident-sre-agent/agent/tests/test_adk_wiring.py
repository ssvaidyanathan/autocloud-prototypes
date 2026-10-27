"""Unit tests verifying Google ADK multi-agent wiring, tool segregation, and schemas."""

from datetime import datetime, timezone

from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import Report
from agent.app.dispatcher import Dispatcher
from agent.app.skills_loader import get_skill_toolset_for_target
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import InvestigationPlan, TriageAgent
from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.skill_toolset import SkillToolset


def _make_sample_incident() -> Incident:
    return Incident(
        id="inc-sample-01",
        source="servicenow",
        external_id="INC0099",
        external_key="INC0099",
        title="Payment failures on shop-api",
        description="Checkout endpoint returns 500 errors",
        severity="P1",
        service_hint="shop-api",
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
        "logs_specialist",
        "metrics_specialist",
        "changes_specialist",
        "resources_specialist",
    }


def test_triage_llm_agent_schema_and_prompt() -> None:
    triage = TriageAgent()
    assert isinstance(triage.llm_agent, LlmAgent)
    assert triage.llm_agent.name == "triage"
    assert triage.llm_agent.output_schema == InvestigationPlan
    assert "InvestigationPlan" in str(triage.llm_agent.instruction)


def test_specialists_tool_segregation_and_readonly_allowlist() -> None:
    logs_spec = LogsSpecialist()
    metrics_spec = MetricsSpecialist()
    changes_spec = ChangesSpecialist()
    resources_spec = ResourcesSpecialist()

    # Check each specialist has an LlmAgent with output_schema Findings
    for spec in [logs_spec, metrics_spec, changes_spec, resources_spec]:
        assert isinstance(spec.llm_agent, LlmAgent)
        assert spec.llm_agent.output_schema == Findings

    # 1. Logs Specialist: query_logs, query_request_logs, aggregate_requests,
    #    summarize_error_groups, list_log_names
    logs_tool_names = [t.name for t in logs_spec.get_tools()]
    assert set(logs_tool_names) == {
        "query_logs",
        "get_gke_workload_events",
        "query_request_logs",
        "aggregate_requests",
        "summarize_error_groups",
        "list_log_names",
    }
    for t in logs_spec.get_tools():
        assert isinstance(t, FunctionTool)

    # 2. Metrics Specialist: query_time_series, compare_to_baseline,
    #    list_alert_policies, list_open_alerts, query_gke_metrics
    metrics_tool_names = [t.name for t in metrics_spec.get_tools()]
    assert set(metrics_tool_names) == {
        "query_time_series",
        "compare_to_baseline",
        "list_alert_policies",
        "list_open_alerts",
        "query_gke_metrics",
    }

    # 3. Changes Specialist: query_audit_logs, diff_revisions, list_cloud_run_revisions,
    #    list_cloud_builds
    changes_tool_names = [t.name for t in changes_spec.get_tools()]
    assert set(changes_tool_names) == {
        "query_audit_logs",
        "diff_revisions",
        "list_cloud_run_revisions",
        "list_cloud_builds",
    }

    # 4. Resources Specialist: describe_cloud_run_service, get_platform_events,
    #    describe_gke_clusters, describe_compute_instances, describe_cloud_sql_instances,
    #    query_gke_workload_status, resolve_endpoint_to_gke
    resources_tool_names = [t.name for t in resources_spec.get_tools()]
    assert set(resources_tool_names) == {
        "describe_cloud_run_service",
        "get_platform_events",
        "describe_gke_clusters",
        "describe_compute_instances",
        "describe_cloud_sql_instances",
        "query_gke_workload_status",
        "resolve_endpoint_to_gke",
    }


def test_synthesizer_llm_agent_schema_and_prompt() -> None:
    synthesizer = SynthesizerAgent()
    assert isinstance(synthesizer.llm_agent, LlmAgent)
    assert synthesizer.llm_agent.name == "synthesizer"
    assert synthesizer.llm_agent.output_schema == Report
    assert "Incident Report" in str(synthesizer.llm_agent.instruction)


def test_fallback_heuristic_mode_recorded_in_report() -> None:
    incident = _make_sample_incident()
    triage = TriageAgent()
    plan = triage.plan(incident)
    assert triage.triage_mode == "heuristic_fallback"

    synthesizer = SynthesizerAgent()
    report = synthesizer.synthesize(incident=incident, plan=plan, findings={})
    assert synthesizer.synthesizer_mode == "heuristic_fallback"
    assert report.metadata.get("synthesizer_mode") == "heuristic_fallback"


def test_skill_toolset_integration_and_filtering() -> None:
    # 1. Verify unprovided service contains strictly base domain skills
    disp_toolset = get_skill_toolset_for_target("dispatcher")
    assert isinstance(disp_toolset, SkillToolset)
    disp_skill_names = [s.name for s in disp_toolset.skills]
    assert "triage-playbook" in disp_skill_names
    assert "service-shop-api" not in disp_skill_names

    logs_base = get_skill_toolset_for_target("logs")
    assert [s.name for s in logs_base.skills] == ["cloud-logging-queries"]

    # 2. Verify dynamic runtime resolution with service_name="shop-api"
    disp_toolset_svc = get_skill_toolset_for_target("dispatcher", service_name="shop-api")
    disp_svc_skill_names = [s.name for s in disp_toolset_svc.skills]
    assert "triage-playbook" in disp_svc_skill_names
    assert "service-shop-api" in disp_svc_skill_names

    # Verify logs specialist skill toolset with dynamic resolution
    logs_toolset = get_skill_toolset_for_target("logs", service_name="shop-api")
    assert isinstance(logs_toolset, SkillToolset)
    logs_skill_names = [s.name for s in logs_toolset.skills]
    assert set(logs_skill_names) == {"cloud-logging-queries", "service-shop-api"}

    # Verify metrics specialist skill toolset with dynamic resolution
    metrics_toolset = get_skill_toolset_for_target("metrics", service_name="shop-api")
    assert isinstance(metrics_toolset, SkillToolset)
    metrics_skill_names = [s.name for s in metrics_toolset.skills]
    assert set(metrics_skill_names) == {"cloud-monitoring-metrics", "service-shop-api"}

    # Verify changes specialist skill toolset with dynamic resolution
    changes_toolset = get_skill_toolset_for_target("changes", service_name="shop-api")
    assert isinstance(changes_toolset, SkillToolset)
    changes_skill_names = [s.name for s in changes_toolset.skills]
    assert set(changes_skill_names) == {
        "change-correlation",
        "k8s-change-correlation",
        "service-shop-api",
    }

    # Verify synthesizer skill toolset with dynamic resolution
    synth_toolset = get_skill_toolset_for_target("synthesizer", service_name="shop-api")
    assert isinstance(synth_toolset, SkillToolset)
    synth_skill_names = [s.name for s in synth_toolset.skills]
    assert set(synth_skill_names) == {"report-style", "service-shop-api"}
