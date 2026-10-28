"""F8: HTML rationale consistency for non-Cloud-Run origins and Apigee proxy changes.

S3 INC0010347 shape: 429 alert on proxy ata-orders-v1, change_induced from Apigee r184.
S4 shape: connections alert on Cloud SQL ata-inventory-db.
"""

from unittest.mock import patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import NOT_APPLICABLE_KEY, CloudRunSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent


@pytest.fixture
def cfg():
    return AgentConfig(project_id="p", name_prefix="ata", region="us-west1", is_local=True)


def _s3_alert():
    return AlertEvent(incident_id="0.s3", policy_name="ata-apigee-orders-429", resource_name="ata-orders-v1",
                      state="open", started_at="2026-10-28T01:41:30Z")


def _s4_alert():
    return AlertEvent(incident_id="0.s4", policy_name="ata-cloudsql-connections-critical",
                      resource_name="ata-inventory-db", state="open", started_at="2026-10-28T02:17:43Z")


def _s3_apigee():
    return EvidenceDossier(
        "ApigeeSpecialist", "ata-orders-v1", anomaly_detected=True,
        metrics={
            "proxy_response_code_429": 633,
            "proxy_revision_change": {
                "previous_revision": "183", "new_revision": "184",
                "new_first_served_at": "2026-10-28T01:34:46Z",
                "previous_requests": 13, "new_requests": 647,
                "previous_spike_arrest_faults": 0, "new_spike_arrest_faults": 633,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_fn,kind", [(_s3_alert, "Apigee proxy"), (_s4_alert, "Cloud SQL")])
async def test_cloud_run_not_queried_for_non_cloud_run(cfg, alert_fn, kind):
    alert = alert_fn()
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon, \
         patch("google.cloud.logging.Client") as log:
        d = await CloudRunSpecialist().investigate(alert, cfg)
        mon.assert_not_called()
        log.assert_not_called()
    assert d.queries_executed == {}
    assert not d.anomaly_detected
    assert "error_rate_5xx" not in d.metrics
    text = d.metrics[NOT_APPLICABLE_KEY]
    assert "not applicable" in text and alert.resource_name in text and kind in text
    assert "baseline operation" not in d.evidence_items[0].content


@pytest.mark.asyncio
async def test_cloud_run_still_queried_for_cloud_run_service(cfg):
    alert = AlertEvent(incident_id="0.s1", policy_name="ata-inventory-5xx", resource_name="ata-inventory-api",
                       state="open", started_at="2026-10-28T01:40:00Z")
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon, \
         patch("google.cloud.logging.Client") as log:
        mon.return_value.list_time_series.return_value = []
        log.return_value.list_entries.return_value = []
        d = await CloudRunSpecialist().investigate(alert, cfg)
        assert mon.called
    assert NOT_APPLICABLE_KEY not in d.metrics


def test_s3_proximity_names_the_apigee_proxy_change():
    synth = SynthesizerAgent()
    na_cr = EvidenceDossier("CloudRunSpecialist", "ata-orders-v1", metrics={NOT_APPLICABLE_KEY: "n/a"})
    _, factors, rationale = synth.calculate_confidence(
        [_s3_apigee(), na_cr], "ata-orders-v1", _s3_alert(), ["ata-orders-v1"], None,
    )
    detail = factors["change_proximity"]["detail"]
    assert "Zero recent deployments" not in detail and "Zero recent deployments" not in rationale
    assert "ata-orders-v1" in detail and "revision 184" in detail and "replacing 183" in detail
    assert "6m prior to alert onset" in detail
    assert factors["change_proximity"]["score"] == 0.15


def test_na_rate_is_not_a_corroborating_signal():
    synth = SynthesizerAgent()
    cr = EvidenceDossier("CloudRunSpecialist", "ata-orders-v1", metrics={"error_rate_5xx": "N/A (not captured)"})
    zero = EvidenceDossier("CloudRunSpecialist", "ata-orders-api", metrics={"error_rate_5xx": "0.0%"})
    real = EvidenceDossier("CloudRunSpecialist", "ata-inventory-api", metrics={"error_rate_5xx": "85.1%"})
    _, factors, _ = synth.calculate_confidence([cr, zero, real], "ata-inventory-api", _s3_alert(), [], None)
    detail = factors["corroborating_signals"]["detail"]
    assert "N/A" not in detail and "0.0%" not in detail
    assert "HTTP 5xx rate 85.1% on ata-inventory-api" in detail
    assert detail.startswith("1 corroborating")


def test_s4_proximity_and_signals_without_cloud_run():
    synth = SynthesizerAgent()
    sql = EvidenceDossier("CloudSQLSpecialist", "ata-inventory-db", anomaly_detected=True,
                          metrics={"active_connections": 24, "max_connections": 25})
    na_cr = EvidenceDossier("CloudRunSpecialist", "ata-inventory-db", metrics={NOT_APPLICABLE_KEY: "n/a"})
    _, factors, _ = synth.calculate_confidence([sql, na_cr], "ata-inventory-db", _s4_alert(), [], None)
    detail = factors["corroborating_signals"]["detail"]
    assert detail.startswith("1 corroborating") and "24/25" in detail
    assert factors["change_proximity"]["detail"] == "Zero recent deployments detected within lookback window"


def test_proxy_revision_first_served_after_onset_scores_zero():
    """N2: a proxy revision first served after the alert onset is not 'prior to' it and earns no proximity score."""
    synth = SynthesizerAgent()
    apigee = _s3_apigee()
    apigee.metrics["proxy_revision_change"]["new_first_served_at"] = "2026-10-28T01:45:30Z"  # onset 01:41:30
    na_cr = EvidenceDossier("CloudRunSpecialist", "ata-orders-v1", metrics={NOT_APPLICABLE_KEY: "n/a"})
    _, factors, _ = synth.calculate_confidence([apigee, na_cr], "ata-orders-v1", _s3_alert(), ["ata-orders-v1"], None)
    detail = factors["change_proximity"]["detail"]
    assert "4m after the alert onset" in detail
    assert "prior to alert onset" not in detail and "proximity)" not in detail
    assert factors["change_proximity"]["score"] == 0.0


@pytest.mark.parametrize("mins,score", [(0, 0.15), (15, 0.15), (16, 0.10), (30, 0.10), (31, 0.05),
                                        (60, 0.05), (61, 0.0)])
def test_change_proximity_bucket_boundaries(mins, score):
    """N6: one bucket table for the Cloud Run and proxy branches; boundaries are inclusive."""
    from agent.app.specialists.synthesizer import _proximity_score
    assert _proximity_score(mins)[0] == score
