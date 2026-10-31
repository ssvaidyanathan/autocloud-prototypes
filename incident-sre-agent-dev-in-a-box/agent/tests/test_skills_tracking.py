"""Unit tests for Stage 2 skill tracking and dynamic runbook resolution."""

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import Report
from agent.app.skills_loader import SkillsLoader, get_skill_toolset_for_target
from agent.app.specialists.base import (
    BaseSpecialist,
    SkillTracker,
)
from agent.app.specialists.logs import LogsSpecialist
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import InvestigationPlan
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types


def _make_incident(service_hint: Optional[str] = "shop-api") -> Incident:
    return Incident(
        id="inc-test-01",
        source="generic",
        external_id="EXT-001",
        external_key="INC-001",
        title="Test Incident",
        description="High 5xx error rate",
        severity="P1",
        service_hint="shop-api",
        opened_at=datetime.now(timezone.utc),
    )


def _make_plan() -> InvestigationPlan:
    now = datetime.now(timezone.utc)
    return InvestigationPlan(
        affected_service="shop-api",
        symptoms=["5xx errors"],
        selected_specialists=["logs", "metrics"],
        start_time=now,
        end_time=now,
    )


def test_contracts_byte_identical_and_fields() -> None:
    """Verify check-contracts.sh passes and Findings contract has new fields."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    check_script = repo_root / "scripts" / "check-contracts.sh"

    res = subprocess.run(["bash", str(check_script)], capture_output=True, text=True)
    assert res.returncode == 0, f"check-contracts.sh failed: {res.stderr}"

    # Default values check
    default_findings = Findings(summary="test summary", confidence=0.8)
    assert default_findings.skills_used == []
    assert default_findings.skill_first is False
    assert default_findings.fallback_used is False

    # Explicit values check
    custom_findings = Findings(
        summary="custom summary",
        confidence=0.9,
        skills_used=["cloud-logging-queries", "service-shop-api"],
        skill_first=True,
        fallback_used=True,
    )
    assert custom_findings.skills_used == ["cloud-logging-queries", "service-shop-api"]
    assert custom_findings.skill_first is True
    assert custom_findings.fallback_used is True

    # JSON roundtrip
    restored = Findings.model_validate_json(custom_findings.model_dump_json())
    assert restored == custom_findings


def test_skill_toolset_creation_dynamic_service_runbook_existing() -> None:
    """Verify SkillToolset creation dynamically resolves existing service runbook."""
    toolset = get_skill_toolset_for_target("logs", service_name="shop-api")
    assert isinstance(toolset, SkillToolset)
    skill_names = [s.name for s in toolset.skills]
    assert "cloud-logging-queries" in skill_names
    assert "service-shop-api" in skill_names


def test_skill_toolset_creation_dynamic_service_runbook_temp_dir(tmp_path: Path) -> None:
    """Verify dynamic service runbook resolution with non-hardcoded service names."""
    # Create mock skills directory with skills.yaml without any static 'all'
    skills_yaml = tmp_path / "skills.yaml"
    skills_yaml.write_text(
        "logs:\n  - domain-logging\nmetrics:\n  - domain-metrics\n",
        encoding="utf-8",
    )

    domain_skill = tmp_path / "domain-logging"
    domain_skill.mkdir()
    (domain_skill / "SKILL.md").write_text(
        "---\nname: domain-logging\ndescription: Logging skill\n---\n# Instructions",
        encoding="utf-8",
    )

    custom_service_skill = tmp_path / "service-custom-auth"
    custom_service_skill.mkdir()
    (custom_service_skill / "SKILL.md").write_text(
        "---\nname: service-custom-auth\ndescription: Custom auth runbook\n---\n# Auth Runbook",
        encoding="utf-8",
    )

    # 1. Without service_name: only domain skill
    toolset_no_svc = get_skill_toolset_for_target("logs", service_name=None, skills_dir=tmp_path)
    assert isinstance(toolset_no_svc, SkillToolset)
    skill_names_no_svc = [s.name for s in toolset_no_svc.skills]
    assert "domain-logging" in skill_names_no_svc
    assert "service-custom-auth" not in skill_names_no_svc

    # 2. With service_name="custom-auth": dynamically resolves service-custom-auth
    toolset_with_svc = get_skill_toolset_for_target(
        "logs", service_name="custom-auth", skills_dir=tmp_path
    )
    assert isinstance(toolset_with_svc, SkillToolset)
    skill_names_with_svc = [s.name for s in toolset_with_svc.skills]
    assert "domain-logging" in skill_names_with_svc
    assert "service-custom-auth" in skill_names_with_svc

    # 3. With service_name already having 'service-' prefix
    toolset_prefix = get_skill_toolset_for_target(
        "logs", service_name="service-custom-auth", skills_dir=tmp_path
    )
    skill_names_prefix = [s.name for s in toolset_prefix.skills]
    assert "service-custom-auth" in skill_names_prefix

    # 4. Check SkillsLoader.get_skills_for_target
    loader = SkillsLoader(skills_dir=tmp_path)
    metadata_list = loader.get_skills_for_target("logs", service_name="custom-auth")
    meta_names = [m.name for m in metadata_list]
    assert "domain-logging" in meta_names
    assert "service-custom-auth" in meta_names


def test_after_tool_callback_captures_loaded_skills() -> None:
    """Verify after_tool_callback captures loaded skill names and deduplicates."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"

    tool_other = MagicMock()
    tool_other.name = "query_logs"

    # Turn 1: load_skill with 'cloud-logging-queries'
    res = tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "cloud-logging-queries"},
        tool_context=None,
        tool_output=None,
    )
    assert res is None
    assert tracker.skills_used == ["cloud-logging-queries"]

    # Turn 2: regular tool call (should not add to skills_used)
    tracker.after_tool_callback(
        tool_other,
        tool_args={"service": "shop-api"},
        tool_context=None,
        tool_output=None,
    )
    assert tracker.skills_used == ["cloud-logging-queries"]

    # Turn 3: load_skill with 'service-shop-api'
    tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "service-shop-api"},
        tool_context=None,
        tool_output=None,
    )
    assert tracker.skills_used == ["cloud-logging-queries", "service-shop-api"]

    # Turn 4: load_skill duplicate (deduplication check)
    tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "cloud-logging-queries"},
        tool_context=None,
        tool_output=None,
    )
    assert tracker.skills_used == ["cloud-logging-queries", "service-shop-api"]

    # Test ADK keyword arguments pattern: (tool=..., args=...)
    tracker.after_tool_callback(
        tool=tool_load,
        args={"skill_name": "new-domain-skill"},
        tool_context=None,
        tool_response={"status": "ok"},
    )
    assert tracker.skills_used == [
        "cloud-logging-queries",
        "service-shop-api",
        "new-domain-skill",
    ]


def test_skill_first_tracking_turn_one_load_skill() -> None:
    """Verify skill_first is True when Turn 1 is load_skill."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    tool_query = MagicMock()
    tool_query.name = "query_logs"

    # Turn 1: load_skill
    tracker.after_tool_callback(tool_load, tool_args={"skill_name": "cloud-logging-queries"})
    assert tracker.skill_first is True

    # Turn 2: query_logs
    tracker.after_tool_callback(tool_query, tool_args={"service": "shop-api"})
    assert tracker.skill_first is True

    # Findings population
    f = Findings(summary="logs", confidence=0.8)
    populated = tracker.populate_findings(f)
    assert populated.skill_first is True
    assert populated.skills_used == ["cloud-logging-queries"]


def test_skill_first_tracking_turn_one_not_load_skill() -> None:
    """Verify skill_first is False when Turn 1 is NOT load_skill."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    tool_query = MagicMock()
    tool_query.name = "query_logs"

    # Turn 1: query_logs (NOT load_skill)
    tracker.after_tool_callback(tool_query, tool_args={"service": "shop-api"})
    assert tracker.skill_first is False

    # Turn 2: load_skill
    tracker.after_tool_callback(tool_load, tool_args={"skill_name": "cloud-logging-queries"})
    # Even though a skill was loaded on Turn 2, skill_first MUST remain False
    assert tracker.skill_first is False
    assert tracker.skills_used == ["cloud-logging-queries"]

    # Findings population
    f = Findings(summary="logs", confidence=0.8)
    populated = tracker.populate_findings(f)
    assert populated.skill_first is False
    assert populated.skills_used == ["cloud-logging-queries"]


def test_skill_first_tracking_no_tool_calls() -> None:
    """Verify skill_first is False when no tool calls are made."""
    tracker = SkillTracker()
    assert tracker.skill_first is False
    assert tracker.skills_used == []

    f = Findings(summary="logs", confidence=0.8)
    populated = tracker.populate_findings(f)
    assert populated.skill_first is False
    assert populated.skills_used == []


@pytest.mark.asyncio
async def test_concurrent_specialists_skill_isolation() -> None:
    """Verify 2+ specialists running concurrently with asyncio.gather maintain
    strictly isolated SkillTracker instances with zero crosstalk in skills_used.
    """

    class StubSpecialistLlm(BaseLlm):
        model: str = "stub"
        _skill_to_load: str = ""
        _turn: int = 0

        def __init__(self, skill_to_load: str) -> None:
            super().__init__(model="stub")
            self._skill_to_load = skill_to_load
            self._turn = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            if self._turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="load_skill",
                                args={"skill_name": self._skill_to_load},
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text=(
                                    f'{{"summary": "Analysis for {self._skill_to_load}", '
                                    f'"confidence": 0.85}}'
                                )
                            )
                        ],
                    )
                )

    fake_client = MagicMock()
    fake_client.list_entries.return_value = []
    fake_client.list_group_stats.return_value = []

    # Specialist 1: loads cloud-logging-queries
    spec1 = LogsSpecialist(
        project_id="proj-1",
        model=StubSpecialistLlm("cloud-logging-queries"),
        client=fake_client,
    )

    # Specialist 2: loads service-shop-api
    spec2 = LogsSpecialist(
        project_id="proj-2",
        model=StubSpecialistLlm("service-shop-api"),
        client=fake_client,
    )

    # Specialist 3: loads cloud-monitoring-metrics
    spec3 = LogsSpecialist(
        project_id="proj-3",
        model=StubSpecialistLlm("cloud-monitoring-metrics"),
        client=fake_client,
    )

    inc1 = _make_incident("svc-1")
    inc2 = _make_incident("svc-2")
    inc3 = _make_incident("svc-3")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    # Run 3 specialists concurrently with asyncio.gather
    res1, res2, res3 = await asyncio.gather(
        spec1.run_investigation_async(incident=inc1, start_time=start, end_time=now),
        spec2.run_investigation_async(incident=inc2, start_time=start, end_time=now),
        spec3.run_investigation_async(incident=inc3, start_time=start, end_time=now),
    )

    # Assert complete isolation with zero crosstalk in tracker and findings
    assert spec1.skill_tracker.skills_used == ["cloud-logging-queries"]
    assert res1.skills_used == ["cloud-logging-queries"]
    assert res1.skill_first is True

    assert spec2.skill_tracker.skills_used == ["service-shop-api"]
    assert res2.skills_used == ["service-shop-api"]
    assert res2.skill_first is True

    assert spec3.skill_tracker.skills_used == ["cloud-monitoring-metrics"]
    assert res3.skills_used == ["cloud-monitoring-metrics"]
    assert res3.skill_first is True

    # Assert no cross-contamination between specialists
    assert "service-shop-api" not in spec1.skill_tracker.skills_used
    assert "cloud-monitoring-metrics" not in spec1.skill_tracker.skills_used
    assert "cloud-logging-queries" not in spec2.skill_tracker.skills_used
    assert "cloud-monitoring-metrics" not in spec2.skill_tracker.skills_used
    assert "cloud-logging-queries" not in spec3.skill_tracker.skills_used
    assert "service-shop-api" not in spec3.skill_tracker.skills_used

    # Assert distinct SkillTracker instances
    assert spec1.skill_tracker is not spec2.skill_tracker
    assert spec2.skill_tracker is not spec3.skill_tracker
    assert spec1.skill_tracker is not spec3.skill_tracker


def test_specialist_skill_tracking_and_findings_population() -> None:
    """Verify BaseSpecialist tracking callbacks and Findings population."""
    specialist = LogsSpecialist(project_id="test-proj")
    assert hasattr(specialist, "skill_tracker")
    assert isinstance(specialist.skill_tracker, SkillTracker)

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    specialist.after_tool_callback(tool_load, tool_args={"skill_name": "cloud-logging-queries"})
    assert specialist.skill_tracker.skill_first is True
    assert specialist.skill_tracker.skills_used == ["cloud-logging-queries"]

    # create_findings reads tracker state
    findings = specialist.create_findings(
        summary="Logs analyzed",
        evidence=[EvidenceItem(kind="log", excerpt="error 500")],
        confidence=0.85,
    )
    assert findings.skill_first is True
    assert findings.skills_used == ["cloud-logging-queries"]
    assert findings.fallback_used is False


def test_specialist_failure_sets_fallback_used_and_preserves_skills() -> None:
    """Verify specialist failure populates fallback_used=True and preserves tracked skills."""

    class FailingSpecialist(BaseSpecialist):
        def run_investigation(
            self,
            incident: Incident,
            start_time: datetime,
            end_time: datetime,
            **kwargs: Any,
        ) -> Findings:
            raise RuntimeError("API timeout")

    spec = FailingSpecialist(name="FailingSpecialist", description="Fails on purpose")
    tool_load = MagicMock()
    tool_load.name = "load_skill"
    spec.after_tool_callback(tool_load, tool_args={"skill_name": "partial-skill"})

    incident = _make_incident()
    findings = spec.investigate(incident)

    assert "FailingSpecialist investigation failed" in findings.summary
    assert findings.fallback_used is True
    assert findings.skill_first is True
    assert findings.skills_used == ["partial-skill"]


def test_synthesizer_merges_specialist_skills_into_report() -> None:
    """Verify Synthesizer merges specialist f.skills_used into Report.skills_used."""
    loader = SkillsLoader()
    synthesizer = SynthesizerAgent(skills_loader=loader)

    incident = _make_incident()
    plan = _make_plan()

    findings = {
        "logs": Findings(
            summary="Logs findings",
            confidence=0.8,
            skills_used=["cloud-logging-queries", "service-shop-api"],
            skill_first=True,
        ),
        "metrics": Findings(
            summary="Metrics findings",
            confidence=0.85,
            skills_used=["cloud-monitoring-metrics"],
            skill_first=True,
        ),
        "changes": Findings(
            summary="Changes findings",
            confidence=0.7,
            skills_used=[],
        ),
    }

    # 1. Synthesize with triage skills passed explicitly
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
        skills_used=["triage-playbook"],
    )

    assert isinstance(report, Report)
    expected_skills = [
        "triage-playbook",
        "cloud-logging-queries",
        "service-shop-api",
        "cloud-monitoring-metrics",
        "report-style",
    ]
    assert report.skills_used == expected_skills

    # 2. Synthesize without passing triage skills_used (defaults)
    report_no_triage = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
        skills_used=None,
    )
    expected_no_triage = [
        "cloud-logging-queries",
        "service-shop-api",
        "cloud-monitoring-metrics",
        "report-style",
    ]
    assert report_no_triage.skills_used == expected_no_triage


def test_skill_tracker_before_tool_callback_budget_enforcement() -> None:
    """Verify before_tool_callback returns status='complete' dictionary without 'error' key."""
    tracker = SkillTracker(max_tool_calls=3)
    dummy_tool = MagicMock()
    dummy_tool.name = "query_logs"

    # Calls 1, 2, 3 with distinct arguments should not be intercepted
    assert tracker.before_tool_callback(dummy_tool, tool_args={"q": "1"}) is None
    tracker.after_tool_callback(dummy_tool, tool_args={"q": "1"})
    assert tracker.before_tool_callback(dummy_tool, tool_args={"q": "2"}) is None
    tracker.after_tool_callback(dummy_tool, tool_args={"q": "2"})
    assert tracker.before_tool_callback(dummy_tool, tool_args={"q": "3"}) is None
    tracker.after_tool_callback(dummy_tool, tool_args={"q": "3"})

    # Call 4: budget reached (tool_call_count == 3 >= max_tool_calls == 3)
    intercept = tracker.before_tool_callback(dummy_tool, tool_args={"q": "4"})
    assert isinstance(intercept, dict)
    assert intercept.get("status") == "complete"
    assert "error" not in intercept
    assert "Diagnostic query budget reached" in intercept.get("message", "")
    assert "set_model_response" in intercept.get("message", "")

    # After-tool callback does not increment beyond max_tool_calls when intercepted
    tracker.after_tool_callback(dummy_tool, tool_args={"q": "4"}, tool_output=intercept)
    assert tracker.tool_call_count == 3

    # set_model_response is never intercepted and does not increment tool_call_count
    resp_tool = MagicMock()
    resp_tool.name = "set_model_response"
    assert tracker.before_tool_callback(resp_tool) is None
    tracker.after_tool_callback(resp_tool)
    assert tracker.tool_call_count == 3


def test_skill_tracker_intercepts_repeated_load_skill() -> None:
    """Verify before_tool_callback intercepts second load_skill call."""
    tracker = SkillTracker(max_tool_calls=5)
    load_tool = MagicMock()
    load_tool.name = "load_skill"

    # First call to load_skill succeeds
    assert (
        tracker.before_tool_callback(load_tool, tool_args={"skill_name": "cloud-logging-queries"})
        is None
    )
    tracker.after_tool_callback(load_tool, tool_args={"skill_name": "cloud-logging-queries"})
    assert tracker.skills_used == ["cloud-logging-queries"]

    # Second call to load_skill is intercepted
    intercept = tracker.before_tool_callback(
        load_tool, tool_args={"skill_name": "cloud-logging-queries"}
    )
    assert isinstance(intercept, dict)
    assert intercept.get("status") == "skill_already_loaded"
    assert "cloud-logging-queries" in intercept.get("message", "")
    assert "Do not invoke load_skill again" in intercept.get("message", "")


def test_skill_tracker_intercepts_duplicate_queries() -> None:
    """Verify before_tool_callback intercepts calls with identical arguments."""
    tracker = SkillTracker(max_tool_calls=5)
    query_tool = MagicMock()
    query_tool.name = "query_logs"

    # First call with {"filter": "severity>=ERROR"} succeeds
    assert tracker.before_tool_callback(query_tool, tool_args={"filter": "severity>=ERROR"}) is None
    tracker.after_tool_callback(query_tool, tool_args={"filter": "severity>=ERROR"})

    # Duplicate call with same args is intercepted
    intercept = tracker.before_tool_callback(query_tool, tool_args={"filter": "severity>=ERROR"})
    assert isinstance(intercept, dict)
    assert intercept.get("status") == "already_executed"
    assert "This query was already executed" in intercept.get("message", "")

    # Call with different args succeeds
    assert (
        tracker.before_tool_callback(query_tool, tool_args={"filter": "severity>=WARNING"}) is None
    )
