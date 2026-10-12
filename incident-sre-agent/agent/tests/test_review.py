from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.review import ReviewEvaluator
from agent.app.specialists.base import BaseSpecialist
from agent.app.triage import InvestigationPlan


def _create_sample_incident() -> Incident:
    return Incident(
        id="inc-rev-1",
        source="servicenow",
        external_id="INC0099",
        external_key="INC0099",
        title="Errors after release on shop-api",
        description="Customers reporting 500 errors after deployment",
        severity="P2",
        service_hint="shop-api",
        opened_at=datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
    )


def _create_sample_plan() -> InvestigationPlan:
    opened = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    return InvestigationPlan(
        affected_service="shop-api",
        symptoms=["500 errors"],
        start_time=opened - timedelta(hours=1),
        end_time=opened,
        selected_specialists=["logs", "metrics", "changes", "resources"],
        initial_hypotheses=["Deployment regression"],
    )


def test_review_evaluator_triggers_follow_up_on_change() -> None:
    evaluator = ReviewEvaluator(max_rounds=1)
    incident = _create_sample_incident()
    plan = _create_sample_plan()

    deploy_time = datetime(2026, 10, 9, 1, 55, 0, tzinfo=timezone.utc)
    findings = {
        "changes": Findings(
            summary="New revision deployed",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision shop-api-v2 deployed with 100% traffic",
                    timestamp=deploy_time,
                )
            ],
            anomalies=["Deployment detected at 01:55 UTC"],
            queries_run=["list_cloud_run_revisions"],
            confidence=0.9,
            errors=[],
        ),
        "logs": Findings(
            summary="Initial log query found 500s",
            evidence=[],
            anomalies=[],
            queries_run=["query_logs"],
            confidence=0.5,
            errors=[],
        ),
    }

    action = evaluator.evaluate(incident, plan, findings)
    assert action is not None
    assert action.specialist == "logs"
    assert "01:55:00" in action.reason
    assert action.start_time == deploy_time - timedelta(minutes=5)
    assert action.end_time == deploy_time + timedelta(minutes=5)


def test_review_evaluator_enforces_max_one_round() -> None:
    evaluator = ReviewEvaluator(max_rounds=1)
    incident = _create_sample_incident()
    plan = _create_sample_plan()

    error_time = datetime(2026, 10, 9, 1, 50, 0, tzinfo=timezone.utc)
    findings = {
        "logs": Findings(
            summary="Error group spike",
            evidence=[
                EvidenceItem(
                    kind="error",
                    excerpt="Unhandled Exception: Connection refused",
                    timestamp=error_time,
                )
            ],
            anomalies=["Repeated connection errors"],
            queries_run=["summarize_error_groups"],
            confidence=0.8,
            errors=[],
        ),
        "metrics": Findings(
            summary="Metrics",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.5,
            errors=[],
        ),
    }

    # Round 1: should return action
    action = evaluator.evaluate(incident, plan, findings)
    assert action is not None

    # Execute round 1
    mock_specialist = MagicMock(spec=BaseSpecialist)
    mock_specialist.investigate.return_value = Findings(
        summary="Follow-up metrics findings",
        evidence=[],
        anomalies=[],
        queries_run=["query_time_series"],
        confidence=0.85,
        errors=[],
    )

    follow_up_res = evaluator.execute_follow_up(action, mock_specialist, incident)
    assert follow_up_res.confidence == 0.85
    assert evaluator.rounds_executed == 1

    # Round 2: evaluator must return None (strictly enforced max 1 round)
    action_round_2 = evaluator.evaluate(incident, plan, findings)
    assert action_round_2 is None

    # Directly attempting second execution raises RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        evaluator.execute_follow_up(action, mock_specialist, incident)
    assert "max rounds" in str(exc_info.value).lower()


def test_review_merge_findings() -> None:
    original = Findings(
        summary="Original logs summary",
        evidence=[
            EvidenceItem(kind="log", excerpt="Initial log entry 1"),
        ],
        anomalies=["Anomaly A"],
        queries_run=["query_logs(filter=A)"],
        confidence=0.6,
        errors=[],
    )

    follow_up = Findings(
        summary="Drill-down logs",
        evidence=[
            EvidenceItem(kind="log", excerpt="Initial log entry 1"),  # Duplicate
            EvidenceItem(kind="log", excerpt="Targeted stack trace in /checkout"),
        ],
        anomalies=["Anomaly B"],
        queries_run=["query_logs(filter=B)"],
        confidence=0.85,
        errors=[],
    )

    merged = ReviewEvaluator.merge_findings(original, follow_up)
    assert len(merged.evidence) == 2  # Deduplicated
    assert len(merged.anomalies) == 2
    assert len(merged.queries_run) == 2
    assert "[follow-up]" in merged.queries_run[1]
    assert merged.confidence == 0.85
    assert "Follow-up investigation added" in merged.summary
