"""F7: HTML report text is backed by evidence (longer-term fixes, alternatives ruled out)."""

from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent


def _alert(policy="ata-inventory-5xx", res="ata-inventory-api"):
    return AlertEvent(incident_id="0.f7", policy_name=policy, resource_name=res,
                      state="open", started_at="2026-10-28T01:40:00Z")


def _sql(conns, max_conns=25, anomaly=False):
    return EvidenceDossier("CloudSQLSpecialist", "ata-inventory-db",
                           metrics={"active_connections": conns, "max_connections": max_conns},
                           queries_executed={"cloud_monitoring": ["q"]}, anomaly_detected=anomaly)


def _cr(svc, rate="0.0%", **extra):
    return EvidenceDossier("CloudRunSpecialist", svc, metrics={"error_rate_5xx": rate, **extra},
                           queries_executed={"cloud_monitoring": ["q"]})


def test_s1_shape_db_normal_no_db_or_breaker_fixes():
    synth = SynthesizerAgent()
    d = [_cr("ata-inventory-api", "85.1%"), _cr("ata-orders-api", "0.0%"), _sql(3)]
    assert synth._build_longer_term_fixes("ata-inventory-api", _alert(), d) == []


def test_s4_shape_db_saturated_gives_pool_fixes_with_facts_no_breaker():
    synth = SynthesizerAgent()
    d = [_sql(24, anomaly=True), _cr("ata-inventory-api"), _cr("ata-orders-api")]
    fixes = synth._build_longer_term_fixes("ata-inventory-db", _alert("ata-cloudsql-connections-critical", "ata-inventory-db"), d)
    assert len(fixes) == 2
    assert all("24/25" in f for f in fixes)
    assert not any("circuit breaker" in f.lower() for f in fixes)


def test_circuit_breaker_only_with_measured_cascade():
    synth = SynthesizerAgent()
    d = [_cr("ata-inventory-api", "60.0%"), _cr("ata-orders-api", "40.0%"), _sql(3)]
    fixes = synth._build_longer_term_fixes("ata-inventory-api", _alert(), d)
    assert len(fixes) == 1 and "circuit breaker" in fixes[0].lower()
    assert "ata-orders-api: HTTP 5xx error rate 40.0%" in fixes[0]


def test_no_dossiers_no_fixes():
    assert SynthesizerAgent()._build_longer_term_fixes("x", _alert()) == []


def _alts(dossiers):
    rca = SynthesizerAgent()._build_root_cause_analysis("rc", 0.8, dossiers, "ata-inventory-api", _alert())
    return {a["alternative"]: a["reason"] for a in rca["alternatives_ruled_out"]}


def test_na_never_rules_out():
    apigee = EvidenceDossier("ApigeeSpecialist", "ata-inventory-v1",
                             metrics={"proxy_response_code_429": "N/A (not captured)"},
                             queries_executed={"cloud_monitoring": ["q"]})
    sql = EvidenceDossier("CloudSQLSpecialist", "ata-inventory-db",
                          metrics={"active_connections": None}, queries_executed={"q": ["q"]})
    cr = _cr("ata-inventory-api", container_restarts=None)
    alts = _alts([apigee, sql, cr])
    a = alts["Upstream API Gateway Rate-Limiting"]
    assert "not available" in a and "ruling out" not in a and "not ruled out" in a
    assert "not available" in alts["Container Crash Loop / Crash Throttling"]
    for reason in alts.values():
        assert not ("N/A" in reason and "ruling out" in reason)


def test_measured_429_still_rules_out():
    apigee = EvidenceDossier("ApigeeSpecialist", "ata-inventory-v1",
                             metrics={"proxy_response_code_429": 0}, queries_executed={"q": ["q"]})
    assert "ruling out" in _alts([apigee])["Upstream API Gateway Rate-Limiting"]


def test_deployment_alternative_uses_serving_revision():
    chg = EvidenceDossier("ChangeSpecialist", "ata-inventory-api", queries_executed={"t": ["q"]})
    cr = _cr("ata-inventory-api", serving_revisions={"ata-inventory-api-pin-1": 100})
    reason = _alts([chg, cr])["Recent Application Deployment Regression"]
    assert "serving revision: ata-inventory-api-pin-1 (100% of traffic)" in reason
    assert "N/A" not in reason


def test_deployment_alternative_states_why_revision_unknown():
    chg = EvidenceDossier("ChangeSpecialist", "ata-inventory-api", queries_executed={"t": ["q"]})
    reason = _alts([chg, _cr("ata-inventory-api")])["Recent Application Deployment Regression"]
    assert "serving revision not known" in reason and "N/A" not in reason
    reason = _alts([chg])["Recent Application Deployment Regression"]
    assert "no Cloud Run read of ata-inventory-api" in reason


def test_pool_fixes_at_exact_near_saturation_ratio():
    """N1: 20/25 is exactly DB_POOL_NEAR_SATURATION_RATIO (0.8): the pool fixes apply (>=)."""
    synth = SynthesizerAgent()
    d = [_sql(20, anomaly=True), _cr("ata-inventory-api")]
    fixes = synth._build_longer_term_fixes("ata-inventory-db", _alert("ata-cloudsql-connections-critical", "ata-inventory-db"), d)
    assert len(fixes) == 2 and all("20/25" in f for f in fixes)
    d = [_sql(19, anomaly=True), _cr("ata-inventory-api")]
    assert synth._build_longer_term_fixes("ata-inventory-db", _alert("ata-cloudsql-connections-critical", "ata-inventory-db"), d) == []
