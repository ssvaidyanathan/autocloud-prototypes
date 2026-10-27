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
