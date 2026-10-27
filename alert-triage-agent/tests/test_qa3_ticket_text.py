"""Regression tests for QA round 3 ticket-text accuracy (qa-results-r3.md S1-S5, r2 R2-9/R2-11).

Shapes and values come from the live round-3 tickets (cloud-assist-fde-2, 2026-10-27):
- S1 INC0010334: Problem line quoted ata-orders-api's 84.5% as ata-inventory-api's rate (86.1%)
  and called the callers "upstream consumers".
- S1/S2/S3/S4: the description ended with a boilerplate "Rollback: Revert latest ..." line.
- S2 INC0010335 / S3 INC0010338: "Apigee proxy deployment history not queried" next to a
  Change-Induced line that lists the Apigee access log as checked; "Issue Type: runtime_anomaly"
  next to "Change-Induced: Yes".
- S5 (r2 INC0010333): root cause named the first alert's policy (ata-inventory-latency) for 5xx
  evidence; action 3's query (password|authentication|denied) matched 0 of the real lines;
  the OKF frontmatter said P2 while the ticket said P1.
- S4 INC0010339: only the database was investigated, so its dependents' 5xx was not measured.
"""

from pathlib import Path
import re

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import EvidenceItem, InvestigationReport
from agent.app.grouping.models import OKFDocument
from agent.app.grouping.store import OKFStore
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.change import NO_CLOUD_RUN_DEPLOYMENTS_FINDING
from agent.app.specialists.cloud_run import db_auth_search_term, has_db_auth_signature
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.specialists.telemetry_reads import CHANGE_SOURCES_KEY
from bridge.app.contracts.report import InvestigationReport as BridgeInvestigationReport

PROJECT = "cloud-assist-fde-2"
REGION = "us-west1"
INV = "ata-inventory-api"
ORD = "ata-orders-api"
DB = "ata-inventory-db"
INV_PROXY = "ata-inventory-v1"
ORD_PROXY = "ata-orders-v1"
ACCESS_LOG_SOURCE = f"Apigee access log ata-apigee-access (proxy revisions of {INV_PROXY})"
# The real S5 line (587x on ata-inventory-api-s5-1793115239, 15:34-15:41Z).
S5_LOG_LINE = "Failed to connect to Cloud SQL: 'NoneType' object has no attribute 'decode'"
S5_REV = "ata-inventory-api-s5-1793115239"


def _cfg(tmp_path) -> AgentConfig:
    return AgentConfig(project_id=PROJECT, region=REGION, is_local=True,
                       incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path))


def _entry(inc, policy, component, signal, severity, started="2026-10-27T14:57:32Z"):
    return {"incident_id": inc, "policy_name": policy, "resource_name": component, "component": component,
            "signal": signal, "labels": {"severity": severity, "signal": signal}, "started_at": started}


def _cr(target, rate, rpm=None, anomaly=True, **extra):
    metrics = {"error_rate_5xx": rate}
    if rpm is not None:
        metrics["request_count_rpm"] = rpm
    metrics.update(extra)
    return EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource=target,
                           metrics=metrics, anomaly_detected=anomaly)


def _no_deploy_change_dossier(target):
    return EvidenceDossier(
        specialist_name="ChangeSpecialist", target_resource=target,
        findings=[NO_CLOUD_RUN_DEPLOYMENTS_FINDING],
        metrics={CHANGE_SOURCES_KEY: ["Cloud Run Admin API revision history", "Cloud Run audit log"]},
        evidence_items=[EvidenceItem(
            headline="Deployment History Analysis",
            content=("No recent Cloud Run deployments detected. Apigee proxy deployment history was not "
                     "queried; proxy revision changes come from the Apigee access log."),
        )],
    )


def _apigee_dossier(proxy=INV_PROXY, switch=None):
    metrics = {CHANGE_SOURCES_KEY: [f"Apigee access log ata-apigee-access (proxy revisions of {proxy})"]}
    if switch:
        metrics["proxy_revision_change"] = switch
    return EvidenceDossier(specialist_name="ApigeeSpecialist", target_resource=proxy, metrics=metrics)


def _wire(synth, cloud_run, apigee, sql, change, calls=None):
    async def _ret(d):
        return d

    def _cloud_run(alert, config, target_service=None):
        if calls is not None:
            calls.append(target_service)
        return _ret(cloud_run(target_service or alert.resource_name))

    synth.cloud_run_specialist.investigate = _cloud_run
    synth.apigee_specialist.investigate = lambda alert, config: _ret(apigee)
    synth.cloud_sql_specialist.investigate = lambda alert, config: _ret(sql)
    synth.change_specialist.investigate = lambda alert, config: _ret(change)


# ---------------------------------------------------------------------------------------
# 1. S1: each 5xx rate is attributed to its own service
# ---------------------------------------------------------------------------------------

S1_ALERT = AlertEvent(incident_id="0.oebegz5hdxwe", policy_name="ata-inventory-5xx", resource_name=INV,
                      started_at="2026-10-27T14:57:32Z", policy_user_labels={"signal": "5xx", "severity": "p1"})
S1_BLAST = [INV, INV_PROXY, ORD, ORD_PROXY]


def test_s1_problem_states_the_origin_rate_not_the_last_dossier_read():
    # Dossier order as in the live run: origin first, the dependency (orders) last.
    dossiers = [_cr(INV, "86.1%", 38), _cr(ORD, "84.5%", 21)]
    summary = SynthesizerAgent()._synthesize_problem(S1_ALERT, dossiers, INV, S1_BLAST)
    assert summary.startswith(f"Elevated HTTP 5xx error rate (86.1%) on {INV} impacting dependent services")
    assert summary.endswith(f"; HTTP 5xx rate on dependent services: {ORD} 84.5%")
    assert f"(84.5%) on {INV}" not in summary
    assert "upstream consumers" not in summary


def test_s1_problem_without_an_origin_rate_says_so_and_borrows_none():
    dossiers = [_cr(INV, "N/A (not captured)"), _cr(ORD, "84.5%", 21)]
    summary = SynthesizerAgent()._synthesize_problem(S1_ALERT, dossiers, INV, S1_BLAST)
    assert f"Elevated HTTP 5xx error rate on {INV} (HTTP 5xx rate not measured on {INV})" in summary
    assert "(84.5%)" not in summary
    assert f"{ORD} 84.5%" in summary
    assert "(elevated)" not in summary


@pytest.mark.asyncio
async def test_s1_ticket_end_to_end(tmp_path):
    """S1 shape through execute_investigation and the ServiceNow description."""
    synth = SynthesizerAgent()
    by_target = {INV: _cr(INV, "86.1%", 38), ORD: _cr(ORD, "84.5%", 21)}
    sql = EvidenceDossier(specialist_name="CloudSQLSpecialist", target_resource=DB,
                          metrics={"active_connections": 3, "max_connections": 25})
    _wire(synth, lambda t: by_target.get(t) or _cr(t, "N/A (not captured)", anomaly=False),
          _apigee_dossier(), sql, _no_deploy_change_dossier(INV))
    group = [_entry("0.oebegz5hdxwk", "ata-orders-5xx", ORD, "5xx", "p1"),
             _entry("0.oebegz5hdxwe", "ata-inventory-5xx", INV, "5xx", "p1")]

    report = await synth.execute_investigation(S1_ALERT, "g-s1", "incident-g-s1.md", _cfg(tmp_path), group_alerts=group)
    desc = report.build_structured_description(project_id=PROJECT, incidents_bucket="test-project-ata-incidents")

    assert report.origin == INV
    assert f"(86.1%) on {INV}" in report.summary and f"{ORD} 84.5%" in report.summary
    # Item 2: no generic rollback line; nothing in the actions names a rollback.
    assert "Revert latest" not in desc
    assert "Rollback:" not in desc
    # Item 3: Change-Induced and the deployment statement come from the same sources.
    assert report.change_induced.startswith("No (") and ACCESS_LOG_SOURCE in report.change_induced
    assert "Issue Type: runtime_anomaly" in desc
    assert "deployment history not queried" not in desc
    assert "deployment history not queried" not in str(report.evidence_by_area)
    assert f"no Apigee proxy revision switch in {ACCESS_LOG_SOURCE}" in str(report.evidence_by_area)


# ---------------------------------------------------------------------------------------
# 2. Rollback lines only from actions that name one
# ---------------------------------------------------------------------------------------

def _report(cls=InvestigationReport, **kw):
    base = {"group_id": "g", "summary": "Elevated HTTP 5xx error rate (86.1%) on ata-inventory-api",
                "observations": "o", "probable_root_cause": {"text": "x", "confidence": 0.8},
                "origin": INV, "blast_radius": S1_BLAST, "priority": "P1"}
    base.update(kw)
    return cls(**base)


@pytest.mark.parametrize("cls", [InvestigationReport, BridgeInvestigationReport])
def test_description_has_no_boilerplate_rollback(cls):
    actions = [{"title": f"Describe traffic on {INV} (read-only)", "command": f"gcloud run services describe {INV}"}]
    desc = _report(cls, recommended_actions=actions).build_structured_description(project_id=PROJECT, incidents_bucket="test-project-ata-incidents")
    assert "Rollback:" not in desc
    assert "Revert latest" not in desc


@pytest.mark.parametrize("cls", [InvestigationReport, BridgeInvestigationReport])
def test_description_keeps_an_evidenced_rollback(cls):
    actions = [{"title": "Roll back", "command": "gcloud run services update-traffic ...",
                "rollback": "route back explicitly with --to-revisions=X=100"}]
    desc = _report(cls, recommended_actions=actions).build_structured_description(project_id=PROJECT, incidents_bucket="test-project-ata-incidents")
    assert "  - Rollback: route back explicitly with --to-revisions=X=100" in desc


def test_fallback_report_has_no_generic_rollback(tmp_path):
    from agent.app.triage import generate_investigation_report

    report = generate_investigation_report(S1_ALERT, "g", "incident-g.md", _cfg(tmp_path))
    assert not any("Revert latest" in (a.rollback or "") for a in report.get_normalized_actions())
    assert "Revert latest" not in report.build_structured_description(project_id=PROJECT, incidents_bucket="test-project-ata-incidents")


# ---------------------------------------------------------------------------------------
# 3. Issue Type, Change-Induced and the deployment-history statement agree
# ---------------------------------------------------------------------------------------

S3_YES = ("Yes (Apigee proxy ata-orders-v1 revision 177 first served 2026-10-27T15:55:02Z, replacing revision 176; "
          "access log: revision 177 served 566 requests with 553 SpikeArrest faults, revision 176 served 76 requests with none)")


@pytest.mark.parametrize("cls", [InvestigationReport, BridgeInvestigationReport])
def test_s3_issue_type_follows_change_induced_yes(cls):
    report = _report(cls, origin=ORD_PROXY, summary="SpikeArrest rate-limit violation (HTTP 429 rate at 74%) on ata-orders-v1",
                     change_induced=S3_YES)
    desc = report.build_structured_description(project_id=PROJECT, incidents_bucket="test-project-ata-incidents")
    assert "Issue Type: change_induced" in desc
    assert "Change-Induced: Yes (" in desc


@pytest.mark.parametrize("cls", [InvestigationReport, BridgeInvestigationReport])
def test_issue_type_is_not_change_induced_without_an_implicated_change(cls):
    # The summary mentions a revision, but Change-Induced says no change is implicated.
    report = _report(cls, summary="Deployment regression on revision x for ata-inventory-api",
                     change_induced="No (recent revision x on ata-inventory-api not implicated; checked: ...)")
    assert report.infer_issue_type() != "change_induced"
    report = _report(cls, summary="Deployment regression on revision x", change_induced=None)
    assert report.infer_issue_type() != "change_induced"


def test_s2_deployment_statement_names_the_access_log_that_was_read():
    dossiers = [_no_deploy_change_dossier(INV), _apigee_dossier()]
    out = SynthesizerAgent()._reconcile_change_history(dossiers)
    chg = out[0]
    assert chg.findings == [
        ("No Cloud Run revision deployments detected in the 2-hour window prior to alert onset; "
        f"no Apigee proxy revision switch in {ACCESS_LOG_SOURCE}.")
    ]
    assert chg.evidence_items[0].content == chg.findings[0]
    assert out[1] is dossiers[1]


def test_s3_deployment_statement_names_the_proxy_revision_switch():
    switch = {"previous_revision": "176", "new_revision": "177", "new_first_served_at": "2026-10-27T15:55:02Z"}
    out = SynthesizerAgent()._reconcile_change_history(
        [_no_deploy_change_dossier(ORD_PROXY), _apigee_dossier(ORD_PROXY, switch)]
    )
    text = out[0].findings[0]
    assert "not queried" not in text
    assert ("Apigee proxy ata-orders-v1 switched from revision 176 to 177 "
            "(first served 2026-10-27T15:55:02Z, Apigee access log)") in text


def test_deployment_statement_unchanged_when_the_access_log_was_not_read():
    no_log = EvidenceDossier(specialist_name="ApigeeSpecialist", target_resource=INV_PROXY, metrics={})
    dossiers = [_no_deploy_change_dossier(INV), no_log]
    assert SynthesizerAgent()._reconcile_change_history(dossiers) is dossiers
    # The local "nothing queried" finding is never turned into "no deployments detected".
    local = EvidenceDossier(specialist_name="ChangeSpecialist", target_resource=INV, findings=[
        ("Change history not queried: no change source was available (...; Apigee proxy deployment history "
        "not queried); change attribution not assessed.")])
    dossiers = [local, _apigee_dossier()]
    assert SynthesizerAgent()._reconcile_change_history(dossiers) is dossiers


# ---------------------------------------------------------------------------------------
# 4. S5: the policy that matches the evidence; a log query that finds the evidence
# ---------------------------------------------------------------------------------------

S5_LATENCY_ALERT = AlertEvent(incident_id="0.oebdcf2m1bud", policy_name="ata-inventory-latency", resource_name=INV,
                              started_at="2026-10-27T14:08:12Z", policy_user_labels={"signal": "latency", "severity": "p2"})
S5_GROUP = [
    _entry("0.oebdcf2m1bud", "ata-inventory-latency", INV, "latency", "p2", "2026-10-27T14:08:12Z"),
    _entry("0.oebdcf2m1bu3", "ata-inventory-5xx", INV, "5xx", "p1", "2026-10-27T14:08:12Z"),
    _entry("0.oebdcf2m1bu8", "ata-orders-5xx", ORD, "5xx", "p1", "2026-10-27T14:08:12Z"),
]


def _s5_dossiers(log_term=True):
    chg = EvidenceDossier(specialist_name="ChangeSpecialist", target_resource=INV, anomaly_detected=True, metrics={
        "recent_revision": S5_REV, "change_service": INV, "minutes_since_deploy": 7,
        "deploy_timestamp": "2026-10-27T15:34:01.518558Z",
        "all_changes": [{"service": INV, "revision": S5_REV, "deployed_minutes_ago": 7,
                         "timestamp": "2026-10-27T15:34:01.518558Z"}],
    })
    metrics = {"has_db_auth_error": True}
    if log_term:
        metrics["db_auth_log_term"] = db_auth_search_term(S5_LOG_LINE)
    return [chg, _cr(INV, "99.2%", 80, **metrics)]


def test_s5_root_cause_names_the_5xx_policy_for_5xx_evidence():
    text, _ = SynthesizerAgent()._determine_root_cause(
        _s5_dossiers(), INV, S5_LATENCY_ALERT, blast_radius=S1_BLAST, group_alerts=S5_GROUP
    )
    assert "database authentication failures in application logs (HTTP 5xx error rate: 99.2%)" in text
    assert text.endswith("under policy `ata-inventory-5xx`.")
    assert "ata-inventory-latency" not in text


def test_s5_root_cause_keeps_the_investigated_policy_without_a_matching_group_alert():
    text, _ = SynthesizerAgent()._determine_root_cause(
        _s5_dossiers(), INV, S5_LATENCY_ALERT, blast_radius=S1_BLAST, group_alerts=[S5_GROUP[0], S5_GROUP[2]]
    )
    # ata-orders-5xx is not on the origin: it is not named for inventory's evidence.
    assert text.endswith("under policy `ata-inventory-latency`.")


def test_s5_log_query_finds_the_real_evidence_line():
    assert has_db_auth_signature(S5_LOG_LINE)
    term = db_auth_search_term(S5_LOG_LINE)
    assert term == "failed to connect to cloud sql:"
    # Cloud Logging global restriction: case-insensitive substring of a field.
    assert term in S5_LOG_LINE.lower()
    # The old query's regex (case-sensitive RE2) matched none of the real lines.
    assert not re.search(r"password|authentication|denied", S5_LOG_LINE)

    actions = SynthesizerAgent()._synthesize_recommendations(
        _s5_dossiers(), INV, S5_LATENCY_ALERT, region=REGION, project_id=PROJECT
    )
    cmd = next(a.command for a in actions if (a.command or "").startswith("gcloud logging read"))
    assert cmd == (
        "gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"ata-inventory-api\""
        " AND \"failed to connect to cloud sql:\"' --limit=30"
    )
    assert "password|authentication|denied" not in " ".join(a.command or "" for a in actions)


def test_s5_no_log_query_without_a_term_from_the_matched_line():
    actions = SynthesizerAgent()._synthesize_recommendations(
        _s5_dossiers(log_term=False), INV, S5_LATENCY_ALERT, region=REGION, project_id=PROJECT
    )
    assert not any((a.command or "").startswith("gcloud logging read") for a in actions)
    assert any("update-traffic" in (a.command or "") or "revisions" in (a.command or "") for a in actions)


@pytest.mark.parametrize("line, term", [
    ('FATAL: password authentication failed for user "ata"', "password authentication failed"),
    ('FATAL: role "bad-user" does not exist', 'role "bad-user" does not exist'),
    ("GET /items 200", None),
])
def test_db_auth_search_term_is_text_of_the_line(line, term):
    assert db_auth_search_term(line) == term
    if term:
        assert term in line.lower() and "'" not in term


def test_double_quotes_in_the_term_are_escaped_for_the_logging_query():
    dossiers = [_cr(INV, "99.2%", has_db_auth_error=True, db_auth_log_term='role "bad-user" does not exist')]
    action = SynthesizerAgent._db_auth_log_action(dossiers, INV)
    assert 'AND "role \\"bad-user\\" does not exist"\' --limit=30' in action.command


@pytest.mark.asyncio
async def test_cloud_run_specialist_records_the_term_from_the_s5_line():
    from tests.test_db_auth_detection_and_rca_config import _investigate_with_logs

    dossier = await _investigate_with_logs([S5_LOG_LINE], INV, "ata-inventory-5xx")
    assert dossier.metrics["has_db_auth_error"] is True
    assert dossier.metrics["db_auth_log_term"] == "failed to connect to cloud sql:"


# ---------------------------------------------------------------------------------------
# 5. OKF frontmatter priority follows the ticket priority
# ---------------------------------------------------------------------------------------

def test_okf_priority_is_written_after_the_group_raises_it(tmp_path):
    cfg = _cfg(tmp_path)
    store = OKFStore(config=cfg)
    group_id = "ata-inventory-latency-ata-inventory-api-20261027t140825z"
    store.write(group_id, OKFDocument(group_id=group_id, priority="P2", policy_name="ata-inventory-latency",
                                      resource_name=INV, origin=INV, alerts=[dict(e) for e in S5_GROUP]))
    report = _report(group_id=group_id, filename=f"incident-{group_id}.md", priority="P2",
                     alerts=[dict(S5_GROUP[0])])

    SynthesizerAgent()._update_okf_file(report, S5_LATENCY_ALERT, cfg, store=store)

    assert report.priority == "P1"
    assert store.read(group_id).priority == "P1"


def test_ticket_record_syncs_the_file_priority():
    from agent.app.main import _record_ticket_title

    doc = OKFDocument(group_id="g", priority="P2")
    _record_ticket_title(doc, _report(priority="P1"))
    assert doc.ticket_priority == "P1"
    assert doc.priority == "P1"


# ---------------------------------------------------------------------------------------
# 6. S4: a database origin's dependents are measured
# ---------------------------------------------------------------------------------------

S4_ALERT = AlertEvent(incident_id="0.oebgfkhaglsk", policy_name="ata-cloudsql-connections-critical", resource_name=DB,
                      started_at="2026-10-27T16:23:24Z", policy_user_labels={"signal": "connections", "severity": "p1"})


def _s4_sql():
    return EvidenceDossier(specialist_name="CloudSQLSpecialist", target_resource=DB, anomaly_detected=True,
                           metrics={"active_connections": 24, "max_connections": 25})


@pytest.mark.asyncio
@pytest.mark.parametrize("rates, expected", [
    # Live S4: 100% HTTP 200 on both services.
    ({INV: "0.0%", ORD: "0.0%"},
     (f"(connections at 24/25) on {DB} since 2026-10-27T16:23:24Z (no HTTP 5xx errors measured on {INV}, {ORD}; "
     f"HTTP 5xx rate not measured on {INV_PROXY}, {ORD_PROXY})")),
    ({INV: "5.3%", ORD: "0.0%"},
     f"since 2026-10-27T16:23:24Z; HTTP 5xx rate on dependent services: {INV} 5.3%"),
])
async def test_s4_report_states_the_measured_dependent_impact(tmp_path, rates, expected):
    synth = SynthesizerAgent()
    calls: list = []
    _wire(synth, lambda t: _cr(t, rates.get(t, "N/A (not captured)"), 12, anomaly=False),
          EvidenceDossier(specialist_name="ApigeeSpecialist", target_resource=INV_PROXY), _s4_sql(),
          EvidenceDossier(specialist_name="ChangeSpecialist", target_resource=DB), calls=calls)

    report = await synth.execute_investigation(S4_ALERT, "g-s4", "incident-g-s4.md", _cfg(tmp_path))

    assert report.origin == DB
    assert sorted(c for c in calls if c) == [INV, ORD]
    assert report.summary.startswith("Database connection pool saturation (connections at 24/25)")
    assert expected in report.summary


@pytest.mark.asyncio
async def test_non_database_origin_does_not_investigate_extra_dependents(tmp_path):
    synth = SynthesizerAgent()
    calls: list = []
    _wire(synth, lambda t: _cr(t, "86.1%" if t == INV else "N/A (not captured)", 38, anomaly=t == INV),
          _apigee_dossier(), EvidenceDossier(specialist_name="CloudSQLSpecialist", target_resource=DB),
          _no_deploy_change_dossier(INV), calls=calls)
    await synth.execute_investigation(S1_ALERT, "g", "incident-g.md", _cfg(tmp_path))
    assert ORD not in calls


def test_ticket_record_never_lowers_the_file_priority(caplog):
    from agent.app.main import _record_ticket_title

    doc = OKFDocument(group_id="g", priority="P1")
    _record_ticket_title(doc, _report(priority="P2"))
    assert doc.priority == "P1"
    assert "not lowered automatically" in caplog.text


# ---------------------------------------------------------------------------------------
# O2 / N1 (fresh-rev-s12): both file-priority write paths are raise-only, via one helper
# ---------------------------------------------------------------------------------------

def test_okf_update_never_lowers_the_file_priority(tmp_path, caplog):
    # The group was created at P1 (policy-name heuristic); every alert is labelled p2, so the
    # report says P2. The file keeps P1 and the lowering is logged, as on the ticket-record path.
    cfg = _cfg(tmp_path)
    store = OKFStore(config=cfg)
    group_id = "ata-inventory-5xx-ata-inventory-api-20261027t140825z"
    p2_entry = _entry("0.oebdcf2m1bu3", "ata-inventory-5xx", INV, "5xx", "p2", "2026-10-27T14:08:12Z")
    store.write(group_id, OKFDocument(group_id=group_id, priority="P1", policy_name="ata-inventory-5xx",
                                      resource_name=INV, origin=INV, alerts=[dict(p2_entry)]))
    report = _report(group_id=group_id, filename=f"incident-{group_id}.md", priority="P2", alerts=[dict(p2_entry)])
    alert = AlertEvent(incident_id="0.oebdcf2m1bu3", policy_name="ata-inventory-5xx", resource_name=INV,
                       started_at="2026-10-27T14:08:12Z", policy_user_labels={"signal": "5xx", "severity": "p2"})

    with caplog.at_level("WARNING"):
        SynthesizerAgent()._update_okf_file(report, alert, cfg, store=store)

    assert store.read(group_id).priority == "P1"
    assert report.priority == "P2"
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "not lowered automatically" in r.getMessage()]
    assert len(warnings) == 1
    assert "P1" in warnings[0].getMessage() and "P2" in warnings[0].getMessage()


def test_both_priority_write_paths_use_the_shared_raise_only_helper(tmp_path, monkeypatch):
    from agent.app import main as agent_main
    from agent.app.specialists import synthesizer as synth_mod

    calls: list[tuple[str, str]] = []
    real = synth_mod.raise_file_priority

    def _spy(doc, priority, source):
        calls.append((priority, source))
        return real(doc, priority, source)

    monkeypatch.setattr(synth_mod, "raise_file_priority", _spy)
    monkeypatch.setattr(agent_main, "raise_file_priority", _spy)

    agent_main._record_ticket_title(OKFDocument(group_id="g", priority="P2"), _report(priority="P1"))
    cfg = _cfg(tmp_path)
    store = OKFStore(config=cfg)
    store.write("g2", OKFDocument(group_id="g2", priority="P2", policy_name="ata-inventory-latency",
                                  resource_name=INV, origin=INV, alerts=[dict(S5_GROUP[0])]))
    SynthesizerAgent()._update_okf_file(
        _report(group_id="g2", filename="incident-g2.md", priority="P2", alerts=[dict(S5_GROUP[0])]),
        S5_LATENCY_ALERT, cfg, store=store,
    )
    assert [p for p, _ in calls] == ["P1", "P2"]


@pytest.mark.parametrize("current, new, expected", [("P2", "P1", "P1"), ("P1", "P2", "P1"), ("P2", "P2", "P2"),
                                                    (None, "P3", "P3"), ("P1", None, "P1")])
def test_raise_file_priority_only_raises(current, new, expected):
    from agent.app.specialists.synthesizer import raise_file_priority

    doc = OKFDocument(group_id="g", priority=current)
    raise_file_priority(doc, new, "test")
    assert doc.priority == expected


def test_priority_rank_is_public_and_main_does_not_import_a_private_helper():
    from agent.app.specialists.synthesizer import priority_rank

    assert priority_rank("P1") < priority_rank("p2") < priority_rank(None)
    main_src = (Path(__file__).resolve().parents[1] / "agent" / "app" / "main.py").read_text()
    assert "_priority_rank" not in main_src
