"""R2: root-cause alternatives never claim a Cloud Run describe or restart count for a component
that is not a Cloud Run service (Cloud SQL instance, Apigee proxy), whose Cloud Run dossier is the
not-applicable one from CloudRunSpecialist."""

import re

import pytest

from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import not_applicable_dossier
from agent.app.specialists.synthesizer import SynthesizerAgent

DEPLOY = "Recent Application Deployment Regression"
CRASH = "Container Crash Loop / Crash Throttling"


def _rca(dossiers, origin, policy, resource):
    alert = AlertEvent(incident_id="0.z2", policy_name=policy, resource_name=resource,
                       state="open", started_at="2026-10-28T01:40:00Z")
    rca = SynthesizerAgent()._build_root_cause_analysis("rc", 0.8, dossiers, origin, alert)
    return {a["alternative"]: a["reason"] for a in rca["alternatives_ruled_out"]}


def _no_cloud_run_claims(alts, name):
    for reason in alts.values():
        assert not re.search(rf"describe of {re.escape(name)}", reason), reason
        assert f"restart count on {name}" not in reason, reason
        assert f"restart count queried on {name}" not in reason, reason


def _s4():
    """S4: Cloud SQL connections critical on ata-inventory-db (24/25)."""
    chg = EvidenceDossier("ChangeSpecialist", "ata-inventory-db", queries_executed={"t": ["q"]})
    sql = EvidenceDossier("CloudSQLSpecialist", "ata-inventory-db",
                          metrics={"active_connections": 24, "max_connections": 25},
                          queries_executed={"cloud_monitoring": ["q"]}, anomaly_detected=True)
    na = not_applicable_dossier("CloudRunSpecialist", "ata-inventory-db", "Cloud SQL")
    return [chg, sql, na]


def _s3():
    """S3: Apigee proxy origin ata-orders-v1 without an attributed proxy change."""
    chg = EvidenceDossier("ChangeSpecialist", "ata-orders-v1", queries_executed={"t": ["q"]})
    na = not_applicable_dossier("CloudRunSpecialist", "ata-orders-v1", "Apigee proxy")
    return [chg, na]


@pytest.mark.parametrize("shape,name,ctype", [
    (_s4, "ata-inventory-db", "Cloud SQL"),
    (_s3, "ata-orders-v1", "Apigee proxy"),
])
def test_not_applicable_origin_has_no_cloud_run_claims(shape, name, ctype):
    alts = _rca(shape(), name, "p", name)
    _no_cloud_run_claims(alts, name)
    deploy = alts[DEPLOY]
    assert f"not a Cloud Run service ({ctype}); no serving revision" in deploy
    assert "Cloud Run service describe" not in deploy
    assert CRASH not in alts  # no container to restart, and no other Cloud Run dossier


def test_crash_loop_uses_a_real_cloud_run_dossier_when_one_exists():
    real = EvidenceDossier("CloudRunSpecialist", "ata-inventory-api",
                           metrics={"container_restarts": 0}, queries_executed={"cloud_monitoring": ["q"]})
    alts = _rca(_s4() + [real], "ata-inventory-db", "ata-cloudsql-connections-critical", "ata-inventory-db")
    _no_cloud_run_claims(alts, "ata-inventory-db")
    assert "restart count queried on ata-inventory-api (0 restarts)" in alts[CRASH]


def test_real_cloud_run_service_text_unchanged():
    chg = EvidenceDossier("ChangeSpecialist", "ata-inventory-api", queries_executed={"t": ["q"]})
    cr = EvidenceDossier("CloudRunSpecialist", "ata-inventory-api", metrics={"error_rate_5xx": "0.0%"},
                         queries_executed={"cloud_monitoring": ["q"]})
    alts = _rca([chg, cr], "ata-inventory-api", "ata-inventory-5xx", "ata-inventory-api")
    assert "the Cloud Run service describe of ata-inventory-api returned no traffic split" in alts[DEPLOY]
    assert "Container restart count on ata-inventory-api: not available" in alts[CRASH]
