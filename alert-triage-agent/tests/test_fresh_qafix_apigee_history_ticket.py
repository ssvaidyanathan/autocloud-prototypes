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
