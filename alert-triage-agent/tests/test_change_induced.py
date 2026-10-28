"""Change-Induced is set from change detection, never from the contract default.

Before: nothing in the agent set ``change_induced``, so every ticket printed the contract
default "Change-Induced: No", including S3 (proxy ata-orders-v1 revision 169 brought the
SpikeArrest faults) and S5 (bad inventory deploy). Now:

- "Yes (<evidence>)" when a change is implicated: the change verdict (Cloud Run revision) or
  an Apigee proxy revision that brought the SpikeArrest faults.
- "No (<what was checked>)" only when change detection read at least one change source and
  found no implicated change in the window.
- "Not assessed (<why>)" when a change source failed, none was queried, or the origin proxy's
  revisions were not evaluated. The contract default is "Not assessed" too (agent and bridge).
"""

import asyncio
from unittest.mock import patch

import pytest

from agent.app.tools.tools_client import ToolsNotConfigured
from agent.app.contracts.report import InvestigationReport
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.change import ChangeSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.specialists.telemetry_reads import CHANGE_SOURCES_KEY
from bridge.app.contracts.report import InvestigationReport as BridgeReport

from tests.test_group_origin_selection import (  # noqa: F401  (fixtures)
    INVENTORY_BAD_REV,
    INVENTORY_GOOD_REV,
    ORDERS_REDEPLOY_RECENT,
    ORDERS_REV,
    S2_ORDERS,
    _change,
    _entry,
    _event,
    agent_cfg,
    telemetry,
)
from tests.test_s3_evidence_quality import FakeGCP, _orders_alert, _orders_report, cfg  # noqa: F401

TIMELINE = (
    "ata-tools change timeline (Cloud Run revision history), "
    "ata-tools change timeline (Apigee deployment history)"
)


def _investigate(alert, agent_cfg, group_alerts=None):
    return asyncio.run(
        SynthesizerAgent().execute_investigation(alert, "grp-ci", "incident-grp-ci.md", agent_cfg, group_alerts=group_alerts)
    )


def _desc(report) -> str:
    return report.build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="test-project-ata-incidents")


# ---------------------------------------------------------------------------------------
# Yes
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_proxy_revision_169_is_change_induced_yes(cfg):
    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=ToolsNotConfigured("TOOLS_URL is not set")):
        report = await _orders_report(cfg, FakeGCP())

    assert report.change_induced == (
        "Yes (Apigee proxy ata-orders-v1 revision 169 first served 2026-10-27T12:52:24Z, replacing revision 168; "
        "access log: revision 169 served 619 requests with 595 SpikeArrest faults, revision 168 served 32 "
        "requests with none)"
    )
    desc = _desc(report)
    assert "Change-Induced: Yes (Apigee proxy ata-orders-v1 revision 169" in desc
    assert "Change-Induced: No" not in desc


def test_s5_bad_inventory_deploy_is_change_induced_yes(agent_cfg, telemetry):
    inventory = _entry("0.s5", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    telemetry["changes"] = [_change("ata-inventory-api", INVENTORY_BAD_REV, INVENTORY_GOOD_REV, "2026-10-27T05:55:00Z")]
    telemetry["traffic"] = {"ata-inventory-api": {INVENTORY_GOOD_REV: [8, 8, 3]}}

    report = _investigate(_event(inventory), agent_cfg)

    assert report.change_induced == (
        f"Yes (revision {INVENTORY_BAD_REV} on ata-inventory-api deployed 2026-10-27T05:55:00Z (6m before the alert); "
        "ata-inventory-api degraded (alert ata-inventory-5xx fired on it) "
        "with no degraded upstream dependency)"
    )
    assert f"Change-Induced: Yes (revision {INVENTORY_BAD_REV} on ata-inventory-api" in _desc(report)


# ---------------------------------------------------------------------------------------
# No: change detection ran and found nothing implicated
# ---------------------------------------------------------------------------------------


def test_no_change_in_the_window_is_no_with_the_sources_checked(agent_cfg, telemetry):
    inventory = _entry("0.no-change", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}

    report = _investigate(_event(inventory), agent_cfg)

    assert report.change_induced == (
        "No (no deployment or configuration change on ata-inventory-api or its upstream dependencies "
        f"in the 2 h before the alert; checked: {TIMELINE})"
    )
    assert "Change-Induced: No (no deployment or configuration change on ata-inventory-api" in _desc(report)


def test_recent_change_not_implicated_is_no_with_the_reason(agent_cfg, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY_RECENT]

    report = _investigate(_event(S2_ORDERS), agent_cfg)

    assert report.change_induced.startswith("No (")
    assert f"recent revision {ORDERS_REV} not implicated" in report.change_induced
    assert report.change_induced.endswith(f"; checked: {TIMELINE})")


# ---------------------------------------------------------------------------------------
# Not assessed: the data was unavailable
# ---------------------------------------------------------------------------------------


def test_no_change_source_queried_is_not_assessed(agent_cfg, telemetry):
    inventory = _entry("0.no-source", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}

    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=ToolsNotConfigured("TOOLS_URL is not set")):
        report = _investigate(_event(inventory), agent_cfg)

    assert report.change_induced == (
        "Not assessed (no deployment or configuration change source was queried; change attribution not assessed)"
    )
    assert "Change-Induced: Not assessed (no deployment or configuration change source was queried" in _desc(report)


def test_failed_change_timeline_is_not_assessed(agent_cfg, telemetry):
    inventory = _entry("0.tl-fail", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}

    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=RuntimeError("deadline exceeded")):
        report = _investigate(_event(inventory), agent_cfg)

    assert report.change_induced == (
        "Not assessed (change detection incomplete: change timeline unavailable: RuntimeError: deadline exceeded; "
        "change attribution not assessed)"
    )


@pytest.mark.asyncio
async def test_s3_access_log_quota_exhausted_is_not_assessed(cfg):
    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=ToolsNotConfigured("TOOLS_URL is not set")):
        report = await _orders_report(cfg, FakeGCP(access_log_failures=99))
    assert report.change_induced.startswith("Not assessed (change detection incomplete: Cloud Logging read quota")


def _chg_ran() -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="ChangeSpecialist", target_resource="ata-orders-v1",
        metrics={CHANGE_SOURCES_KEY: ["Cloud Run audit log"]},
    )


def test_apigee_origin_with_truncated_access_log_is_not_assessed():
    apigee = EvidenceDossier(
        specialist_name="ApigeeSpecialist", target_resource="ata-orders-v1",
        metrics={"access_log_cap_hit": {"cap": 100}},
    )
    value = SynthesizerAgent()._change_induced(None, [_chg_ran(), apigee], "ata-orders-v1", None)
    assert value == (
        "Not assessed (Apigee proxy ata-orders-v1 revisions not evaluated (access log read cap hit); "
        "change attribution not assessed)"
    )


def test_apigee_origin_switch_without_spike_arrest_faults_is_not_assessed():
    apigee = EvidenceDossier(
        specialist_name="ApigeeSpecialist", target_resource="ata-orders-v1",
        metrics={
            CHANGE_SOURCES_KEY: ["Apigee access log ata-apigee-access (proxy revisions of ata-orders-v1)"],
            "proxy_revision_change": {
                "previous_revision": "168", "new_revision": "169", "new_first_served_at": "2026-10-27T12:52:24Z",
                "new_requests": 10, "new_spike_arrest_faults": 0, "previous_requests": 10,
                "previous_spike_arrest_faults": 0,
            },
        },
    )
    value = SynthesizerAgent()._change_induced(None, [_chg_ran(), apigee], "ata-orders-v1", None)
    assert value.startswith("Not assessed (Apigee proxy ata-orders-v1 switched from revision 168 to 169")


def test_apigee_origin_with_revisions_evaluated_and_no_switch_is_no():
    apigee = EvidenceDossier(
        specialist_name="ApigeeSpecialist", target_resource="ata-orders-v1",
        metrics={CHANGE_SOURCES_KEY: ["Apigee access log ata-apigee-access (proxy revisions of ata-orders-v1)"]},
    )
    value = SynthesizerAgent()._change_induced(None, [_chg_ran(), apigee], "ata-orders-v1", None)
    assert value == (
        "No (no deployment or configuration change on ata-orders-v1 or its upstream dependencies in the 2 h "
        "before the alert; checked: Cloud Run audit log, "
        "Apigee access log ata-apigee-access (proxy revisions of ata-orders-v1))"
    )


# ---------------------------------------------------------------------------------------
# Change sources recorded by the specialists
# ---------------------------------------------------------------------------------------


def test_change_specialist_records_the_timeline_as_checked(agent_cfg):
    full = {"changes": [], "apigee": {"org": "test-org", "environment": "test-env", "proxy_prefix": "ata-",
                                      "deployments": []}}
    with patch("agent.app.specialists.change.fetch_change_timeline", return_value=full):
        dossier = asyncio.run(ChangeSpecialist().investigate(_event(S2_ORDERS), agent_cfg))
    assert ", ".join(dossier.metrics[CHANGE_SOURCES_KEY]) == TIMELINE
    assert any("No Cloud Run revision deployments detected" in f for f in dossier.findings)
    assert any(f.startswith("Apigee deployment history (org test-org, environment test-env, proxies ata-*): "
                            "no proxy deployments") for f in dossier.findings)


def test_change_specialist_with_no_source_makes_no_negative_finding(agent_cfg):
    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=ToolsNotConfigured("TOOLS_URL is not set")):
        dossier = asyncio.run(ChangeSpecialist().investigate(_event(S2_ORDERS), agent_cfg))
    assert CHANGE_SOURCES_KEY not in dossier.metrics
    joined = " ".join(dossier.findings)
    assert "No configuration or revision deployments detected" not in joined
    assert "No Cloud Run revision deployments detected" not in joined
    assert "Change history not queried" in joined


@pytest.mark.asyncio
async def test_apigee_access_log_counts_as_checked_only_when_fully_read(cfg):
    with FakeGCP():
        full = await ApigeeSpecialist().investigate(_orders_alert(), cfg)
    assert full.metrics[CHANGE_SOURCES_KEY] == ["Apigee access log ata-apigee-access (proxy revisions of ata-orders-v1)"]

    capped = cfg.model_copy(update={"apigee_access_log_max_entries": 100})
    with FakeGCP():
        truncated = await ApigeeSpecialist().investigate(_orders_alert(), capped)
    assert CHANGE_SOURCES_KEY not in truncated.metrics


# ---------------------------------------------------------------------------------------
# Contract default (agent and bridge contracts stay identical)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("report_cls", [InvestigationReport, BridgeReport], ids=["agent", "bridge"])
def test_contract_default_is_not_assessed_and_set_values_are_kept(report_cls):
    def make(**kw):
        return report_cls(
            group_id="grp", filename="f.md", summary="s", observations="o", recommended_actions=[],
            probable_root_cause={"text": "x", "confidence": 0.9}, origin="ata-inventory-api", **kw,
        )

    assert "Change-Induced: Not assessed\n" in make().build_structured_description(project_id="test-project", incidents_bucket="test-project-ata-incidents")
    assert "Change-Induced: No\n" not in make().build_structured_description(project_id="test-project", incidents_bucket="test-project-ata-incidents")
    assert "Change-Induced: No (checked)\n" in make(change_induced="No (checked)").build_structured_description(project_id="test-project", incidents_bucket="test-project-ata-incidents")
    old = make(raw_metadata={"change_induced": "Yes (rev 2)"})
    assert "Change-Induced: Yes (rev 2)\n" in old.build_structured_description(project_id="test-project", incidents_bucket="test-project-ata-incidents")


@pytest.mark.parametrize("timeline,reason", [
    # The old ata-tools (queries env prod, ignores non-200) returns exactly this shape.
    ({"changes": []}, "no Apigee deployment summary (no 'apigee' block)"),
    ({"changes": [], "apigee": "n/a"}, "malformed Apigee deployment summary"),
    ({"changes": [], "apigee": {"proxy_prefix": "ata-", "deployments": []}}, "incomplete (missing org, environment)"),
    ({"changes": [], "apigee": {"org": "o", "environment": "e"}}, "incomplete (missing deployments)"),
])
def test_missing_or_incomplete_apigee_block_is_not_assessed_never_no_deployments(agent_cfg, timeline, reason):
    """crev R4: without the summary the ticket must not say "no proxy deployments"; it says the
    Apigee deployment history was not assessed, with the reason."""
    with patch("agent.app.specialists.change.fetch_change_timeline", return_value=timeline):
        dossier = asyncio.run(ChangeSpecialist().investigate(_event(S2_ORDERS), agent_cfg))
    joined = " ".join(dossier.findings)
    assert "no proxy deployments" not in joined
    assert not any(f.startswith("Apigee deployment history") for f in dossier.findings)
    assert "Apigee proxy deployment history was not assessed" in joined
    assert reason in joined
    assert "apigee_deployments_in_window" not in dossier.metrics
    # Cloud Run history still counts as checked; the Apigee half does not.
    assert dossier.metrics[CHANGE_SOURCES_KEY] == ["ata-tools change timeline (Cloud Run revision history)"]
