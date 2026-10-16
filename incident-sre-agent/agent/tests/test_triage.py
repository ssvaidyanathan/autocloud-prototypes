from datetime import datetime, timedelta, timezone

import pytest
from agent.app.contracts.incident import Incident
from agent.app.triage import InvestigationPlan, TriageAgent
from pydantic import ValidationError


def _create_incident(
    severity: str = "P2",
    service_hint: str | None = "shop-api",
    title: str = "Spike in 500 errors on shop-api",
    description: str = "Checkout endpoint failing with HTTP 500 after new revision rollout",
    opened_at: datetime | None = None,
) -> Incident:
    return Incident(
        id="inc-triage-1",
        source="servicenow",
        external_id="INC0011",
        external_key="INC0011",
        title=title,
        description=description,
        severity=severity,  # type: ignore[arg-type]
        service_hint=service_hint,
        opened_at=opened_at or datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc),
    )


def test_investigation_plan_validation() -> None:
    now = datetime.now(timezone.utc)
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["5xx errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics"],
        initial_hypotheses=["Spike caused by code regression"],
    )
    assert plan.affected_service == "shop-api"
    assert plan.start_time.tzinfo == timezone.utc

    # Invalid specialist
    with pytest.raises(ValidationError):
        InvestigationPlan(
            affected_service="shop-api",
            start_time=now - timedelta(hours=1),
            end_time=now,
            selected_specialists=["invalid_specialist"],
        )

    # start_time after end_time
    with pytest.raises(ValidationError):
        InvestigationPlan(
            affected_service="shop-api",
            start_time=now,
            end_time=now - timedelta(hours=1),
            selected_specialists=["logs"],
        )


def test_triage_service_and_symptom_extraction() -> None:
    agent = TriageAgent()

    # Case 1: Service from service_hint
    inc1 = _create_incident(service_hint="inventory-svc")
    plan1 = agent.plan(inc1)
    assert plan1.affected_service == "inventory-svc"
    assert any("5xx" in s or "error" in s for s in plan1.symptoms)

    # Case 2: Service parsed from text when hint is None
    inc2 = _create_incident(
        service_hint=None,
        title="High latency on shop-api checkout",
        description="p99 latency degraded to 4.5s; database connection timeout",
    )
    plan2 = agent.plan(inc2)
    assert plan2.affected_service == "shop-api"
    assert any("latency" in s.lower() for s in plan2.symptoms)
    assert any("database" in s.lower() for s in plan2.symptoms)


def test_triage_time_window_default_and_clamping() -> None:
    agent = TriageAgent(max_query_window_hours=24)
    opened = datetime.now(timezone.utc) - timedelta(minutes=15)
    inc = _create_incident(opened_at=opened)

    # Default window: opened_at - 15 min ... now (when opened_at is within last 2 hours)
    now_before = datetime.now(timezone.utc)
    plan = agent.plan(inc)
    now_after = datetime.now(timezone.utc)

    assert plan.start_time == opened - timedelta(minutes=15)
    assert now_before <= plan.end_time <= now_after

    # When opened_at is > 2 hours in the past, start_time is now_utc - 60 min
    old_opened = datetime.now(timezone.utc) - timedelta(hours=5)
    old_inc = _create_incident(opened_at=old_opened)
    old_plan = agent.plan(old_inc)
    assert (
        abs((old_plan.start_time - (old_plan.end_time - timedelta(minutes=60))).total_seconds()) < 2
    )

    # 24-hour maximum query window limit enforcement
    # Requesting an override window of 48 hours earlier than end_time
    start_override = plan.end_time - timedelta(hours=48)
    clamped_plan = agent.plan(
        inc,
        start_time_override=start_override,
        end_time_override=plan.end_time,
    )
    delta = clamped_plan.end_time - clamped_plan.start_time
    assert delta <= timedelta(hours=24)
    assert clamped_plan.start_time == plan.end_time - timedelta(hours=24)


def test_triage_specialist_selection() -> None:
    agent = TriageAgent()

    # P1/P2 incidents select all 4 specialists
    p1_inc = _create_incident(severity="P1")
    plan_p1 = agent.plan(p1_inc)
    assert set(plan_p1.selected_specialists) == {"logs", "metrics", "changes", "resources"}

    p2_inc = _create_incident(severity="P2")
    plan_p2 = agent.plan(p2_inc)
    assert set(plan_p2.selected_specialists) == {"logs", "metrics", "changes", "resources"}

    # P3 incident with specific symptoms (HTTP 500 selects both logs and metrics)
    p3_inc = _create_incident(
        severity="P3",
        title="Elevated 500 error count",
        description="Minor increase in 500 status codes",
    )
    plan_p3 = agent.plan(p3_inc)
    assert "logs" in plan_p3.selected_specialists
    assert "metrics" in plan_p3.selected_specialists

    # P3 incident with database connection saturation selects resources as well
    p3_db_inc = _create_incident(
        severity="P3",
        title="Elevated 500 error count with database connection pool exhaustion",
        description="Pool saturated, unable to acquire connection",
    )
    plan_p3_db = agent.plan(p3_db_inc)
    assert "logs" in plan_p3_db.selected_specialists
    assert "metrics" in plan_p3_db.selected_specialists
    assert "resources" in plan_p3_db.selected_specialists

    # P4 incident with latency only
    p4_inc = _create_incident(
        severity="P4",
        title="Slight latency bump",
        description="p95 latency is a bit slow",
    )
    plan_p4 = agent.plan(p4_inc)
    assert "metrics" in plan_p4.selected_specialists


def test_triage_resets_onset_time_unless_override_provided() -> None:
    """Verifies that triage completion sets onset_time to None unless explicitly overridden."""
    from unittest.mock import MagicMock

    agent = TriageAgent()
    inc = _create_incident()

    # 1. Heuristic triage without override -> onset_time is None
    plan_heuristic = agent.plan(inc)
    assert plan_heuristic.onset_time is None

    # 2. Heuristic triage with override -> onset_time matches override
    override_ts = datetime(2026, 10, 9, 1, 55, 0, tzinfo=timezone.utc)
    plan_override = agent.plan(inc, onset_time_override=override_ts)
    assert plan_override.onset_time == override_ts

    # 3. LLM triage returning plan with onset_time set -> reset to None when override is None
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    mock_plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["5xx errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics"],
        initial_hypotheses=["Error spike"],
        onset_time=inc.opened_at,
    )
    mock_llm_client = MagicMock()
    mock_llm_client.generate_plan.return_value = mock_plan

    llm_agent = TriageAgent(llm_client=mock_llm_client)
    plan_llm = llm_agent.plan(inc)
    assert plan_llm.onset_time is None

    # 4. LLM triage with explicit override -> preserves override
    plan_llm_override = llm_agent.plan(inc, onset_time_override=override_ts)
    assert plan_llm_override.onset_time == override_ts
