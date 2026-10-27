"""Regression tests for the quality bugs found by the live QA run (S1 INC0010321, S2 INC0010322).

1. Description 'Dependency Path' is the topology path from impacted entry points to the origin,
   not the reversed blast radius.
2. Work-note alert lines are distinguishable when one policy fires twice on one resource.
3. The RCA states the Apigee gateway timeout only from gathered data (never a guessed value).
4. Log excerpts label only genuine error lines as errors (severity- and status-aware).
"""

from agent.app.contracts.report import InvestigationReport, ProbableRootCause
from agent.app.specialists.synthesizer import SynthesizerAgent, dependency_path_for
from topology.topology_graph import get_loader

# --- S1/S2 shape: origin ata-inventory-api, blast radius as the agent reported it ---------
S1_ORIGIN = "ata-inventory-api"
S1_BLAST_RADIUS = ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]
S1_EXPECTED_PATH = (
    "ata-inventory-v1 -> ata-inventory-api; "
    "ata-orders-v1 -> ata-orders-api -> ata-inventory-api"
)


def _report(**kwargs) -> InvestigationReport:
    base = dict(
        group_id="ata-inventory-5xx-ata-inventory-api-20261027t071737z",
        summary="Service degradation on ata-inventory-api",
        observations="Cloud Monitoring policy 'ata-inventory-5xx' triggered on 'ata-inventory-api'",
        probable_root_cause=ProbableRootCause(text="x", confidence=0.81),
        origin=S1_ORIGIN,
        blast_radius=S1_BLAST_RADIUS,
        priority="P1",
    )
    base.update(kwargs)
    return InvestigationReport(**base)


# ---------------------------------------------------------------------------------------
# Bug 1: Dependency Path
# ---------------------------------------------------------------------------------------


def test_dependency_paths_follow_topology_edges_to_origin():
    paths = get_loader().dependency_paths(S1_ORIGIN)
    assert paths == [
        ["ata-inventory-v1", "ata-inventory-api"],
        ["ata-orders-v1", "ata-orders-api", "ata-inventory-api"],
    ]
    # Every hop is a real TOPOLOGY.md edge (caller -> callee).
    edges = {(e.from_node, e.to_node) for e in get_loader().edges}
    for p in paths:
        for a, b in zip(p, p[1:]):
            assert (a, b) in edges


def test_dependency_paths_for_db_origin_and_leaf_and_unknown():
    db_paths = get_loader().dependency_paths("ata-inventory-db")
    assert ["ata-orders-v1", "ata-orders-api", "ata-inventory-api", "ata-inventory-db"] in db_paths
    assert ["ata-inventory-v1", "ata-inventory-api", "ata-inventory-db"] in db_paths
    # Nothing depends on an Apigee proxy: the path is the proxy itself.
    assert get_loader().dependency_paths("ata-orders-v1") == [["ata-orders-v1"]]
    # Aliases resolve to the canonical component.
    assert get_loader().dependency_paths("inventory-api") == get_loader().dependency_paths(S1_ORIGIN)
    assert get_loader().dependency_paths("not-a-component") == []
    assert get_loader().format_dependency_paths("not-a-component") is None


def test_synthesizer_dependency_path_is_not_reversed_blast_radius():
    dep = SynthesizerAgent()._determine_dependency_path(S1_ORIGIN, S1_BLAST_RADIUS)
    assert dep == S1_EXPECTED_PATH
    # The live S1 ticket showed this false chain (orders-api does not call inventory-v1).
    assert dep != " -> ".join(reversed(S1_BLAST_RADIUS))
    assert "ata-orders-api -> ata-inventory-v1" not in dep


def test_dependency_path_for_unknown_origin_is_none_and_logged(caplog):
    with caplog.at_level("WARNING"):
        assert dependency_path_for("not-a-component") is None
    assert "No topology dependency path for not-a-component" in caplog.text


def test_description_renders_topology_path():
    desc = _report(dependency_path=S1_EXPECTED_PATH).build_structured_description()
    assert f"  - Dependency Path: {S1_EXPECTED_PATH}\n" in desc
    assert "ata-orders-v1 -> ata-orders-api -> ata-inventory-v1" not in desc


def test_description_without_path_omits_line_instead_of_guessing():
    desc = _report().build_structured_description()
    assert "Dependency Path" not in desc
    assert "Client -> Ingress" not in desc
    assert "ata-orders-v1 -> ata-orders-api -> ata-inventory-v1 -> ata-inventory-api" not in desc


# ---------------------------------------------------------------------------------------
# Bug 2: two alerts of one policy on one resource (S2: one per response_code series)
# ---------------------------------------------------------------------------------------

from agent.app.contracts.alert import AlertEvent  # noqa: E402
from agent.app.specialists.synthesizer import apply_group_alerts, format_alert_trigger_line  # noqa: E402


def _s2_alert(inc_id: str, policy: str, resource: str, started_at: str) -> dict:
    return {
        "incident_id": inc_id,
        "policy": policy,
        "policy_name": policy,
        "resource": resource,
        "resource_name": resource,
        "component": resource,
        "signal": "latency",
        "state": "open",
        "started_at": started_at,
    }


S2_ALERTS = [
    _s2_alert("0.oeb4hxzrsiqu", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:40:49Z"),
    _s2_alert("0.oeb4hxzrsiqp", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:40:49Z"),
    _s2_alert("0.oeb4j8ur7t63", "ata-orders-latency", "ata-orders-api", "2026-10-27T07:42:24Z"),
]


def _s2_report() -> InvestigationReport:
    primary = S2_ALERTS[0]
    trigger = format_alert_trigger_line(
        primary["policy"], primary["resource"], primary["started_at"], "open", primary["incident_id"]
    )
    return _report(
        group_id="ata-inventory-latency-ata-inventory-api-20261027t074049z",
        observations=f"{trigger} | HTTP 5xx error rate reached 0.0% (33 rpm) on ata-inventory-api",
        priority="P2",
        alerts=[dict(primary)],
    )


def test_trigger_observation_carries_incident_id():
    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-27T07:40:49Z",
        state="open",
    )
    obs = SynthesizerAgent()._synthesize_observations([], alert)
    assert obs.split(" | ")[0] == (
        "Cloud Monitoring policy 'ata-inventory-latency' triggered on 'ata-inventory-api' "
        "at 2026-10-27T07:40:49Z (state: open, incident 0.oeb4hxzrsiqu)"
    )


def test_same_policy_same_resource_alert_lines_are_distinguishable():
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS])
    items = report.observations.split(" | ")
    alert_lines = [i for i in items if "Cloud Monitoring policy" in i]
    assert len(alert_lines) == 3
    assert len(set(alert_lines)) == 3
    # Stripping the correlated prefix must not make two lines identical (the S2 work note did).
    assert len({ln.replace("Correlated alert: ", "") for ln in alert_lines}) == 3
    for a in S2_ALERTS:
        assert sum(f"incident {a['incident_id']})" in ln for ln in alert_lines) == 1
    # The title count still equals the alerts in the group.
    assert report.alert_count() == 3
    assert report.build_short_description().endswith("(3 alerts)")
    html = report.build_executive_summary_html()
    for a in S2_ALERTS:
        assert a["incident_id"] in html


def test_third_alert_of_same_policy_is_not_dropped():
    extra = _s2_alert("0.third", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:41:00Z")
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS] + [extra])
    assert "incident 0.third)" in report.observations
    assert report.alert_count() == 4


def test_apply_group_alerts_stays_idempotent_with_incident_ids():
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS])
    once = report.observations
    report = apply_group_alerts(report, [dict(a) for a in S2_ALERTS])
    assert report.observations == once
    assert report.alert_count() == 3
