"""F9: the one-line Apigee deployment history is in the ticket description and work note."""

import pytest

from agent.app.contracts.report import InvestigationReport as AgentReport
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent
from bridge.app.contracts.report import InvestigationReport as BridgeReport

HIST = ("Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies ata-*): "
        "proxy ata-orders-v1 revision 184 deployed 2026-10-28T01:34:37Z (7m before alert onset).")
SPELLED = HIST.replace("proxies ata-*", "proxies with prefix ata-")
S1_OBS = " | ".join(f"Observation {i} on ata-inventory-api" for i in range(1, 6))


def _report(cls, hist):
    return cls(group_id="g", summary="Elevated HTTP 5xx error rate (85.1%) on ata-inventory-api",
               observations=S1_OBS, probable_root_cause={"text": "x", "confidence": 0.8},
               origin="ata-inventory-api", blast_radius=["ata-inventory-api"], priority="P1",
               change_induced="No (checked: ata-tools change timeline (Apigee deployment history))",
               apigee_history=hist)


def test_synthesizer_takes_the_change_specialist_line_and_spells_out_the_glob():
    chg = EvidenceDossier("ChangeSpecialist", "ata-orders-v1", findings=["No recent deploys.", HIST])
    assert SynthesizerAgent._apigee_history_line([chg]) == SPELLED
    assert SynthesizerAgent._apigee_history_line([EvidenceDossier("ChangeSpecialist", "x")]) is None
    assert SynthesizerAgent._apigee_history_line([]) is None


@pytest.mark.parametrize("cls", [AgentReport, BridgeReport])
def test_description_and_work_note_carry_the_history_line(cls):
    r = _report(cls, SPELLED)
    desc = r.build_structured_description(project_id="p", incidents_bucket="b")
    lines = desc.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("Change-Induced:"))
    assert lines[i + 1] == SPELLED
    note = r.build_executive_summary_html()
    assert "proxy ata-orders-v1 revision 184 deployed 2026-10-28T01:34:37Z" in note
    # Not one of the capped observations: all 5 observations still shown, no "more" marker.
    assert "Observation 5 on ata-inventory-api" in note and "more (see attached report)" not in note


@pytest.mark.parametrize("cls", [AgentReport, BridgeReport])
def test_no_history_no_line(cls):
    r = _report(cls, None)
    desc = r.build_structured_description(project_id="p", incidents_bucket="b")
    assert "Apigee deployment history (" not in desc
    assert "Apigee deployment history (" not in r.build_executive_summary_html()


# --- N3: ticket history scoped to the group (final-qa2 S3, INC0010352 inventory 5xx) -------
# The orders-v1 r186 deploy belongs to the concurrent 429 group (INC0010353). The inventory
# ticket counts it ("1 other proxy changed in window") instead of naming it; the HTML keeps it.

S3_FULL = ("Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies ata-*): "
           "proxy ata-orders-v1 revision 186 deployed 2026-10-28T05:09:31Z (6m before alert onset).")
S3_IN_WINDOW = [{"proxy": "ata-orders-v1", "revision": "186", "environment": "prod",
                 "timestamp": "2026-10-28T05:09:31Z", "deployed_minutes_ago": 6}]


def _s3_change_dossier(in_window=S3_IN_WINDOW):
    return EvidenceDossier(
        "ChangeSpecialist", "ata-inventory-api", findings=[S3_FULL],
        metrics={"apigee_deployments_in_window": in_window,
                 "apigee_deployment_scope": "org cloud-assist-fde-2, environment prod, proxies ata-*",
                 "apigee_deployment_window_minutes": 120},
    )


def _scope(origin, resource):
    from agent.app.contracts.alert import AlertEvent

    alert = AlertEvent(incident_id="0.x", policy_name="p", resource_name=resource, state="open",
                       started_at="2026-10-28T05:16:16Z")
    return SynthesizerAgent._history_scope(origin, alert, None)


def test_history_scope_is_group_components_plus_direct_ingress_proxies():
    # ata-orders-v1 reaches ata-inventory-api only through ata-orders-api: not in scope.
    assert _scope("ata-inventory-api", "ata-inventory-api") == ["ata-inventory-api", "ata-inventory-v1"]
    assert _scope("ata-orders-v1", "ata-orders-v1") == ["ata-orders-v1"]


def test_s3_inventory_ticket_counts_the_other_groups_proxy_and_does_not_name_it():
    line = SynthesizerAgent._apigee_history_line(
        [_s3_change_dossier()], origin="ata-inventory-api", components=["ata-inventory-api", "ata-inventory-v1"],
    )
    assert line == (
        "Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies with prefix ata-; "
        "group components and proxies routing directly to ata-inventory-api: ata-inventory-api, ata-inventory-v1): "
        "no deployments of these proxies in the 2-hour window prior to alert onset "
        "(1 other proxy changed in window, see report)."
    )
    assert "ata-orders-v1" not in line and "186" not in line
    # R4: never "no proxy deployments" as a blanket claim; the other change is counted.
    assert "no proxy deployments" not in line


def test_s3_orders_ticket_keeps_its_own_proxy_change():
    line = SynthesizerAgent._apigee_history_line(
        [_s3_change_dossier()], origin="ata-orders-v1", components=["ata-orders-v1"],
    )
    assert line == SPELLED_S3


SPELLED_S3 = S3_FULL.replace("proxies ata-*", "proxies with prefix ata-")


def test_in_scope_and_other_changes_name_in_scope_and_count_others():
    both = S3_IN_WINDOW + [
        {"proxy": "ata-inventory-v1", "revision": "12", "environment": "prod",
         "timestamp": "2026-10-28T05:00:00Z", "deployed_minutes_ago": 16},
        {"proxy": "ata-x-v1", "revision": "3", "environment": "prod",
         "timestamp": "2026-10-28T05:01:00Z", "deployed_minutes_ago": 15},
    ]
    line = SynthesizerAgent._apigee_history_line(
        [_s3_change_dossier(both)], origin="ata-inventory-api", components=["ata-inventory-api", "ata-inventory-v1"],
    )
    assert ": proxy ata-inventory-v1 revision 12 deployed 2026-10-28T05:00:00Z (16m before alert onset) " in line
    assert line.endswith("(2 other proxies changed in window, see report).")
    assert "ata-orders-v1" not in line and "ata-x-v1" not in line


def test_no_window_changes_keeps_the_full_line():
    line = SynthesizerAgent._apigee_history_line(
        [_s3_change_dossier([])], origin="ata-inventory-api", components=["ata-inventory-api", "ata-inventory-v1"],
    )
    assert line == SPELLED_S3  # the finding text itself is unchanged when nothing needs scoping
