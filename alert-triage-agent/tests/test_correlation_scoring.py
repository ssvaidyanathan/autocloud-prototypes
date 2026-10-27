"""Mathematical and behavioral unit tests for the Correlation Scoring Engine."""

from datetime import datetime, timedelta, timezone
import pytest

from agent.app.contracts.alert import AlertEvent
from agent.app.correlation.scoring import (
    calculate_causal_score,
    calculate_change_score,
    calculate_temporal_score,
    calculate_topology_score,
    classify_anomaly_category,
    compute_multi_factor_score,
    WEIGHT_CAUSAL,
    WEIGHT_CHANGE,
    WEIGHT_TIME,
    WEIGHT_TOPO,
)
from agent.app.correlation.engine import (
    CandidateGroup,
    CorrelationEngine,
    GrayZoneResolution,
    resolve_gray_zone_with_gemini,
)


def test_topology_distance_math():
    """Verify exact scoring across distance boundaries:
    0 hops = 35 pts, 1 hop = 28 pts, 2 hops = 17.5 pts, >=3 hops / disjoint = 0 pts.
    """
    pts0, factor0 = calculate_topology_score(0)
    assert pts0 == 35.0
    assert factor0 == 1.0

    pts1, factor1 = calculate_topology_score(1)
    assert pts1 == 28.0
    assert factor1 == 0.8

    pts2, factor2 = calculate_topology_score(2)
    assert pts2 == 17.5
    assert factor2 == 0.5

    pts3, factor3 = calculate_topology_score(3)
    assert pts3 == 0.0
    assert factor3 == 0.0

    pts_disjoint, factor_disjoint = calculate_topology_score(None)
    assert pts_disjoint == 0.0
    assert factor_disjoint == 0.0


def test_temporal_decay_curve():
    """Verify temporal decay curves:
    t = 0 -> 20 pts, t = 5 min -> 10 pts, t >= 10 min -> 0 pts.
    """
    anchor = datetime(2026, 10, 22, 21, 0, 0, tzinfo=timezone.utc)

    # t = 0 min
    t0 = anchor
    pts0, factor0, delta0 = calculate_temporal_score(t0, anchor)
    assert pts0 == 20.0
    assert factor0 == 1.0
    assert delta0 == 0.0

    # t = 5 min
    t5 = anchor + timedelta(minutes=5)
    pts5, factor5, delta5 = calculate_temporal_score(t5, anchor)
    assert pts5 == 10.0
    assert factor5 == 0.5
    assert delta5 == 5.0

    # t = 10 min
    t10 = anchor + timedelta(minutes=10)
    pts10, factor10, delta10 = calculate_temporal_score(t10, anchor)
    assert pts10 == 0.0
    assert factor10 == 0.0
    assert delta10 == 10.0

    # t = 15 min (> 10 min horizon)
    t15 = anchor + timedelta(minutes=15)
    pts15, factor15, delta15 = calculate_temporal_score(t15, anchor)
    assert pts15 == 0.0
    assert factor15 == 0.0
    assert delta15 == 15.0


def test_causal_failure_chain_matrices():
    """Verify causal chain compatibility rules:
    - Direct Propagation: DB connection / lock -> Backend 5xx -> Apigee 5xx = 30 pts.
    - Incompatible: SpikeArrest 429 vs DB saturation = 0 pts; Auth 401/403 vs 5xx = 0 pts.
    - Plausible: general 5xx across services = 15 pts.
    """
    # Direct DB -> Backend 5xx
    pts, factor, cat = calculate_causal_score("db_saturation", "backend_5xx")
    assert pts == 30.0
    assert factor == 1.0

    # Direct Backend 5xx -> Apigee 504 / Latency
    pts_lat, factor_lat, _ = calculate_causal_score("backend_5xx", "latency_504")
    assert pts_lat == 30.0
    assert factor_lat == 1.0

    # Incompatible SpikeArrest 429 vs DB saturation
    pts_incompat, factor_incompat, cat_incompat = calculate_causal_score("client_429", "db_saturation")
    assert pts_incompat == 0.0
    assert factor_incompat == 0.0
    assert "incompatible" in cat_incompat

    # Incompatible Client Auth vs Backend 5xx
    pts_auth, factor_auth, cat_auth = calculate_causal_score("client_auth", "backend_5xx")
    assert pts_auth == 0.0
    assert factor_auth == 0.0
    assert "incompatible" in cat_auth

    # Plausible general 5xx
    pts_plausible, factor_plausible, _ = calculate_causal_score("backend_5xx", "generic_anomaly")
    assert pts_plausible == 15.0
    assert factor_plausible == 0.5


def test_deployment_change_alignment_scoring():
    """Verify deployment change alignment gives 15 pts within 30 min of onset, 0 pts otherwise."""
    alert_t = datetime(2026, 10, 22, 21, 30, 0, tzinfo=timezone.utc)

    # Within 30 minutes (e.g. 15 minutes before)
    dep_t_aligned = alert_t - timedelta(minutes=15)
    pts, factor, aligned = calculate_change_score(alert_t, dep_t_aligned)
    assert pts == 15.0
    assert factor == 1.0
    assert aligned is True

    # Exactly 30 minutes
    dep_t_30 = alert_t - timedelta(minutes=30)
    pts30, factor30, aligned30 = calculate_change_score(alert_t, dep_t_30)
    assert pts30 == 15.0
    assert aligned30 is True

    # 45 minutes prior (exceeds 30 min threshold)
    dep_t_unaligned = alert_t - timedelta(minutes=45)
    pts_un, factor_un, aligned_un = calculate_change_score(alert_t, dep_t_unaligned)
    assert pts_un == 0.0
    assert factor_un == 0.0
    assert aligned_un is False

    # None deployment time
    pts_none, _, aligned_none = calculate_change_score(alert_t, None)
    assert pts_none == 0.0
    assert aligned_none is False


def test_multi_factor_scoring_combined_auto_attach():
    """Verify high-confidence auto-attach threshold >= 65."""
    now = datetime(2026, 10, 22, 21, 30, 0, tzinfo=timezone.utc)
    # Direct neighbor (1 hop = 28 pts) + DB to 5xx (30 pts) + t=2m (16 pts) + change (15 pts) = 89 pts >= 65
    breakdown = compute_multi_factor_score(
        alert_resource="ata-orders-api",
        alert_category="backend_5xx",
        alert_time=now + timedelta(minutes=2),
        group_resources=["ata-inventory-api"],
        group_category="db_saturation",
        group_anchor_time=now,
        deployment_time=now - timedelta(minutes=10),
    )
    assert breakdown.total_score >= 65.0
    assert breakdown.hop_distance == 1
    assert breakdown.topo_score == 28.0
    assert breakdown.causal_score == 30.0
    assert breakdown.change_score == 15.0


def test_multi_factor_scoring_combined_auto_new_group():
    """Verify disjoint auto-new group threshold < 35."""
    now = datetime(2026, 10, 22, 21, 30, 0, tzinfo=timezone.utc)
    # SpikeArrest 429 vs DB saturation (0 causal) + disjoint (0 topo) + t=12m (0 time) + no change = 0 pts < 35
    breakdown = compute_multi_factor_score(
        alert_resource="ata-apigee-orders-v1",
        alert_category="client_429",
        alert_time=now + timedelta(minutes=12),
        group_resources=["ata-inventory-db"],
        group_category="db_saturation",
        group_anchor_time=now,
        deployment_time=None,
    )
    assert breakdown.total_score < 35.0
    assert breakdown.total_score == 0.0


def test_gray_zone_resolution_and_schema():
    """Verify gray zone (35 <= score < 65) invokes resolution returning structured schema."""
    now = datetime(2026, 10, 22, 21, 30, 0, tzinfo=timezone.utc)
    # 1 hop (28 pts) + 15 pts plausible causal + t=6m (8 pts) = 51 pts (Gray Zone)
    breakdown = compute_multi_factor_score(
        alert_resource="ata-orders-api",
        alert_category="backend_5xx",
        alert_time=now + timedelta(minutes=6),
        group_resources=["ata-inventory-api"],
        group_category="generic_anomaly",
        group_anchor_time=now,
        deployment_time=None,
    )
    assert 35.0 <= breakdown.total_score < 65.0

    alert = AlertEvent(
        incident_id="test.001",
        policy_name="ata-backend-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-22T21:36:00Z",
    )
    candidate = CandidateGroup(
        group_id="grp_test_inventory",
        filename="incident-test.md",
        file_path=None,
        origin="ata-inventory-api",
        resources=["ata-inventory-api"],
        category="generic_anomaly",
        anchor_time=now,
    )

    # Live Gemini is blocked in tests (conftest), so the deterministic fallback decides and says so.
    res = resolve_gray_zone_with_gemini(
        alert, candidate, breakdown, model_name="test-model", project_id="test-project", location="global"
    )
    assert isinstance(res, GrayZoneResolution)
    assert res.source == "deterministic_fallback"
    assert res.fallback_reason
    assert res.decision in {"attach", "new_group"}
    assert 0.0 <= res.confidence <= 1.0
    assert len(res.rationale) > 0


def test_upstream_dependency_latency_correlation():
    """Verify that caller orders-api latency auto-correlates to upstream inventory-api group.

    1 hop distance (28 pts) + direct latency cascade (30 pts) + temporal proximity (20 pts)
    = 78 pts (well above the auto-attach threshold of 65 pts).
    """
    now = datetime(2026, 10, 23, 4, 42, 25, tzinfo=timezone.utc)
    t_alert = now + timedelta(minutes=2)

    breakdown = compute_multi_factor_score(
        alert_resource="ata-orders-api",
        alert_category="latency_504",
        alert_time=t_alert,
        group_resources=["ata-inventory-api"],
        group_category="latency_504",
        group_anchor_time=now,
        deployment_time=None,
    )

    assert breakdown.hop_distance == 1
    assert breakdown.topo_score == 28.0
    assert breakdown.causal_score == 30.0
    assert breakdown.time_score > 15.0
    assert breakdown.total_score >= 65.0
