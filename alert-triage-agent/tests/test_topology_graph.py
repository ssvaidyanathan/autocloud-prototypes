"""Unit tests for topology graph, loader, and traversal."""

import tempfile
import pytest
from topology.loader import TopologyLoader, TopologyValidationError
from topology.topology_graph import (
    get_topology_graph,
    get_topology,
    get_blast_radius,
    blast_radius,
    get_upstream_dependencies,
    upstream,
    downstream,
    calculate_hop_distance,
    hop_distance,
    canonicalize,
    _canonicalize_node,
    get_loader,
    NODES,
    NODE_ALIASES,
    DIRECTED_EDGES,
)

# Test constants matching the demo topology
NODE_ORDERS_V1 = "ata-orders-v1"
NODE_INVENTORY_V1 = "ata-inventory-v1"
NODE_ORDERS_API = "ata-orders-api"
NODE_INVENTORY_API = "ata-inventory-api"
NODE_INVENTORY_DB = "ata-inventory-db"

# Aliases
ALIAS_APIGEE_ORDERS = "ata-apigee-orders-v1"
ALIAS_APIGEE_INVENTORY = "ata-apigee-inventory-v1"
ALIAS_ORDERS_SHORT = "orders-v1"
ALIAS_INVENTORY_SHORT = "inventory-v1"


def test_topology_graph_structure():
    graph = get_topology_graph()
    assert len(graph["nodes"]) == 5
    assert set(graph["nodes"]) == set(NODES)
    assert len(graph["edges"]) == 4


def test_get_topology_tool_output():
    topo = get_topology(resource=NODE_INVENTORY_API)
    assert "source_file" in topo
    assert len(topo["components"]) == 5
    assert len(topo["edges"]) == 4
    assert topo["resource"] == NODE_INVENTORY_API
    assert topo["upstream"] == [NODE_INVENTORY_DB]
    assert set(topo["downstream"]) == {NODE_INVENTORY_V1, NODE_ORDERS_API, NODE_ORDERS_V1}


def test_hop_distance_self():
    for node in NODES:
        assert calculate_hop_distance(node, node) == 0
        assert hop_distance(node, node) == 0


def test_hop_distance_direct_neighbors():
    assert calculate_hop_distance(NODE_INVENTORY_API, NODE_INVENTORY_DB) == 1
    assert calculate_hop_distance(NODE_ORDERS_API, NODE_INVENTORY_API) == 1
    assert calculate_hop_distance(NODE_ORDERS_V1, NODE_ORDERS_API) == 1
    assert calculate_hop_distance(NODE_INVENTORY_V1, NODE_INVENTORY_API) == 1


def test_hop_distance_multi_hop():
    # orders-api -> inventory-api -> inventory-db = 2 hops
    assert calculate_hop_distance(NODE_ORDERS_API, NODE_INVENTORY_DB) == 2
    # orders-v1 -> orders-api -> inventory-api -> inventory-db = 3 hops
    assert calculate_hop_distance(NODE_ORDERS_V1, NODE_INVENTORY_DB) == 3
    # inventory-v1 -> inventory-api -> inventory-db = 2 hops
    assert calculate_hop_distance(NODE_INVENTORY_V1, NODE_INVENTORY_DB) == 2
    # orders-v1 -> orders-api -> inventory-api -> inventory-v1 = 3 hops
    assert calculate_hop_distance(NODE_ORDERS_V1, NODE_INVENTORY_V1) == 3


def test_hop_distance_unknown_node():
    assert calculate_hop_distance("unknown-node", NODE_INVENTORY_DB) is None
    assert calculate_hop_distance(NODE_INVENTORY_DB, "unknown-node") is None


def test_blast_radius_db_failure():
    # DB failure cascades to everything upstream
    impacted = get_blast_radius(NODE_INVENTORY_DB)
    expected = {
        NODE_INVENTORY_API,
        NODE_INVENTORY_V1,
        NODE_ORDERS_API,
        NODE_ORDERS_V1,
    }
    assert set(impacted) == expected
    assert blast_radius(NODE_INVENTORY_DB) == sorted(list(expected))


def test_blast_radius_inventory_api_failure():
    # Inventory API failure affects both its proxy and orders tier
    impacted = get_blast_radius(NODE_INVENTORY_API)
    expected = {
        NODE_INVENTORY_V1,
        NODE_ORDERS_API,
        NODE_ORDERS_V1,
    }
    assert set(impacted) == expected


def test_blast_radius_orders_api_failure():
    # Orders API failure affects only orders proxy
    impacted = get_blast_radius(NODE_ORDERS_API)
    assert set(impacted) == {NODE_ORDERS_V1}


def test_blast_radius_apigee_ingress():
    # Edge proxies have no callers in internal topology
    assert get_blast_radius(NODE_ORDERS_V1) == []
    assert get_blast_radius(NODE_INVENTORY_V1) == []


def test_upstream_dependencies():
    # Orders API depends on inventory API and inventory DB
    orders_deps = get_upstream_dependencies(NODE_ORDERS_API)
    assert set(orders_deps) == {NODE_INVENTORY_API, NODE_INVENTORY_DB}
    assert upstream(NODE_ORDERS_API) == [NODE_INVENTORY_API, NODE_INVENTORY_DB]

    # Apigee orders proxy depends on orders API, inventory API, inventory DB
    apigee_deps = get_upstream_dependencies(NODE_ORDERS_V1)
    assert set(apigee_deps) == {NODE_ORDERS_API, NODE_INVENTORY_API, NODE_INVENTORY_DB}

    # Inventory DB has no upstream dependencies
    assert get_upstream_dependencies(NODE_INVENTORY_DB) == []


def test_blast_radius_with_raw_monitoring_string():
    raw_str = "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00001}"
    impacted = get_blast_radius(raw_str)
    expected = {
        NODE_INVENTORY_V1,
        NODE_ORDERS_API,
        NODE_ORDERS_V1,
    }
    assert set(impacted) == expected


def test_blast_radius_with_raw_sql_string():
    raw_sql = "Cloud SQL Database labels {database_id=cloud-assist-fde-2:ata-inventory-db}"
    impacted = get_blast_radius(raw_sql)
    assert NODE_INVENTORY_API in impacted
    assert NODE_ORDERS_API in impacted
    assert NODE_ORDERS_V1 in impacted
    assert NODE_INVENTORY_V1 in impacted


def test_proxy_aliases_canonicalization_and_hop_distance():
    assert _canonicalize_node(ALIAS_APIGEE_ORDERS) == NODE_ORDERS_V1
    assert _canonicalize_node(ALIAS_APIGEE_INVENTORY) == NODE_INVENTORY_V1
    assert _canonicalize_node(ALIAS_ORDERS_SHORT) == NODE_ORDERS_V1
    assert _canonicalize_node(ALIAS_INVENTORY_SHORT) == NODE_INVENTORY_V1
    assert _canonicalize_node("Apigee Proxy labels {proxy_name=ata-orders-v1}") == NODE_ORDERS_V1
    assert canonicalize("Apigee Proxy labels {proxy_name=ata-apigee-orders-v1}") == NODE_ORDERS_V1

    # Hop distance using aliased names
    assert calculate_hop_distance(ALIAS_APIGEE_ORDERS, NODE_ORDERS_API) == 1
    assert calculate_hop_distance(ALIAS_APIGEE_ORDERS, NODE_INVENTORY_API) == 2
    assert calculate_hop_distance(ALIAS_APIGEE_ORDERS, NODE_INVENTORY_DB) == 3

    # Upstream dependencies using aliased names
    deps = get_upstream_dependencies(ALIAS_APIGEE_ORDERS)
    assert set(deps) == {NODE_ORDERS_API, NODE_INVENTORY_API, NODE_INVENTORY_DB}


def test_blast_radius_inventory_api_includes_all_dependents():
    """Verify get_blast_radius('ata-inventory-api') includes both proxies and orders API."""
    dependents = get_blast_radius(NODE_INVENTORY_API)
    assert set(dependents) == {
        NODE_INVENTORY_V1,
        NODE_ORDERS_API,
        NODE_ORDERS_V1,
    }


def test_loader_validation_duplicate_id():
    """TopologyLoader raises TopologyValidationError on duplicate component IDs."""
    content = """
# Test Topology
## Components
| Component | Type |
|---|---|
| svc-a | Cloud Run |
| svc-a | Cloud Run |
## Dependencies
| From | To |
|---|---|
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(content)
        f.flush()
        with pytest.raises(TopologyValidationError, match="Duplicate component ID"):
            TopologyLoader(f.name)


def test_loader_validation_unknown_type():
    """TopologyLoader raises TopologyValidationError on invalid component type."""
    content = """
# Test Topology
## Components
| Component | Type |
|---|---|
| svc-a | Serverless Function |
## Dependencies
| From | To |
|---|---|
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(content)
        f.flush()
        with pytest.raises(TopologyValidationError, match="Unknown component type"):
            TopologyLoader(f.name)


def test_loader_validation_unknown_edge_nodes():
    """TopologyLoader raises TopologyValidationError on edges pointing to unknown nodes."""
    content = """
# Test Topology
## Components
| Component | Type |
|---|---|
| svc-a | Cloud Run |
## Dependencies
| From | To |
|---|---|
| svc-a | non-existent-service |
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(content)
        f.flush()
        with pytest.raises(TopologyValidationError, match="is not declared in Components table"):
            TopologyLoader(f.name)


def test_loader_validation_cycle_detection():
    """TopologyLoader raises TopologyValidationError when cycles are present."""
    content = """
# Test Topology
## Components
| Component | Type |
|---|---|
| svc-a | Cloud Run |
| svc-b | Cloud Run |
## Dependencies
| From | To |
|---|---|
| svc-a | svc-b |
| svc-b | svc-a |
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(content)
        f.flush()
        with pytest.raises(TopologyValidationError, match="Directed cycle detected"):
            TopologyLoader(f.name)


def test_loader_validation_missing_file():
    """TopologyLoader raises FileNotFoundError on non-existent path."""
    with pytest.raises(FileNotFoundError):
        TopologyLoader("/tmp/non_existent_topology_12345.md")
