"""Hermetic unit tests for Item 8.3: Onset detection, timeline, and dependency fan-out."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from agent.app.config import AgentConfig
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import ProbableRootCause, RecentChangeItem, Report
from agent.app.dependencies import (
    extract_downstream_dependencies,
    inspect_downstream_dependency,
    resolve_run_app_service_name,
)
from agent.app.onset import PriorIncident, detect_onset_from_telemetry
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.synthesizer import SynthesizerAgent, calibrate_report_confidence
from agent.app.triage import InvestigationPlan


def _create_sample_incident(
    opened_at: datetime | None = None,
    service_hint: str = "order-api",
) -> Incident:
    return Incident(
        id="inc-item83-001",
        source="servicenow",
        external_id="INC83001",
        external_key="INC83001",
        title="Spike in 500 errors on order-api",
        description="Checkout endpoint failing with HTTP 500 and downstream 403 errors",
        severity="P2",
        service_hint=service_hint,
        opened_at=opened_at or datetime(2026, 10, 15, 12, 45, 0, tzinfo=timezone.utc),
    )


def test_onset_detection_from_request_logs_and_metrics() -> None:
    """Verifies that onset detection identifies earliest error deviation and anchors window."""
    end_time = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 15, 12, 35, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "status": 500,
                    "latency_ms": 150.0,
                    "excerpt": "POST /api/checkout HTTP 500",
                },
                {
                    "timestamp": (onset_ts + timedelta(minutes=5)).isoformat(),
                    "status": 500,
                    "latency_ms": 200.0,
                    "excerpt": "POST /api/checkout HTTP 500",
                },
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)
    assert res.end_time == end_time
    assert len(res.prior_incidents) == 0


def test_onset_detection_fallback_when_telemetry_clean() -> None:
    """Verifies safe fallback to lookback window when telemetry exhibits zero errors."""
    end_time = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert res.is_fallback
    assert res.onset_time is None
    assert res.anchored_start_time == end_time - timedelta(minutes=60)


def test_prior_incident_separation() -> None:
    """Verifies that earlier resolved error bursts ending before onset are segregated."""
    end_time = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)
    prior_start = datetime(2026, 10, 15, 12, 5, 0, tzinfo=timezone.utc)
    prior_end = datetime(2026, 10, 15, 12, 10, 0, tzinfo=timezone.utc)
    active_onset = datetime(2026, 10, 15, 12, 35, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                # Earlier episode
                {"timestamp": prior_start.isoformat(), "status": 500, "excerpt": "error 1"},
                {"timestamp": prior_end.isoformat(), "status": 500, "excerpt": "error 2"},
                # 25-minute quiescent gap with healthy traffic
                # Active incident episode
                {"timestamp": active_onset.isoformat(), "status": 500, "excerpt": "active error 1"},
                {
                    "timestamp": (active_onset + timedelta(minutes=10)).isoformat(),
                    "status": 500,
                    "excerpt": "active error 2",
                },
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        prior_quiescent_minutes=10,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    assert res.onset_time == active_onset
    assert len(res.prior_incidents) == 1
    assert res.prior_incidents[0].start_time == prior_start
    assert res.prior_incidents[0].end_time == prior_end

    # Test that Synthesizer caveats acknowledge and segregate the prior incident
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=res.anchored_start_time,
        end_time=end_time,
        onset_time=active_onset,
    )
    setattr(plan, "prior_incidents", res.prior_incidents)

    rep = Report(
        summary="Investigation summary",
        impact="High impact",
        probable_root_cause=ProbableRootCause(text="Preliminary root cause", confidence=0.7),
    )
    calibrated = calibrate_report_confidence(rep, findings={}, plan=plan)
    assert any("prior incident" in c.lower() for c in calibrated.caveats)


def test_revision_causality_rule_long_running_revision_not_blamed() -> None:
    """Verifies that revisions deployed long before onset are marked benign and not blamed."""
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(hours=4)

    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    old_rev = MagicMock()
    old_rev.name = "order-api-old-rev"
    old_rev.create_time = old_deploy_time
    old_rev.traffic_percent = 100
    old_rev.template.containers = [MagicMock(image="gcr.io/test-proj/order-api:v1")]
    mock_client.list_revisions.return_value = [old_rev]
    mock_client.list_builds.return_value = []

    cfg = AgentConfig(revision_causality_window_minutes=30)
    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client, config=cfg)
    incident = _create_sample_incident()
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )

    findings = specialist.investigate(incident, plan=plan)

    # Long-running revision should be marked as benign baseline in evidence
    assert any("[benign baseline" in ev.excerpt for ev in findings.evidence)
    # Long-running revision should NOT be flagged as an anomalous triggering deployment
    assert not any(
        "Recent revision deployment:" in a and "order-api-old-rev" in a for a in findings.anomalies
    )

    # When Synthesizer evaluates report with only old revisions, confidence is down-calibrated
    rep = Report(
        summary="Revision deploy blamed",
        impact="Service degraded",
        probable_root_cause=ProbableRootCause(
            text="Service disruption triggered by revision order-api-old-rev rollout",
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(timestamp=old_deploy_time, description="Revision order-api-old-rev")
        ],
    )
    calibrated = calibrate_report_confidence(rep, findings={"changes": findings}, plan=plan)
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.40
    assert any("causality window" in c.lower() for c in calibrated.caveats)


def test_revision_causality_rule_proximate_revision_evaluated() -> None:
    """Verifies that revisions deployed near onset are treated as plausible causal candidates."""
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    new_deploy_time = onset_time - timedelta(minutes=10)

    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    new_rev = MagicMock()
    new_rev.name = "order-api-new-rev"
    new_rev.create_time = new_deploy_time
    new_rev.traffic_percent = 100
    new_rev.template.containers = [MagicMock(image="gcr.io/test-proj/order-api:v2")]
    mock_client.list_revisions.return_value = [new_rev]
    mock_client.list_builds.return_value = []

    cfg = AgentConfig(revision_causality_window_minutes=30)
    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client, config=cfg)
    incident = _create_sample_incident()
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )

    findings = specialist.investigate(incident, plan=plan)

    assert any("[causal candidate" in ev.excerpt for ev in findings.evidence)
    assert any("order-api-new-rev" in a for a in findings.anomalies)


def test_dependency_traversal_detection_and_fanout() -> None:
    """Verifies that downstream dependency errors are detected and trigger bounded fan-out."""
    primary_findings: dict[str, Findings] = {
        "logs": Findings(
            summary="Application logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="HTTP 403 Forbidden connecting to downstream service payment-svc",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Call to https://payment-svc.run.app/api/charge timed out",
                    timestamp=datetime(2026, 10, 15, 12, 33, 0, tzinfo=timezone.utc),
                ),
            ],
            anomalies=["Downstream call to payment-svc returned 403"],
            confidence=0.8,
        )
    }

    downstream_svcs = extract_downstream_dependencies(
        primary_service="order-api",
        findings=primary_findings,
        max_services=3,
    )
    assert "payment-svc" in downstream_svcs
    assert "order-api" not in downstream_svcs

    # Mock dependency inspection
    mock_client = MagicMock()
    mock_client.list_entries.return_value = [
        MagicMock(
            timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
            severity="ERROR",
            payload_excerpt="IAM permission denied: missing run.routes.invoke",
            httpRequest=None,
            http_request=None,
        )
    ]
    dep_findings = inspect_downstream_dependency(
        service_name="payment-svc",
        start_time=datetime(2026, 10, 15, 12, 20, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 15, 12, 50, 0, tzinfo=timezone.utc),
        client=mock_client,
    )

    assert isinstance(dep_findings, Findings)
    assert len(dep_findings.evidence) > 0


def test_downstream_dependency_attribution_and_caveats() -> None:
    """Verifies that downstream failures populate caveats attributing root cause to dependency."""
    primary_findings: dict[str, Findings] = {
        "logs": Findings(
            summary="Logs show 500s from downstream failure",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="payment-svc returned HTTP 403 Forbidden",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                )
            ],
            confidence=0.7,
        ),
        "dependency:payment-svc": Findings(
            summary="payment-svc inspection findings",
            evidence=[
                EvidenceItem(
                    kind="dependency_log_entry",
                    excerpt="[ERROR] [payment-svc] 403 Forbidden: Caller lacks Invoker role",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                )
            ],
            anomalies=["Downstream payment-svc IAM/permission authorization failure"],
            confidence=0.85,
        ),
    }

    rep = Report(
        summary="Service degradation on order-api",
        impact="Order processing failure",
        probable_root_cause=ProbableRootCause(
            text="order-api experienced error spike",
            confidence=0.80,
        ),
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=datetime(2026, 10, 15, 12, 20, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 15, 12, 50, 0, tzinfo=timezone.utc),
        onset_time=datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc),
    )

    calibrated = calibrate_report_confidence(rep, findings=primary_findings, plan=plan)
    assert any(
        "payment-svc" in c and "downstream dependency" in c.lower() for c in calibrated.caveats
    )


def test_unified_chronological_timeline_ordering() -> None:
    """Verifies strict chronological ordering of onset, changes, dependency, and ticket events."""
    incident = _create_sample_incident(
        opened_at=datetime(2026, 10, 15, 12, 45, 0, tzinfo=timezone.utc)
    )
    onset_ts = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    change_ts = datetime(2026, 10, 15, 12, 25, 0, tzinfo=timezone.utc)
    dep_ts = datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc)
    log_ts = datetime(2026, 10, 15, 12, 35, 0, tzinfo=timezone.utc)
    prior_ts = datetime(2026, 10, 15, 12, 5, 0, tzinfo=timezone.utc)

    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=change_ts - timedelta(minutes=5),
        end_time=datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc),
        onset_time=onset_ts,
    )
    setattr(
        plan,
        "prior_incidents",
        [
            PriorIncident(
                start_time=prior_ts,
                end_time=prior_ts + timedelta(minutes=5),
                error_count=3,
                description="Prior resolved burst",
            )
        ],
    )

    findings: dict[str, Findings] = {
        "changes": Findings(
            summary="Changes",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision order-api-v2 deployed by test@example.com",
                    timestamp=change_ts,
                )
            ],
            confidence=0.7,
        ),
        "dependency:payment-svc": Findings(
            summary="Payment svc",
            evidence=[
                EvidenceItem(
                    kind="dependency_log_entry",
                    excerpt="HTTP 403 Forbidden on payment-svc",
                    timestamp=dep_ts,
                )
            ],
            confidence=0.8,
        ),
        "logs": Findings(
            summary="Logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="HTTP 500 error logged",
                    timestamp=log_ts,
                )
            ],
            confidence=0.8,
        ),
    }

    synth = SynthesizerAgent()
    timeline = synth._build_timeline(incident=incident, findings=findings, plan=plan)

    # Assert timeline is strictly sorted by timestamp
    for i in range(len(timeline) - 1):
        assert timeline[i].timestamp <= timeline[i + 1].timestamp

    # Assert timestamps are strictly UTC
    for item in timeline:
        assert item.timestamp.tzinfo == timezone.utc

    descs = [item.description for item in timeline]

    # Verify presence and expected order of key timeline elements
    prior_idx = next(i for i, d in enumerate(descs) if "[PRIOR_INCIDENT]" in d)
    change_idx = next(i for i, d in enumerate(descs) if "[CHANGES]" in d)
    onset_idx = next(i for i, d in enumerate(descs) if "[ONSET]" in d)
    dep_idx = next(i for i, d in enumerate(descs) if "[DEPENDENCY: payment-svc]" in d)
    log_idx = next(i for i, d in enumerate(descs) if "[LOGS]" in d)
    opened_idx = next(i for i, d in enumerate(descs) if "Incident INC83001 opened" in d)

    assert prior_idx < change_idx < onset_idx < dep_idx < log_idx < opened_idx


def test_revision_causality_rule_post_onset_revision_not_causal_candidate() -> None:
    """Verifies that revisions deployed after symptom onset are classified as post-onset."""
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    post_onset_deploy_time = onset_time + timedelta(minutes=15)

    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    post_rev = MagicMock()
    post_rev.name = "order-api-hotfix-rev"
    post_rev.create_time = post_onset_deploy_time
    post_rev.traffic_percent = 100
    post_rev.template.containers = [MagicMock(image="gcr.io/test-proj/order-api:v3")]
    mock_client.list_revisions.return_value = [post_rev]
    mock_client.list_builds.return_value = []

    cfg = AgentConfig(revision_causality_window_minutes=30)
    specialist = ChangesSpecialist(project_id="test-proj", client=mock_client, config=cfg)
    incident = _create_sample_incident()
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=45),
        onset_time=onset_time,
    )

    findings = specialist.investigate(incident, plan=plan)

    # Post-onset revision should NOT be tagged as causal candidate
    assert not any("[causal candidate" in ev.excerpt for ev in findings.evidence)
    # It should be tagged as post-onset deployment: candidate mitigation
    assert any(
        "[post-onset deployment: candidate mitigation]" in ev.excerpt for ev in findings.evidence
    )
    assert any(
        "Post-onset revision deployment: order-api-hotfix-rev" in a for a in findings.anomalies
    )


def test_cloud_run_multi_hyphen_hostname_extraction() -> None:
    """Verifies that Cloud Run URLs with multi-hyphen service names are parsed accurately."""
    findings: dict[str, Findings] = {
        "logs": Findings(
            summary="Logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling https://payment-gateway-svc-414853093282.us-central1.run.app/checkout",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling https://user-auth-service-xyz123-uc.a.run.app/verify",
                    timestamp=datetime(2026, 10, 15, 12, 33, 0, tzinfo=timezone.utc),
                ),
            ],
            confidence=0.8,
        )
    }

    downstream_svcs = extract_downstream_dependencies(
        primary_service="order-api",
        findings=findings,
        max_services=3,
    )

    assert "payment-gateway-svc" in downstream_svcs
    assert "user-auth-service" in downstream_svcs


def test_dynamic_revision_causality_window_in_synthesizer(monkeypatch: object) -> None:
    """Verifies that calibrate_report_confidence uses dynamic config for causality window."""
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(minutes=40)

    # Create config with 45-minute causality window
    custom_cfg = AgentConfig(revision_causality_window_minutes=45)
    from agent.app import config as cfg_module

    monkeypatch.setattr(cfg_module, "get_settings", lambda: custom_cfg)  # type: ignore[attr-defined]

    rep = Report(
        summary="Revision deploy blamed",
        impact="Service degraded",
        probable_root_cause=ProbableRootCause(
            text="Service disruption caused by revision deployment",
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(timestamp=old_deploy_time, description="Revision order-api-old-rev")
        ],
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )

    # With 45-minute window, a deploy 40 minutes prior is within window
    calibrated = calibrate_report_confidence(rep, findings={}, plan=plan)
    assert not any("prior to the 45-minute causality window" in c for c in calibrated.caveats)

    # With 30-minute window, the 40-minute-old deploy is outside window
    default_cfg = AgentConfig(revision_causality_window_minutes=30)
    monkeypatch.setattr(cfg_module, "get_settings", lambda: default_cfg)  # type: ignore[attr-defined]
    calibrated_30 = calibrate_report_confidence(rep, findings={}, plan=plan)
    assert any("30-minute causality window" in c for c in calibrated_30.caveats)


@pytest.mark.parametrize(
    "operational_cause",
    [
        "Database connection pool exhaustion on /checkout",
        "IAM authorization loss resulting in downstream HTTP 403 Forbidden",
        "Network latency degradation on upstream gateway",
        "Container out of memory (OOM) memory exhaustion",
    ],
)
def test_operational_root_causes_not_capped_by_older_revisions(
    operational_cause: str,
) -> None:
    """Verifies confidence is NOT capped to 0.40 for operational root causes
    when revisions are older than causality window.
    """
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(minutes=45)

    rep = Report(
        summary="Service degradation observed",
        impact="Checkout endpoint errors",
        probable_root_cause=ProbableRootCause(
            text=operational_cause,
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(
                timestamp=old_deploy_time,
                description="Revision order-api-baseline deployed earlier",
            )
        ],
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )
    findings = {
        "logs": Findings(
            summary="Operational errors observed in logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=f"Operational failure logged: {operational_cause}",
                    timestamp=onset_time,
                )
            ],
            anomalies=[f"Anomalous telemetry signal: {operational_cause}"],
            confidence=0.85,
        ),
        "metrics": Findings(
            summary="Corroborating metrics signal",
            evidence=[
                EvidenceItem(
                    kind="metric_spike",
                    excerpt="Elevated latency and error rate",
                    timestamp=onset_time,
                )
            ],
            anomalies=["Metric deviation detected"],
            confidence=0.85,
        ),
    }

    calibrated = calibrate_report_confidence(rep, findings=findings, plan=plan)
    assert calibrated.probable_root_cause is not None
    # Confidence should NOT be capped down to 0.40
    assert calibrated.probable_root_cause.confidence == 0.85
    # No causality window caveat should be attached
    assert not any("causality window" in c.lower() for c in calibrated.caveats)


def test_revision_blamed_outside_causality_window_is_capped() -> None:
    """Verifies confidence IS capped to 0.40 if a revision is blamed
    but is outside the causality window.
    """
    onset_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(minutes=45)

    rep = Report(
        summary="Service outage following revision release",
        impact="Order processing failing",
        probable_root_cause=ProbableRootCause(
            text="Service disruption caused by revision deployment regression",
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(
                timestamp=old_deploy_time,
                description="Revision order-api-bad-build deployed earlier",
            )
        ],
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )

    calibrated = calibrate_report_confidence(rep, findings={}, plan=plan)
    assert calibrated.probable_root_cause is not None
    # Confidence should be capped to 0.40
    assert calibrated.probable_root_cause.confidence <= 0.40
    # Causality window caveat must be present
    assert any("causality window" in c.lower() for c in calibrated.caveats)


def test_onset_detection_ignores_health_latency_spikes() -> None:
    """Verifies that /health latency spikes (e.g. cold starts) are ignored by onset detection."""
    end_time = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)
    health_spike_time = datetime(2026, 10, 15, 12, 10, 0, tzinfo=timezone.utc)
    real_onset_time = datetime(2026, 10, 15, 12, 25, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                # Cloud Run cold-start latency spike on /health - should be ignored
                {
                    "timestamp": health_spike_time.isoformat(),
                    "status": 200,
                    "route": "/health",
                    "latency_ms": 3200.0,
                    "excerpt": "GET /health HTTP 200 (3200.0ms)",
                },
                # Real symptom onset on /checkout (HTTP 500)
                {
                    "timestamp": real_onset_time.isoformat(),
                    "status": 500,
                    "route": "/checkout",
                    "latency_ms": 150.0,
                    "excerpt": "POST /checkout HTTP 500",
                },
                # Subsequent 403 error on /checkout
                {
                    "timestamp": (real_onset_time + timedelta(minutes=2)).isoformat(),
                    "status": 403,
                    "route": "/checkout",
                    "latency_ms": 80.0,
                    "excerpt": "POST /checkout HTTP 403",
                },
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    # Onset time MUST be the real symptom time (12:25), NOT the benign /health cold start (12:10)
    assert res.onset_time == real_onset_time


def test_five_minute_quiescent_gap_cleanly_separates_episodes() -> None:
    """Verifies that a 5-minute quiescent gap cleanly separates episodes into
    prior incident and active onset.
    """
    end_time = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)
    ep1_start = datetime(2026, 10, 15, 12, 10, 0, tzinfo=timezone.utc)
    ep1_end = datetime(2026, 10, 15, 12, 12, 0, tzinfo=timezone.utc)
    # 6 minutes later (> 5 minute default quiescent gap)
    active_onset = datetime(2026, 10, 15, 12, 18, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                {
                    "timestamp": ep1_start.isoformat(),
                    "status": 500,
                    "route": "/orders",
                    "excerpt": "error 1",
                },
                {
                    "timestamp": ep1_end.isoformat(),
                    "status": 500,
                    "route": "/orders",
                    "excerpt": "error 2",
                },
                {
                    "timestamp": active_onset.isoformat(),
                    "status": 500,
                    "route": "/orders",
                    "excerpt": "error 3",
                },
            ]
        }

    # With default AgentConfig (prior_incident_quiescent_minutes = 5)
    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    assert res.onset_time == active_onset
    assert len(res.prior_incidents) == 1
    assert res.prior_incidents[0].start_time == ep1_start
    assert res.prior_incidents[0].end_time == ep1_end

    # In contrast, when error events are only 4 minutes apart (<= 5 minute gap), they cluster
    clustered_ep_time = ep1_start + timedelta(minutes=4)

    def mock_req_logs_close(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                {
                    "timestamp": ep1_start.isoformat(),
                    "status": 500,
                    "route": "/orders",
                    "excerpt": "error 1",
                },
                {
                    "timestamp": clustered_ep_time.isoformat(),
                    "status": 500,
                    "route": "/orders",
                    "excerpt": "error 2",
                },
            ]
        }

    res_close = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        request_logs_fn=mock_req_logs_close,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res_close.is_fallback
    assert res_close.onset_time == ep1_start
    assert len(res_close.prior_incidents) == 0


def test_query_request_logs_sample_entries_not_capped_at_10() -> None:
    """Verifies that query_request_logs returns sample_entries up to
    effective_limit, not capped at 10.
    """
    from agent.app.tools.logs import query_request_logs

    mock_client = MagicMock()
    mock_entries = []
    base_time = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(25):
        entry = MagicMock()
        entry.timestamp = base_time + timedelta(seconds=i * 10)
        entry.http_request = {
            "status": 500,
            "requestMethod": "GET",
            "requestUrl": f"https://order-api.run.app/orders/{i}",
            "latency": "0.15s",
        }
        mock_entries.append(entry)

    mock_client.list_entries.return_value = mock_entries

    result = query_request_logs(
        service_name="order-api",
        project_id="test-proj",
        max_entries=50,
        client=mock_client,
    )

    assert len(result["sample_entries"]) == 25
    assert result["sample_entries"][0]["status"] == 500
    assert result["sample_entries"][24]["status"] == 500

    mock_client.list_entries.return_value = mock_entries
    result_clamped = query_request_logs(
        service_name="order-api",
        project_id="test-proj",
        max_entries=15,
        client=mock_client,
    )
    assert len(result_clamped["sample_entries"]) == 15


def test_onset_detection_multi_episode_separation() -> None:
    """Verifies multi-episode telemetry separation with Episode X at T0 and Episode Y at T0+20m.
    - onset_res.onset_time is Episode Y's start time (20:54)
    - onset_res.prior_incidents contains Episode X (20:34)
    - onset_res.anchored_start_time is 20:54 - 10m = 20:44 (excluding Episode X)
    """
    t0 = datetime(2026, 10, 15, 20, 34, 0, tzinfo=timezone.utc)
    ep_x_end = t0 + timedelta(minutes=2)
    ep_y_start = t0 + timedelta(minutes=20)
    end_time = datetime(2026, 10, 15, 21, 5, 0, tzinfo=timezone.utc)

    ep_x_entries = [
        {
            "timestamp": (t0 + timedelta(seconds=i * 8)).isoformat(),
            "status": 500,
            "route": "/checkout",
            "excerpt": f"Episode X error {i}",
        }
        for i in range(15)
    ]
    ep_y_entries = [
        {
            "timestamp": (ep_y_start + timedelta(seconds=i * 10)).isoformat(),
            "status": 500,
            "route": "/checkout",
            "excerpt": f"Episode Y error {i}",
        }
        for i in range(5)
    ]

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {"sample_entries": ep_x_entries + ep_y_entries}

    onset_res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        prior_quiescent_minutes=5,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not onset_res.is_fallback
    assert onset_res.onset_time == ep_y_start
    assert len(onset_res.prior_incidents) == 1
    assert onset_res.prior_incidents[0].start_time == t0
    assert onset_res.anchored_start_time == ep_y_start - timedelta(minutes=10)
    assert onset_res.anchored_start_time == datetime(2026, 10, 15, 20, 44, 0, tzinfo=timezone.utc)
    assert onset_res.anchored_start_time > ep_x_end


def test_synthesizer_ruling_out_revisions_retains_high_confidence() -> None:
    """Verifies that a probable root cause explicitly ruling out revision deployments
    maintains high confidence and does NOT get capped to 0.40.
    """
    onset_time = datetime(2026, 10, 15, 20, 54, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(hours=4)

    rep = Report(
        summary="Database pool exhaustion during peak load",
        impact="API requests failing with 500s",
        probable_root_cause=ProbableRootCause(
            text=(
                "DatabaseConnectionError: Connection pool exhausted. The active revision "
                "cymbal-shop-api-00005-mlh was deployed hours earlier at 16:29:35Z, "
                "indicating that the failure was triggered by database connection pooling "
                "constraints under elevated concurrency rather than an immediate "
                "deployment regression."
            ),
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(
                timestamp=old_deploy_time,
                description="Revision cymbal-shop-api-00005-mlh deployed at 16:29:35Z",
            )
        ],
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )
    findings = {
        "logs": Findings(
            summary="Operational errors observed in logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Operational failure logged: DatabaseConnectionError",
                    timestamp=onset_time,
                )
            ],
            anomalies=["Anomalous telemetry signal: DatabaseConnectionError"],
            confidence=0.85,
        ),
        "metrics": Findings(
            summary="Corroborating metrics signal",
            evidence=[
                EvidenceItem(
                    kind="metric_spike",
                    excerpt="Elevated latency and error rate",
                    timestamp=onset_time,
                )
            ],
            anomalies=["Metric deviation detected"],
            confidence=0.85,
        ),
    }

    calibrated = calibrate_report_confidence(rep, findings=findings, plan=plan)

    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence == 0.85
    assert not any("causality window" in c.lower() for c in calibrated.caveats)


def test_synthesizer_actively_blaming_revision_is_capped() -> None:
    """Verifies that a probable root cause actively attributing the incident to a revision
    DOES get capped to 0.40 when the revision is outside the causality window.
    """
    onset_time = datetime(2026, 10, 15, 20, 54, 0, tzinfo=timezone.utc)
    old_deploy_time = onset_time - timedelta(minutes=45)

    rep = Report(
        summary="Service outage following revision release",
        impact="API requests failing with 500s",
        probable_root_cause=ProbableRootCause(
            text=(
                "The outage was caused by deployment of revision cymbal-shop-api-00005-mlh "
                "introducing broken configuration"
            ),
            confidence=0.85,
        ),
        recent_changes=[
            RecentChangeItem(
                timestamp=old_deploy_time,
                description="Revision cymbal-shop-api-00005-mlh deployed earlier",
            )
        ],
    )
    plan = InvestigationPlan(
        affected_service="order-api",
        start_time=onset_time - timedelta(minutes=10),
        end_time=onset_time + timedelta(minutes=30),
        onset_time=onset_time,
    )

    calibrated = calibrate_report_confidence(rep, findings={}, plan=plan)

    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.40
    assert any("causality window" in c.lower() for c in calibrated.caveats)


def test_query_request_logs_with_min_latency_ms() -> None:
    """Verifies that query_request_logs properly adds httpRequest.latency > '2.0s'
    to the logging filter.
    """
    from agent.app.tools.logs import query_request_logs

    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    res = query_request_logs(
        service_name="order-api",
        project_id="test-proj",
        min_latency_ms=2000,
        client=mock_client,
    )

    assert 'httpRequest.latency > "2.0s"' in res.get("query_filter", "")
    mock_client.list_entries.assert_called_once()
    call_kwargs = mock_client.list_entries.call_args.kwargs
    assert 'httpRequest.latency > "2.0s"' in call_kwargs.get("filter_", "")


def test_onset_detection_from_pure_latency_spike() -> None:
    """Verifies that onset detection identifies a pure latency spike (HTTP 200, latency > 2000ms)
    on /checkout as onset_time.
    """
    end_time = datetime(2026, 10, 15, 22, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 15, 21, 56, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        status_class = kwargs.get("status_class")
        min_lat = kwargs.get("min_latency_ms")
        if status_class:
            return {"sample_entries": []}
        if min_lat:
            return {
                "sample_entries": [
                    {
                        "timestamp": onset_ts.isoformat(),
                        "status": 200,
                        "route": "/checkout",
                        "latency_ms": 3500.0,
                        "excerpt": "POST /checkout HTTP 200 (3500.0ms)",
                    },
                    {
                        "timestamp": (onset_ts + timedelta(minutes=2)).isoformat(),
                        "status": 200,
                        "route": "/checkout",
                        "latency_ms": 4800.0,
                        "excerpt": "POST /checkout HTTP 200 (4800.0ms)",
                    },
                ]
            }
        return {"sample_entries": []}

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)
    assert len(res.prior_incidents) == 0


def test_onset_detection_latency_spike_separates_prior_500_episode() -> None:
    """Verifies that an Episode Y latency spike separates an earlier Episode X 500 error episode
    into prior_incidents and excludes Episode X from anchored_start_time.
    """
    ep_x_start = datetime(2026, 10, 15, 21, 35, 0, tzinfo=timezone.utc)
    ep_x_end = datetime(2026, 10, 15, 21, 37, 0, tzinfo=timezone.utc)
    ep_y_start = datetime(2026, 10, 15, 21, 56, 0, tzinfo=timezone.utc)
    end_time = datetime(2026, 10, 15, 22, 5, 0, tzinfo=timezone.utc)

    ep_x_entries = [
        {
            "timestamp": ep_x_start.isoformat(),
            "status": 500,
            "route": "/checkout",
            "latency_ms": 120.0,
            "excerpt": "POST /checkout HTTP 500",
        },
        {
            "timestamp": ep_x_end.isoformat(),
            "status": 500,
            "route": "/checkout",
            "latency_ms": 150.0,
            "excerpt": "POST /checkout HTTP 500",
        },
    ]

    ep_y_entries = [
        {
            "timestamp": ep_y_start.isoformat(),
            "status": 200,
            "route": "/checkout",
            "latency_ms": 3500.0,
            "excerpt": "POST /checkout HTTP 200 (3500.0ms)",
        },
        {
            "timestamp": (ep_y_start + timedelta(minutes=2)).isoformat(),
            "status": 200,
            "route": "/checkout",
            "latency_ms": 4800.0,
            "excerpt": "POST /checkout HTTP 200 (4800.0ms)",
        },
    ]

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        status_class = kwargs.get("status_class")
        min_lat = kwargs.get("min_latency_ms")
        if status_class:
            return {"sample_entries": ep_x_entries}
        if min_lat:
            return {"sample_entries": ep_y_entries}
        return {"sample_entries": ep_x_entries + ep_y_entries}

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        prior_quiescent_minutes=5,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert not res.is_fallback
    assert res.onset_time == ep_y_start
    assert len(res.prior_incidents) == 1
    assert res.prior_incidents[0].start_time == ep_x_start
    assert res.prior_incidents[0].end_time == ep_x_end
    expected_anchored = ep_y_start - timedelta(minutes=10)
    assert res.anchored_start_time == expected_anchored
    assert res.anchored_start_time > ep_x_end


def test_onset_detection_from_latency_metrics() -> None:
    """Verifies that onset detection triggers when Cloud Monitoring latency metric
    P99 exceeds 2000ms in a sustained deviation.
    """
    end_time = datetime(2026, 10, 15, 22, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 15, 21, 55, 0, tzinfo=timezone.utc)

    def mock_metrics(**kwargs: object) -> dict[str, object]:
        return {
            "points": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "value": 4500.0,
                    "kind": "latency",
                },
                {
                    "timestamp": (onset_ts + timedelta(minutes=1)).isoformat(),
                    "value": 4200.0,
                    "kind": "latency",
                },
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=mock_metrics,
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)


def test_dependency_traversal_healthy_downstream_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that inspect_downstream_dependency on a service with 0 errors
    returns healthy evidence and summary.
    """
    mock_client = MagicMock()
    start_time = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    end_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)

    # Mock query_request_logs to return healthy request count with 0 errors
    monkeypatch.setattr(
        "agent.app.tools.logs.query_request_logs",
        lambda **kwargs: {
            "total_requests": 250,
            "status_breakdown": {"2xx": 250, "4xx": 0, "5xx": 0},
            "console_url": "https://console.cloud.google.com/logs",
        },
    )
    # Mock query_logs to return empty entries (no warning or error logs)
    monkeypatch.setattr(
        "agent.app.tools.logs.query_logs",
        lambda **kwargs: {"entries": [], "console_url": "https://console.cloud.google.com/logs"},
    )
    # Mock describe_cloud_run_service to return active service
    monkeypatch.setattr(
        "agent.app.tools.resources.describe_cloud_run_service",
        lambda **kwargs: {
            "status": "ACTIVE",
            "uri": "https://inventory-svc-xyz.a.run.app",
            "ingress": "all",
            "console_url": "https://console.cloud.google.com/run",
        },
    )

    dep_findings = inspect_downstream_dependency(
        service_name="inventory-svc",
        start_time=start_time,
        end_time=end_time,
        client=mock_client,
    )

    assert isinstance(dep_findings, Findings)
    assert len(dep_findings.anomalies) == 0
    assert len(dep_findings.errors) == 0
    assert dep_findings.summary == (
        "Downstream dependency investigation for inventory-svc: "
        "verified healthy with 0 errors and no latency anomalies."
    )
    health_ev = [e for e in dep_findings.evidence if e.kind == "dependency_health_check"]
    assert len(health_ev) == 1
    assert health_ev[0].excerpt == (
        "Downstream dependency inventory-svc verified healthy: "
        "250 requests, 0 HTTP errors (4xx: 0, 5xx: 0), no latency anomalies."
    )


def test_synthesizer_healthy_dependency_caveat() -> None:
    """Verify that when a downstream dependency is healthy, synthesizer attaches
    the healthy dependency caveat and does not attribute root cause to downstream dependencies.
    """
    incident = _create_sample_incident(service_hint="shop-api")
    start_time = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    end_time = datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    onset_time = datetime(2026, 10, 15, 12, 10, 0, tzinfo=timezone.utc)

    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Latency Spike on shop-api"],
        start_time=start_time,
        end_time=end_time,
        onset_time=onset_time,
        selected_specialists=["metrics"],
        initial_hypotheses=["shop-api internal delay"],
    )

    healthy_dep_findings = Findings(
        summary=(
            "Downstream dependency investigation for inventory-svc: "
            "verified healthy with 0 errors and no latency anomalies."
        ),
        evidence=[
            EvidenceItem(
                kind="dependency_health_check",
                excerpt=(
                    "Downstream dependency inventory-svc verified healthy: "
                    "250 requests, 0 HTTP errors (4xx: 0, 5xx: 0), no latency anomalies."
                ),
                timestamp=start_time,
            )
        ],
        anomalies=[],
        confidence=0.40,
        errors=[],
    )

    primary_findings: dict[str, Findings] = {
        "metrics": Findings(
            summary="p95 latency spike ~8s on shop-api",
            evidence=[
                EvidenceItem(
                    kind="latency_metric",
                    excerpt="shop-api request latency p95 reached 8200ms",
                    timestamp=onset_time,
                )
            ],
            anomalies=["Latency spike to 8200ms"],
            confidence=0.80,
        ),
        "dependency:inventory-svc": healthy_dep_findings,
    }

    synthesizer = SynthesizerAgent()
    report = synthesizer.synthesize(incident=incident, plan=plan, findings=primary_findings)

    expected_caveat = (
        "Downstream dependency 'inventory-svc' was inspected and verified healthy "
        "(0 errors, no anomalies); delay originates in primary service request processing path, "
        "not downstream dependencies."
    )
    assert any(expected_caveat in c for c in report.caveats)
    assert report.probable_root_cause is not None
    assert "inventory-svc" not in report.probable_root_cause.text or (
        "not downstream dependencies" in report.probable_root_cause.text
    )

    # Test calibrate_report_confidence down-calibrates if report blamed healthy downstream
    spurious_report = Report(
        summary="Latency on shop-api",
        impact="Slow checkouts",
        probable_root_cause=ProbableRootCause(
            text=(
                "External backend dependency saturation on inventory-svc caused the latency spike"
            ),
            confidence=0.85,
        ),
    )
    calibrated = calibrate_report_confidence(spurious_report, findings=primary_findings, plan=plan)
    assert any(expected_caveat in c for c in calibrated.caveats)
    assert calibrated.probable_root_cause is not None
    assert calibrated.probable_root_cause.confidence <= 0.40
    assert any("inventory-svc" in c and "verified healthy" in c for c in calibrated.caveats)


def test_report_metadata_contains_detected_onset() -> None:
    """Verify that report.metadata contains detected_onset matching plan.onset_time
    alongside window_start.
    """
    incident = _create_sample_incident(service_hint="shop-api")
    onset_dt = datetime(2026, 10, 15, 12, 35, 0, tzinfo=timezone.utc)
    start_dt = datetime(2026, 10, 15, 12, 25, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)

    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Latency spike"],
        start_time=start_dt,
        end_time=end_dt,
        onset_time=onset_dt,
        selected_specialists=["metrics"],
    )

    synthesizer = SynthesizerAgent()
    report = synthesizer.synthesize(incident=incident, plan=plan, findings={})

    assert "detected_onset" in report.metadata
    assert report.metadata["detected_onset"] == onset_dt.isoformat()
    assert report.metadata["window_start"] == start_dt.isoformat()
    assert report.metadata["window_end"] == end_dt.isoformat()
    assert report.metadata["detected_onset"] != report.metadata["window_start"]

    # When plan has no onset_time, detected_onset is None
    plan_no_onset = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Latency spike"],
        start_time=start_dt,
        end_time=end_dt,
        onset_time=None,
        selected_specialists=["metrics"],
    )
    report_no_onset = synthesizer.synthesize(incident=incident, plan=plan_no_onset, findings={})
    assert report_no_onset.metadata["detected_onset"] is None


def test_cloud_run_hostname_resolution_forms() -> None:
    """Verifies resolution of Cloud Run hostnames to canonical service names."""
    # Form A: <service>-<digits>.<region>.run.app (project number suffix)
    assert (
        resolve_run_app_service_name("cymbal-shop-api-569406313510.us-central1.run.app")
        == "cymbal-shop-api"
    )
    assert (
        resolve_run_app_service_name(
            "https://cymbal-shop-api-569406313510.us-central1.run.app/checkout"
        )
        == "cymbal-shop-api"
    )
    assert resolve_run_app_service_name("shop-api-569406313510.run.app") == "shop-api"
    assert resolve_run_app_service_name("cymbal-shop-api-569406313510") == "cymbal-shop-api"

    # Form B: <service>-<tag>-<region_code>.a.run.app (zero digits in tag)
    assert (
        resolve_run_app_service_name("cymbal-shop-api-btcbwtmvvq-uc.a.run.app") == "cymbal-shop-api"
    )
    assert (
        resolve_run_app_service_name("https://cymbal-shop-api-btcbwtmvvq-uc.a.run.app:443/orders")
        == "cymbal-shop-api"
    )
    assert (
        resolve_run_app_service_name("cymbal-shop-api-btcbwtmvvq-uc.run.app") == "cymbal-shop-api"
    )
    assert resolve_run_app_service_name("cymbal-shop-api-btcbwtmvvq-uc") == "cymbal-shop-api"

    # Form B: Alphanumeric tag with digits
    assert (
        resolve_run_app_service_name("user-auth-service-xyz123-uc.a.run.app") == "user-auth-service"
    )
    assert (
        resolve_run_app_service_name(
            "https://payment-gateway-svc-414853093282.us-central1.run.app/checkout"
        )
        == "payment-gateway-svc"
    )

    # Plain services and standard hostnames
    assert resolve_run_app_service_name("inventory-svc") == "inventory-svc"
    assert resolve_run_app_service_name("http://backend-service:8080") == "backend-service"


def test_self_dependency_exclusion_form_a_and_form_b() -> None:
    """Verifies that self-referential Cloud Run URLs (Form A and Form B) are strictly excluded."""
    # Findings contain self-referential URLs in both Form A and Form B
    findings: dict[str, Findings] = {
        "logs": Findings(
            summary="Application logs with self URLs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling self Form A: https://cymbal-shop-api-569406313510.us-central1.run.app/checkout",
                    timestamp=datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling self Form B: https://cymbal-shop-api-btcbwtmvvq-uc.a.run.app/orders",
                    timestamp=datetime(2026, 10, 15, 12, 31, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling self short Form A: https://shop-api-569406313510.run.app/api",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling self short Form B: https://shop-api-btcbwtmvvq-uc.a.run.app/api",
                    timestamp=datetime(2026, 10, 15, 12, 33, 0, tzinfo=timezone.utc),
                ),
            ],
            anomalies=[
                "downstream service cymbal-shop-api-btcbwtmvvq-uc returned 500",
                "call to target service shop-api timed out",
            ],
            confidence=0.8,
        )
    }

    # Verify when primary service is 'shop-api'
    downstream_from_short = extract_downstream_dependencies(
        primary_service="shop-api",
        findings=findings,
        max_services=5,
    )
    assert "cymbal-shop-api" not in downstream_from_short
    assert "shop-api" not in downstream_from_short
    assert "cymbal-shop-api-btcbwtmvvq-uc" not in downstream_from_short
    assert "cymbal-shop-api-569406313510" not in downstream_from_short
    assert len(downstream_from_short) == 0

    # Verify when primary service is 'cymbal-shop-api'
    downstream_from_full = extract_downstream_dependencies(
        primary_service="cymbal-shop-api",
        findings=findings,
        max_services=5,
    )
    assert "cymbal-shop-api" not in downstream_from_full
    assert "shop-api" not in downstream_from_full
    assert "cymbal-shop-api-btcbwtmvvq-uc" not in downstream_from_full
    assert "cymbal-shop-api-569406313510" not in downstream_from_full
    assert len(downstream_from_full) == 0


def test_real_downstream_services_extracted_while_self_excluded() -> None:
    """Verifies that genuine downstream services are extracted while self hostnames are excluded."""
    findings: dict[str, Findings] = {
        "logs": Findings(
            summary="Application logs containing self and external downstream services",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Forwarding request to https://cymbal-shop-api-btcbwtmvvq-uc.a.run.app/cart",
                    timestamp=datetime(2026, 10, 15, 12, 30, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Forwarding request to https://shop-api-569406313510.us-central1.run.app/cart",
                    timestamp=datetime(2026, 10, 15, 12, 31, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling https://inventory-svc-569406313510.us-central1.run.app/stock/check",
                    timestamp=datetime(2026, 10, 15, 12, 32, 0, tzinfo=timezone.utc),
                ),
                EvidenceItem(
                    kind="log_entry",
                    excerpt="Calling https://payment-gateway-svc-btcbwtmvvq-uc.a.run.app/charge",
                    timestamp=datetime(2026, 10, 15, 12, 33, 0, tzinfo=timezone.utc),
                ),
            ],
            anomalies=[
                "downstream service order-fulfillment-svc returned 502 Bad Gateway",
            ],
            confidence=0.85,
        )
    }

    downstream_svcs = extract_downstream_dependencies(
        primary_service="shop-api",
        findings=findings,
        max_services=5,
    )

    # Real downstream services must be present
    assert "inventory-svc" in downstream_svcs
    assert "payment-gateway-svc" in downstream_svcs
    assert "order-fulfillment-svc" in downstream_svcs

    # Self references must be strictly excluded
    assert "shop-api" not in downstream_svcs
    assert "cymbal-shop-api" not in downstream_svcs
    assert "cymbal-shop-api-btcbwtmvvq-uc" not in downstream_svcs
    assert "shop-api-569406313510" not in downstream_svcs


def test_isolated_cold_start_latency_blip_does_not_trigger_onset() -> None:
    """Verifies that a single isolated P99 metric point > 2000 ms (e.g. 2012 ms)
    with no subsequent points and no request log errors does NOT trigger onset,
    falling back or detecting subsequent real error onset.
    """
    end_time = datetime(2026, 10, 15, 23, 10, 0, tzinfo=timezone.utc)
    cold_start_ts = datetime(2026, 10, 15, 22, 57, 0, tzinfo=timezone.utc)

    # 1. Isolated blip with no other telemetry falls back to lookback window
    res_isolated = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {
            "points": [
                {
                    "timestamp": cold_start_ts.isoformat(),
                    "value": 2012.5,
                    "kind": "latency",
                }
            ]
        },
    )
    assert res_isolated.is_fallback
    assert res_isolated.onset_time is None
    assert res_isolated.anchored_start_time == end_time - timedelta(minutes=60)

    # 2. Isolated blip at 22:57 followed by real error onset at 23:02 (outside 3m window)
    real_onset_ts = datetime(2026, 10, 15, 23, 2, 0, tzinfo=timezone.utc)
    res_with_real_errors = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {
            "sample_entries": [
                {
                    "timestamp": real_onset_ts.isoformat(),
                    "status": 500,
                    "excerpt": "HTTP 500 internal error",
                },
                {
                    "timestamp": (real_onset_ts + timedelta(minutes=1)).isoformat(),
                    "status": 500,
                    "excerpt": "HTTP 500 internal error",
                },
            ]
        },
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {
            "points": [
                {
                    "timestamp": cold_start_ts.isoformat(),
                    "value": 2012.5,
                    "kind": "latency",
                }
            ]
        },
    )
    assert not res_with_real_errors.is_fallback
    assert res_with_real_errors.onset_time == real_onset_ts
    assert res_with_real_errors.anchored_start_time == real_onset_ts - timedelta(minutes=10)


def test_sustained_latency_metric_deviation_triggers_onset() -> None:
    """Verifies that multiple consecutive P99 metric points > 2000 ms
    (e.g. 2 points spaced 1 minute apart) properly trigger onset.
    """
    end_time = datetime(2026, 10, 15, 23, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 15, 22, 55, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 15, 22, 56, 0, tzinfo=timezone.utc)

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {
            "points": [
                {
                    "timestamp": t1.isoformat(),
                    "value": 2500.0,
                    "kind": "latency",
                },
                {
                    "timestamp": t2.isoformat(),
                    "value": 2600.0,
                    "kind": "latency",
                },
            ]
        },
    )

    assert not res.is_fallback
    assert res.onset_time == t1
    assert res.anchored_start_time == t1 - timedelta(minutes=10)


def test_single_latency_metric_point_corroborated_by_request_log_triggers_onset() -> None:
    """Verifies that an isolated P99 metric point > 2000 ms is accepted when
    corroborated by high-latency request logs or error logs within 3 minutes.
    """
    end_time = datetime(2026, 10, 15, 23, 10, 0, tzinfo=timezone.utc)
    metric_ts = datetime(2026, 10, 15, 22, 55, 0, tzinfo=timezone.utc)
    req_ts = datetime(2026, 10, 15, 22, 56, 0, tzinfo=timezone.utc)

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {
            "sample_entries": [
                {
                    "timestamp": req_ts.isoformat(),
                    "status": 200,
                    "route": "/checkout",
                    "latency_ms": 2500.0,
                    "excerpt": "POST /checkout HTTP 200 (2500.0ms)",
                }
            ]
        },
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {
            "points": [
                {
                    "timestamp": metric_ts.isoformat(),
                    "value": 2800.0,
                    "kind": "latency",
                }
            ]
        },
    )

    assert not res.is_fallback
    assert res.onset_time == metric_ts
    assert res.anchored_start_time == metric_ts - timedelta(minutes=10)


def test_query_request_logs_newest_first_and_excludes_health() -> None:
    """Verifies that query_request_logs includes the NOT httpRequest.requestUrl =~ "/health"
    filter and orders descending by timestamp, while allowing /health if explicitly queried.
    """
    from agent.app.tools.logs import query_request_logs

    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    # 1. Default call (route=None)
    res = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        client=mock_client,
    )

    assert '(NOT httpRequest.requestUrl =~ "/health")' in res["query_filter"]
    mock_client.list_entries.assert_called_once()
    call_args = mock_client.list_entries.call_args
    assert call_args.kwargs.get("order_by") == "timestamp desc"
    assert '(NOT httpRequest.requestUrl =~ "/health")' in call_args.kwargs.get("filter_", "")

    # 2. Explicit /health route
    mock_client.reset_mock()
    res_health = query_request_logs(
        service_name="shop-api",
        project_id="test-proj",
        route="/health",
        client=mock_client,
    )
    assert '(NOT httpRequest.requestUrl =~ "/health")' not in res_health["query_filter"]
    assert 'httpRequest.requestUrl =~ "/health"' in res_health["query_filter"]
    mock_client.list_entries.assert_called_once()
    assert mock_client.list_entries.call_args.kwargs.get("order_by") == "timestamp desc"


def test_synthesizer_does_not_leak_prior_incident_mechanism() -> None:
    """Verifies that when findings contain prior database errors before onset and current
    telemetry shows pure latency, the synthesized root cause does not cite database connection
    pool exhaustion.
    """
    from agent.app.llm import GeminiLlmClient
    from agent.app.synthesizer import SynthesizerAgent

    now = datetime(2026, 10, 15, 20, 0, 0, tzinfo=timezone.utc)
    onset_time = now - timedelta(minutes=15)
    prior_time = onset_time - timedelta(minutes=30)

    incident = Incident(
        id="inc-latency-s2",
        source="servicenow",
        external_id="INC001234",
        external_key="INC001234",
        title="High latency on shop-api checkout",
        description="P95 latency exceeding SLO threshold with client timeouts.",
        severity="P2",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Latency / response time degradation", "Client request timeouts"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        onset_time=onset_time,
        selected_specialists=["logs", "metrics"],
    )

    findings = {
        "logs": Findings(
            summary="Prior resolved database errors and active high-latency requests.",
            evidence=[
                # Prior incident database error strictly before onset
                EvidenceItem(
                    kind="error_log",
                    excerpt=(
                        "[PRIOR_INCIDENT] DatabaseConnectionError: connection pool exhausted at "
                        "connection acquisition delays at shop_app.py line 210"
                    ),
                    timestamp=prior_time,
                ),
                # Active request logs: HTTP 200 with elevated latency, 0 errors
                EvidenceItem(
                    kind="request_log",
                    excerpt="GET /checkout HTTP 200 (latency: 8074ms)",
                    timestamp=onset_time + timedelta(minutes=2),
                ),
            ],
            anomalies=["Elevated request latency on /checkout"],
            confidence=0.85,
        ),
        "metrics": Findings(
            summary="Extreme latency shift observed on Cloud Run requests.",
            evidence=[
                EvidenceItem(
                    kind="latency_metric",
                    excerpt=(
                        'Metric metric.type = "run.googleapis.com/request_latencies": '
                        "current p95=8074ms vs baseline p95=99.6ms (+8003.4% shift)"
                    ),
                    timestamp=onset_time,
                )
            ],
            anomalies=["P95 latency deviation above threshold"],
            confidence=0.85,
        ),
    }

    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    simulated_report = Report(
        summary="Latency degradation on shop-api checkout endpoint.",
        impact="Users experiencing slow response times.",
        probable_root_cause=ProbableRootCause(
            text=(
                "Elevated request latency caused by connection acquisition delays and database "
                "connection pool exhaustion at shop_app.py line 210."
            ),
            confidence=0.85,
        ),
        recommended_actions=[
            "Profile internal request processing pipelines and worker thread pool settings."
        ],
        metadata={},
    )
    mock_response.text = simulated_report.model_dump_json()
    mock_genai_client.models.generate_content.return_value = mock_response

    llm_client = GeminiLlmClient(
        project_id="test-proj",
        location="us-central1",
        client=mock_genai_client,
    )
    synthesizer = SynthesizerAgent(llm_client=llm_client)
    report = synthesizer.synthesize(incident=incident, plan=plan, findings=findings)

    # Verify Gemini prompt contains segregated prior incident evidence and critical instructions
    generate_content_kwargs = mock_genai_client.models.generate_content.call_args.kwargs
    prompt_contents = generate_content_kwargs.get("contents", "")
    assert "[PRIOR RESOLVED INCIDENT EVIDENCE - DO NOT USE AS ACTIVE MECHANISM]" in prompt_contents
    assert "CRITICAL: Evidence and error logs" in prompt_contents

    # Verify root cause does NOT cite database connection pool exhaustion or acquisition delays
    assert report.probable_root_cause is not None
    prc_text = report.probable_root_cause.text.lower()
    assert "connection pool" not in prc_text
    assert "database connection" not in prc_text
    assert "pool exhaustion" not in prc_text
    assert "connection acquisition" not in prc_text

    # Verify root cause attributes to internal request processing delay/thread starvation
    assert "internal request processing delay" in prc_text or "thread starvation" in prc_text

    # Verify caveat explaining earlier resolved exceptions is present
    assert any(
        "prior to detected symptom onset and represent a resolved prior incident" in c
        or "earlier resolved exceptions are not the active incident mechanism" in c
        for c in report.caveats
    )


def test_latency_episode_closes_and_separates_from_subsequent_oom_incident() -> None:
    """Verifies that an earlier latency surge (23:20 to 23:27) closes when symptoms
    subside, and a subsequent OOM container termination error log at 23:44 after a
    15-minute quiet period starts a new episode, setting detected onset to 23:44,
    separating the latency surge into prior_incidents, and anchoring the investigation
    window to 23:44.
    """
    end_time = datetime(2026, 10, 15, 23, 50, 0, tzinfo=timezone.utc)
    t_lat_start = datetime(2026, 10, 15, 23, 20, 0, tzinfo=timezone.utc)
    t_lat_end = datetime(2026, 10, 15, 23, 27, 0, tzinfo=timezone.utc)
    t_oom = datetime(2026, 10, 15, 23, 44, 0, tzinfo=timezone.utc)

    # 1. Latency surge from 23:20 to 23:27 with high latency requests
    lat_req_entries = [
        {
            "timestamp": t_lat_start.isoformat(),
            "status": 200,
            "route": "/api/checkout",
            "latency_ms": 3200.0,
            "excerpt": "POST /api/checkout HTTP 200 (3200ms)",
        },
        {
            "timestamp": (t_lat_start + timedelta(minutes=3)).isoformat(),
            "status": 200,
            "route": "/api/checkout",
            "latency_ms": 3800.0,
            "excerpt": "POST /api/checkout HTTP 200 (3800ms)",
        },
        {
            "timestamp": t_lat_end.isoformat(),
            "status": 200,
            "route": "/api/checkout",
            "latency_ms": 4100.0,
            "excerpt": "POST /api/checkout HTTP 200 (4100ms)",
        },
    ]

    # Baseline requests during quiet period (latency <= 200ms, HTTP 200)
    baseline_req_entries = [
        {
            "timestamp": (t_lat_end + timedelta(minutes=5)).isoformat(),
            "status": 200,
            "route": "/api/checkout",
            "latency_ms": 120.0,
            "excerpt": "POST /api/checkout HTTP 200 (120ms)",
        },
        {
            "timestamp": (t_lat_end + timedelta(minutes=10)).isoformat(),
            "status": 200,
            "route": "/api/checkout",
            "latency_ms": 110.0,
            "excerpt": "POST /api/checkout HTTP 200 (110ms)",
        },
    ]

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        min_lat = kwargs.get("min_latency_ms")
        if min_lat:
            return {"sample_entries": lat_req_entries}
        return {"sample_entries": lat_req_entries + baseline_req_entries}

    # 2. Latency metrics during 23:20-23:27 surge, baseline metrics afterwards
    metric_points = [
        {"timestamp": t_lat_start.isoformat(), "value": 3200.0, "kind": "latency"},
        {
            "timestamp": (t_lat_start + timedelta(minutes=2)).isoformat(),
            "value": 3600.0,
            "kind": "latency",
        },
        {
            "timestamp": (t_lat_start + timedelta(minutes=5)).isoformat(),
            "value": 3900.0,
            "kind": "latency",
        },
        {"timestamp": t_lat_end.isoformat(), "value": 4100.0, "kind": "latency"},
        # 15-minute quiet period: normal baseline latency
        {
            "timestamp": (t_lat_end + timedelta(minutes=5)).isoformat(),
            "value": 150.0,
            "kind": "latency",
        },
        {
            "timestamp": (t_lat_end + timedelta(minutes=10)).isoformat(),
            "value": 140.0,
            "kind": "latency",
        },
        {
            "timestamp": (t_lat_end + timedelta(minutes=15)).isoformat(),
            "value": 160.0,
            "kind": "latency",
        },
    ]

    # 3. Subsequent OOM container termination error log at 23:44
    oom_log_entry = {
        "timestamp": t_oom.isoformat(),
        "severity": "ERROR",
        "payload_excerpt": "Memory limit exceeded. Container terminated.",
    }

    captured_logs_kwargs: dict[str, object] = {}

    def mock_logs(**kwargs: object) -> dict[str, object]:
        captured_logs_kwargs.update(kwargs)
        return {"entries": [oom_log_entry]}

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        prior_quiescent_minutes=5,
        request_logs_fn=mock_req_logs,
        logs_fn=mock_logs,
        metrics_fn=lambda **kw: {"points": metric_points},
    )

    # 1. Detected onset is 23:44 (the OOM event)
    assert not res.is_fallback
    assert res.onset_time == t_oom

    # 2. The latency surge is separated into prior_incidents
    assert len(res.prior_incidents) == 1
    assert res.prior_incidents[0].start_time == t_lat_start
    assert res.prior_incidents[0].end_time == t_lat_end

    # 3. The investigation window is anchored to 23:44 (-10m pre_buffer)
    expected_anchored = t_oom - timedelta(minutes=10)
    assert res.anchored_start_time == expected_anchored
    assert res.anchored_start_time == datetime(2026, 10, 15, 23, 34, 0, tzinfo=timezone.utc)
    assert res.anchored_start_time > t_lat_end

    # 4. Logs query used order_by="timestamp desc" and max_entries=200
    assert captured_logs_kwargs.get("order_by") == "timestamp desc"
    assert captured_logs_kwargs.get("max_entries") == 200


def test_detect_onset_queries_logs_newest_first_with_200_max_entries() -> None:
    """Verifies that detect_onset_from_telemetry passes order_by='timestamp desc'
    and max_entries=200 when querying application logs via query_logs.
    """
    mock_client = MagicMock()
    mock_client.list_entries.return_value = []

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=datetime(2026, 10, 15, 23, 50, 0, tzinfo=timezone.utc),
        window_minutes=60,
        logging_client=mock_client,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        metrics_fn=lambda **kw: {"points": []},
    )

    assert res.is_fallback
    assert mock_client.list_entries.called
    call_kwargs = mock_client.list_entries.call_args.kwargs
    assert call_kwargs.get("order_by") == "timestamp desc"
    assert call_kwargs.get("page_size") == 200


def test_platform_events_ingested_for_onset_detection() -> None:
    """Verifies that platform events (e.g. OOM kills) are ingested for onset detection
    when application logs have zero error entries.
    """
    from unittest.mock import patch

    end_time = datetime(2026, 10, 16, 0, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 15, 23, 44, 0, tzinfo=timezone.utc)

    def mock_platform_events(**kwargs: object) -> dict[str, object]:
        return {
            "events": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "message": "Memory limit of 1024 MiB exceeded with 1032 MiB used",
                }
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_platform_events,
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)
    assert res.end_time == end_time
    assert len(res.prior_incidents) == 0

    # Also verify structured restarts dict format from resources.py
    def mock_platform_events_structured(**kwargs: object) -> dict[str, object]:
        return {
            "service_name": "order-api",
            "total_events": 1,
            "restarts": {
                "count": 1,
                "events": [
                    {
                        "timestamp": onset_ts.isoformat(),
                        "reason": "OOM termination (memory limit exceeded)",
                        "message": "Memory limit of 1024 MiB exceeded with 1032 MiB used",
                    }
                ],
            },
            "probe_failures": {"count": 0, "events": []},
            "system_events": {"count": 0, "events": []},
            "has_oom": True,
        }

    res_structured = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_platform_events_structured,
    )

    assert not res_structured.is_fallback
    assert res_structured.onset_time == onset_ts

    # Also verify via direct patch of get_platform_events when platform_events_fn is None
    with patch(
        "agent.app.tools.resources.get_platform_events",
        return_value={
            "events": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "message": "Memory limit of 1024 MiB exceeded with 1032 MiB used",
                }
            ]
        },
    ):
        res_patched = detect_onset_from_telemetry(
            service_name="order-api",
            end_time=end_time,
            window_minutes=60,
            pre_buffer_minutes=10,
            request_logs_fn=lambda **kw: {"sample_entries": []},
            logs_fn=lambda **kw: {"entries": []},
            metrics_fn=lambda **kw: {"points": []},
        )
        assert not res_patched.is_fallback
        assert res_patched.onset_time == onset_ts


@pytest.mark.parametrize(
    "kept_text",
    [
        "OOM",
        "exit 137",
        "probe failure",
        "container termination",
        "Memory limit exceeded",
        "terminated by kernel OOM-killer",
        "container terminated",
        "container failed",
        "readiness probe failed",
        "startup probe failed",
        "failed to start and listen",
    ],
)
def test_platform_events_kept_symptom_onset(kept_text: str) -> None:
    """Verifies that platform events with failure symptoms are correctly recognized as onset."""
    end_time = datetime(2026, 10, 16, 2, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 16, 1, 30, 0, tzinfo=timezone.utc)

    def mock_platform_events(**kwargs: object) -> dict[str, object]:
        return {
            "events": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "message": f"Platform symptom: {kept_text}",
                }
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_platform_events,
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)


@pytest.mark.parametrize(
    "dropped_text",
    [
        "AUTOSCALING",
        "probe succeeded",
        "Starting new instance",
        "Instance started due to configured scaling factors",
        (
            "Starting new instance. Reason: AUTOSCALING - Instance started due to "
            "configured scaling factors..."
        ),
        "Default STARTUP TCP probe succeeded after 1 attempt...",
    ],
)
def test_platform_events_dropped_benign_onset(dropped_text: str) -> None:
    """Verifies that benign operational platform events are ignored and do not
    trigger symptom onset.
    """
    end_time = datetime(2026, 10, 16, 2, 0, 0, tzinfo=timezone.utc)
    event_ts = datetime(2026, 10, 16, 1, 30, 0, tzinfo=timezone.utc)

    # Test via flat events list
    def mock_platform_events(**kwargs: object) -> dict[str, object]:
        return {
            "events": [
                {
                    "timestamp": event_ts.isoformat(),
                    "message": f"Operational log: {dropped_text}",
                }
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_platform_events,
    )

    assert res.is_fallback
    assert res.onset_time is None
    assert res.anchored_start_time == end_time - timedelta(minutes=60)

    # Test via structured system_events category
    def mock_structured_events(**kwargs: object) -> dict[str, object]:
        return {
            "service_name": "order-api",
            "total_events": 1,
            "restarts": {"count": 0, "events": []},
            "probe_failures": {"count": 0, "events": []},
            "system_events": {
                "count": 1,
                "events": [
                    {
                        "timestamp": event_ts.isoformat(),
                        "message": f"Operational log: {dropped_text}",
                    }
                ],
            },
        }

    res_structured = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_structured_events,
    )

    assert res_structured.is_fallback
    assert res_structured.onset_time is None


def test_benign_autoscaling_event_does_not_mask_actual_error_onset() -> None:
    """Verifies that early benign autoscaling events (e.g. at 01:19:42) are ignored and
    do NOT anchor onset detection; onset anchors at actual error logs (e.g. 01:23:16).
    """
    end_time = datetime(2026, 10, 16, 2, 0, 0, tzinfo=timezone.utc)
    autoscaling_ts = datetime(2026, 10, 16, 1, 19, 42, tzinfo=timezone.utc)
    error_ts = datetime(2026, 10, 16, 1, 23, 16, tzinfo=timezone.utc)

    def mock_platform_events(**kwargs: object) -> dict[str, object]:
        return {
            "system_events": {
                "count": 2,
                "events": [
                    {
                        "timestamp": autoscaling_ts.isoformat(),
                        "message": (
                            "Starting new instance. Reason: AUTOSCALING - Instance started "
                            "due to configured scaling factors."
                        ),
                    },
                    {
                        "timestamp": (autoscaling_ts + timedelta(seconds=10)).isoformat(),
                        "message": "Default STARTUP TCP probe succeeded after 1 attempt.",
                    },
                ],
            },
            "restarts": {"count": 0, "events": []},
            "probe_failures": {"count": 0, "events": []},
        }

    def mock_logs(**kwargs: object) -> dict[str, object]:
        return {
            "entries": [
                {
                    "timestamp": error_ts.isoformat(),
                    "severity": "ERROR",
                    "payload_excerpt": (
                        "Database connection pool exhausted: timeout waiting for connection"
                    ),
                }
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=lambda **kw: {"sample_entries": []},
        logs_fn=mock_logs,
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=mock_platform_events,
    )

    assert not res.is_fallback
    # Critical: onset MUST anchor at 01:23:16 (actual error), NOT at 01:19:42 (autoscaling)
    assert res.onset_time == error_ts
    assert res.anchored_start_time == error_ts - timedelta(minutes=10)


def test_503_request_logs_treated_as_symptom_for_onset() -> None:
    """Verifies that HTTP 503 Service Unavailable responses in request logs are
    explicitly treated as symptom events for onset detection.
    """
    end_time = datetime(2026, 10, 16, 0, 0, 0, tzinfo=timezone.utc)
    onset_ts = datetime(2026, 10, 15, 23, 44, 0, tzinfo=timezone.utc)

    def mock_req_logs(**kwargs: object) -> dict[str, object]:
        return {
            "sample_entries": [
                {
                    "timestamp": onset_ts.isoformat(),
                    "status": 503,
                    "latency_ms": 120.0,
                    "excerpt": "GET /api/checkout HTTP 503 Service Unavailable",
                }
            ]
        }

    res = detect_onset_from_telemetry(
        service_name="order-api",
        end_time=end_time,
        window_minutes=60,
        pre_buffer_minutes=10,
        request_logs_fn=mock_req_logs,
        logs_fn=lambda **kw: {"entries": []},
        metrics_fn=lambda **kw: {"points": []},
        platform_events_fn=lambda **kw: {"events": []},
    )

    assert not res.is_fallback
    assert res.onset_time == onset_ts
    assert res.anchored_start_time == onset_ts - timedelta(minutes=10)


@pytest.mark.anyio
async def test_dispatcher_runs_telemetry_onset_even_if_triage_set_onset_time() -> None:
    """Verifies that when triage produces a plan where onset_time is set to ticket opened_at,
    dispatcher.investigate still executes detect_onset_from_telemetry and overrides
    plan.onset_time and plan.start_time with the detected telemetry onset and anchored start time.
    """
    from unittest.mock import patch

    from agent.app.contracts.request import InvestigationRequest
    from agent.app.contracts.result import InvestigationResult
    from agent.app.dispatcher import Dispatcher
    from agent.app.onset import OnsetDetectionResult
    from agent.app.specialists.base import BaseSpecialist

    opened_at = datetime(2026, 10, 15, 0, 40, 44, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-test-onset-override",
        source="servicenow",
        external_id="INC0099",
        external_key="INC0099",
        title="Spike in 500 errors on checkout-service",
        description="Checkout endpoint failing with HTTP 500 errors",
        severity="P2",
        service_hint="checkout-service",
        opened_at=opened_at,
    )
    request = InvestigationRequest(
        incident=incident,
        callback_url="https://example.com/callback",
        requested_at=opened_at + timedelta(minutes=1),
    )

    triage_start = opened_at - timedelta(minutes=15)
    triage_end = opened_at + timedelta(minutes=5)
    triage_plan = InvestigationPlan(
        affected_service="checkout-service",
        symptoms=["HTTP 5xx / application errors"],
        start_time=triage_start,
        end_time=triage_end,
        selected_specialists=["logs"],
        initial_hypotheses=["Investigating checkout-service errors"],
        onset_time=opened_at,  # Erroneously set to ticket opened_at by triage
    )

    mock_triage = MagicMock()
    mock_triage.llm_agent = None
    mock_triage.plan.return_value = triage_plan

    # True telemetry onset is earlier than ticket opened_at
    telemetry_onset = datetime(2026, 10, 15, 0, 35, 53, tzinfo=timezone.utc)
    anchored_start = telemetry_onset - timedelta(minutes=10)
    mock_onset_res = OnsetDetectionResult(
        onset_time=telemetry_onset,
        anchored_start_time=anchored_start,
        end_time=triage_end,
        is_fallback=False,
    )

    specialist_called_with: dict[str, object] = {}

    class MockSpecialist(BaseSpecialist):
        def __init__(self) -> None:
            super().__init__(name="logs", description="Mock logs specialist")

        def run_investigation(
            self,
            incident: Incident,
            start_time: datetime,
            end_time: datetime,
            **kwargs: object,
        ) -> Findings:
            specialist_called_with["start_time"] = start_time
            specialist_called_with["end_time"] = end_time
            specialist_called_with["plan"] = kwargs.get("plan")
            return Findings(
                summary="Logs specialist findings",
                evidence=[],
                anomalies=[],
                queries_run=["query_logs"],
                confidence=0.8,
                errors=[],
            )

    mock_synthesizer = MagicMock()
    mock_synthesizer.llm_agent = None
    mock_synthesizer.synthesize.return_value = Report(
        summary="Synthesized investigation report",
        impact="Checkout degradation",
        probable_root_cause=ProbableRootCause(text="Observed fault", confidence=0.85),
        specialists_status=[],
        metadata={"detected_onset": telemetry_onset.isoformat()},
    )

    mock_review = MagicMock()
    mock_review.evaluate.return_value = None

    config = AgentConfig(timeout_seconds=30)
    dispatcher = Dispatcher(
        config=config,
        specialists={"logs": MockSpecialist()},
        triage_agent=mock_triage,
        review_evaluator=mock_review,
        synthesizer=mock_synthesizer,
        client=MagicMock(),
    )

    with patch(
        "agent.app.dispatcher.detect_onset_from_telemetry",
        return_value=mock_onset_res,
    ) as mock_detect:
        result = await dispatcher.investigate(request)

    # Verify detect_onset_from_telemetry was called despite triage plan having onset_time
    mock_detect.assert_called_once()
    call_kwargs = mock_detect.call_args.kwargs
    assert call_kwargs["service_name"] == "checkout-service"
    assert call_kwargs["end_time"] == triage_end

    # Verify plan fields were updated with telemetry onset and anchored start time
    assert triage_plan.onset_time == telemetry_onset
    assert triage_plan.start_time == anchored_start

    # Verify specialist received the anchored telemetry start time
    assert specialist_called_with["start_time"] == anchored_start

    # Verify investigation completed successfully
    assert isinstance(result, InvestigationResult)
    assert result.status == "completed"
