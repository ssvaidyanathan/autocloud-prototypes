"""Unit tests for GeminiLlmClient structured plan and report generation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import (
    ProbableRootCause,
    Report,
    TimelineItem,
)
from agent.app.llm import GeminiLlmClient
from agent.app.triage import InvestigationPlan


def _create_sample_incident() -> Incident:
    return Incident(
        id="inc-test-llm-1",
        source="servicenow",
        external_id="INC009988",
        external_key="INC009988",
        title="Spike in 500 errors on shop-api",
        description="HTTP 500 responses and latency spikes detected on checkout endpoint.",
        severity="P1",
        service_hint="shop-api",
        opened_at=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
    )


def test_generate_plan_success() -> None:
    """Validates structured InvestigationPlan return from model response."""
    incident = _create_sample_incident()
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    expected_plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 500 Internal Server Error", "Latency spike on checkout"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics"],
        initial_hypotheses=["Database connection pool exhausted after deployment"],
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = expected_plan.model_dump_json()
    mock_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-project",
        location="test-location",
        model="test-model",
        client=mock_client,
    )

    plan = llm_client.generate_plan(incident)

    assert isinstance(plan, InvestigationPlan)
    assert plan.affected_service == "shop-api"
    assert plan.selected_specialists == ["logs", "metrics"]
    assert plan.symptoms == expected_plan.symptoms
    now_utc_approx = datetime.now(timezone.utc)
    assert abs((plan.end_time - now_utc_approx).total_seconds()) < 5
    assert abs((plan.start_time - (now_utc_approx - timedelta(minutes=60))).total_seconds()) < 5
    assert plan.initial_hypotheses == expected_plan.initial_hypotheses
    assert plan.onset_time is None

    assert mock_client.models.generate_content.called
    call_args = mock_client.models.generate_content.call_args
    assert call_args.kwargs["model"] == "test-model"
    assert "INC009988" in call_args.kwargs["contents"]


def test_generate_plan_resets_onset_time() -> None:
    """Verifies that generate_plan resets onset_time to None even if the LLM populated it."""
    incident = _create_sample_incident()
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    expected_plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 500 Internal Server Error"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs"],
        initial_hypotheses=["Database connection pool exhausted"],
        onset_time=incident.opened_at,
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = expected_plan.model_dump_json()
    mock_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-project",
        location="test-location",
        model="test-model",
        client=mock_client,
    )

    plan = llm_client.generate_plan(incident)
    assert plan.onset_time is None


def test_generate_plan_empty_response() -> None:
    """Asserts RuntimeError on empty response."""
    incident = _create_sample_incident()

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""
    mock_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-project",
        client=mock_client,
    )

    with pytest.raises(
        RuntimeError,
        match="Vertex AI Gemini returned empty response for triage plan",
    ):
        llm_client.generate_plan(incident)

    mock_client.models.generate_content.return_value = None
    with pytest.raises(
        RuntimeError,
        match="Vertex AI Gemini returned empty response for triage plan",
    ):
        llm_client.generate_plan(incident)


def test_generate_report_success() -> None:
    """Validates Report return, metadata backfilling, and status reconciliation."""
    incident = _create_sample_incident()
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 500 errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "changes"],  # resources is unselected
        initial_hypotheses=["DB connection exhaustion"],
    )

    findings = {
        "logs": Findings(
            summary="Found repeated database connection timeout errors in logs.",
            evidence=[
                EvidenceItem(
                    kind="error_log",
                    excerpt="Connection refused on port 5432 after pool exhaustion",
                    timestamp=now - timedelta(minutes=15),
                    console_url="https://console.cloud.google.com/logs",
                )
            ],
            anomalies=["Log error burst of 200 req/min"],
            queries_run=["query_logs", "summarize_error_groups"],
            confidence=0.9,
            errors=[],
        ),
        "metrics": Findings(
            summary="Database active connections reached 100% saturation.",
            evidence=[
                EvidenceItem(
                    kind="metric_chart",
                    excerpt="Active connections: 100/100",
                    timestamp=now - timedelta(minutes=20),
                )
            ],
            anomalies=["Connection saturation"],
            queries_run=["query_time_series"],
            confidence=0.85,
            errors=["Metrics query timed out for secondary disk IO"],
        ),
        # "changes" in plan.selected_specialists, but omitted from findings (spec_finding is None)
        # to verify null specialist findings status reconciliation handling.
    }

    raw_report = Report(
        summary="Service shop-api degraded due to PostgreSQL connection pool exhaustion.",
        impact="Critical user checkout requests failing with HTTP 500.",
        timeline=[
            TimelineItem(
                timestamp=now - timedelta(minutes=30),
                description="Revision shop-api-v2 rolled out.",
            ),
            TimelineItem(
                timestamp=now - timedelta(minutes=20),
                description="Database connection saturation reached.",
            ),
        ],
        probable_root_cause=ProbableRootCause(
            text="Connection pool leak introduced in revision shop-api-v2.",
            confidence=0.92,
        ),
        recommended_actions=[
            "Roll back to revision shop-api-v1.",
            "Increase max database connection pool size.",
        ],
        # Deliberately leave metadata, queries_appendix, evidence_by_area, and specialists_status
        # unpopulated to exercise GeminiLlmClient backfilling logic.
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = raw_report.model_dump_json()
    mock_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-project",
        location="test-location",
        model="test-dispatcher-model",
        client=mock_client,
    )

    report = llm_client.generate_report(incident, plan, findings)

    assert isinstance(report, Report)
    assert report.summary == raw_report.summary
    assert report.impact == raw_report.impact
    assert report.probable_root_cause == raw_report.probable_root_cause
    assert report.timeline == raw_report.timeline
    assert report.recommended_actions == raw_report.recommended_actions

    # Verify metadata backfilling
    assert report.metadata["synthesizer_mode"] == "llm"
    assert report.metadata["incident_id"] == incident.id
    assert report.metadata["external_key"] == incident.external_key
    assert report.metadata["affected_service"] == plan.affected_service
    assert report.metadata["window_start"] == plan.start_time.isoformat()
    assert report.metadata["window_end"] == plan.end_time.isoformat()
    assert "generated_at" in report.metadata

    # Verify queries appendix backfilling
    assert report.queries_appendix["logs"] == ["query_logs", "summarize_error_groups"]
    assert report.queries_appendix["metrics"] == ["query_time_series"]

    # Verify evidence backfilling
    assert len(report.evidence_by_area["logs"]) == 1
    assert (
        report.evidence_by_area["logs"][0].excerpt
        == "Connection refused on port 5432 after pool exhaustion"
    )
    assert len(report.evidence_by_area["metrics"]) == 1

    # Verify specialist status reconciliation across all 4 specialist areas:
    # 1. logs: selected, finding present, zero errors -> completed
    # 2. metrics: selected, finding present, has errors -> failed with error message
    # 3. changes: selected, finding is None -> failed with "No findings returned by specialist"
    # 4. resources: not selected -> skipped
    statuses = {s.specialist: s for s in report.specialists_status}
    assert set(statuses.keys()) == {"logs", "metrics", "changes", "resources"}

    assert statuses["logs"].status == "completed"
    assert statuses["logs"].error is None

    assert statuses["metrics"].status == "failed"
    assert statuses["metrics"].error == "Metrics query timed out for secondary disk IO"

    assert statuses["changes"].status == "failed"
    assert statuses["changes"].error == "No findings returned by specialist"

    assert statuses["resources"].status == "skipped"
    assert statuses["resources"].error is None


def test_generate_report_empty_response() -> None:
    """Asserts RuntimeError on empty response."""
    incident = _create_sample_incident()
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 500 errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs"],
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""
    mock_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-project",
        client=mock_client,
    )

    with pytest.raises(
        RuntimeError,
        match="Vertex AI Gemini returned empty response for synthesis report",
    ):
        llm_client.generate_report(incident, plan, {})


def test_client_init_failure() -> None:
    """Asserts RuntimeError when get_genai_client() returns None."""
    with patch("agent.app.llm.get_genai_client", return_value=None):
        with pytest.raises(
            RuntimeError,
            match="Failed to instantiate Vertex AI GenAI client for project 'test-project'",
        ):
            GeminiLlmClient(project_id="test-project", location="us-central1")


def test_llm_generate_report_fallback_actions_labeled() -> None:
    """Verify that when LLM returns no recommended_actions, fallback actions are labeled."""
    from agent.app.contracts.incident import Incident
    from agent.app.contracts.report import ProbableRootCause, Report
    from agent.app.triage import InvestigationPlan

    incident = Incident(
        id="inc-act-test",
        source="servicenow",
        external_id="INC001122",
        external_key="INC001122",
        title="Action label test",
        description="Testing action labels",
        severity="P2",
        service_hint="payment-svc",
        opened_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    plan = InvestigationPlan(
        affected_service="payment-svc",
        symptoms=["Latency spike"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs"],
    )

    report_without_actions = Report(
        summary="Service latency observed.",
        impact="Moderate impact.",
        probable_root_cause=ProbableRootCause(
            text="Elevated request latency due to connection bottleneck.",
            confidence=0.75,
        ),
        recommended_actions=[],
    )

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = report_without_actions.model_dump_json()
    mock_client.models.generate_content.return_value = mock_resp

    llm_client = GeminiLlmClient(
        project_id="test-project",
        client=mock_client,
    )

    result_report = llm_client.generate_report(incident, plan, {})
    assert len(result_report.recommended_actions) > 0
    for action in result_report.recommended_actions:
        assert action.startswith("[Automated default] ")
