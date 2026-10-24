"""Unit tests for topology graph and traversal."""

import pytest
from topology.topology_graph import (
    get_topology_graph,
    get_blast_radius,
    get_upstream_dependencies,
    calculate_hop_distance,
    NODE_APIGEE_ORDERS,
    NODE_APIGEE_INVENTORY,
    NODE_ORDERS_API,
    NODE_INVENTORY_API,
    NODE_INVENTORY_DB,
    NODES,
)


def test_topology_graph_structure():
    graph = get_topology_graph()
    assert len(graph["nodes"]) == 5
    assert set(graph["nodes"]) == set(NODES)
    assert len(graph["edges"]) == 4


def test_hop_distance_self():
    for node in NODES:
        assert calculate_hop_distance(node, node) == 0


def test_hop_distance_direct_neighbors():
    assert calculate_hop_distance(NODE_INVENTORY_API, NODE_INVENTORY_DB) == 1
    assert calculate_hop_distance(NODE_ORDERS_API, NODE_INVENTORY_API) == 1
    assert calculate_hop_distance(NODE_APIGEE_ORDERS, NODE_ORDERS_API) == 1
    assert calculate_hop_distance(NODE_APIGEE_INVENTORY, NODE_INVENTORY_API) == 1


def test_hop_distance_multi_hop():
    # orders-api -> inventory-api -> inventory-db = 2 hops
    assert calculate_hop_distance(NODE_ORDERS_API, NODE_INVENTORY_DB) == 2
    # apigee-orders -> orders-api -> inventory-api -> inventory-db = 3 hops
    assert calculate_hop_distance(NODE_APIGEE_ORDERS, NODE_INVENTORY_DB) == 3
    # apigee-inventory -> inventory-api -> inventory-db = 2 hops
    assert calculate_hop_distance(NODE_APIGEE_INVENTORY, NODE_INVENTORY_DB) == 2
    # apigee-orders -> orders-api -> inventory-api -> apigee-inventory = 3 hops
    assert calculate_hop_distance(NODE_APIGEE_ORDERS, NODE_APIGEE_INVENTORY) == 3


def test_hop_distance_unknown_node():
    assert calculate_hop_distance("unknown-node", NODE_INVENTORY_DB) is None
    assert calculate_hop_distance(NODE_INVENTORY_DB, "unknown-node") is None


def test_blast_radius_db_failure():
    # DB failure cascades to everything upstream
    impacted = get_blast_radius(NODE_INVENTORY_DB)
    expected = {
        NODE_INVENTORY_API,
        NODE_APIGEE_INVENTORY,
        NODE_ORDERS_API,
        NODE_APIGEE_ORDERS,
    }
    assert set(impacted) == expected


def test_blast_radius_inventory_api_failure():
    # Inventory API failure affects both its proxy and orders tier
    impacted = get_blast_radius(NODE_INVENTORY_API)
    expected = {
        NODE_APIGEE_INVENTORY,
        NODE_ORDERS_API,
        NODE_APIGEE_ORDERS,
    }
    assert set(impacted) == expected


def test_blast_radius_orders_api_failure():
    # Orders API failure affects only orders proxy
    impacted = get_blast_radius(NODE_ORDERS_API)
    assert set(impacted) == {NODE_APIGEE_ORDERS}


def test_blast_radius_apigee_ingress():
    # Edge proxies have no callers in internal topology
    assert get_blast_radius(NODE_APIGEE_ORDERS) == []
    assert get_blast_radius(NODE_APIGEE_INVENTORY) == []


def test_upstream_dependencies():
    # Orders API depends on inventory API and inventory DB
    orders_deps = get_upstream_dependencies(NODE_ORDERS_API)
    assert set(orders_deps) == {NODE_INVENTORY_API, NODE_INVENTORY_DB}

    # Apigee orders proxy depends on orders API, inventory API, inventory DB
    apigee_deps = get_upstream_dependencies(NODE_APIGEE_ORDERS)
    assert set(apigee_deps) == {NODE_ORDERS_API, NODE_INVENTORY_API, NODE_INVENTORY_DB}

    # Inventory DB has no upstream dependencies
    assert get_upstream_dependencies(NODE_INVENTORY_DB) == []


def test_blast_radius_with_raw_monitoring_string():
    raw_str = "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00001}"
    impacted = get_blast_radius(raw_str)
    expected = {
        NODE_APIGEE_INVENTORY,
        NODE_ORDERS_API,
        NODE_APIGEE_ORDERS,
    }
    assert set(impacted) == expected


def test_blast_radius_with_raw_sql_string():
    raw_sql = "Cloud SQL Database labels {database_id=cloud-assist-fde-2:ata-inventory-db}"
    impacted = get_blast_radius(raw_sql)
    assert NODE_INVENTORY_API in impacted
    assert NODE_ORDERS_API in impacted
    assert NODE_APIGEE_ORDERS in impacted
    assert NODE_APIGEE_INVENTORY in impacted


def test_proxy_aliases_canonicalization_and_hop_distance():
    from topology.topology_graph import _canonicalize_node, NODE_ORDERS_V1, NODE_INVENTORY_V1

    assert _canonicalize_node(NODE_ORDERS_V1) == NODE_APIGEE_ORDERS
    assert _canonicalize_node(NODE_INVENTORY_V1) == NODE_APIGEE_INVENTORY
    assert _canonicalize_node("orders-v1") == NODE_APIGEE_ORDERS
    assert _canonicalize_node("inventory-v1") == NODE_APIGEE_INVENTORY
    assert _canonicalize_node("Apigee Proxy labels {proxy_name=ata-orders-v1}") == NODE_APIGEE_ORDERS

    # Hop distance using aliased names
    assert calculate_hop_distance("ata-orders-v1", NODE_ORDERS_API) == 1
    assert calculate_hop_distance("ata-orders-v1", NODE_INVENTORY_API) == 2
    assert calculate_hop_distance("ata-orders-v1", NODE_INVENTORY_DB) == 3

    # Upstream dependencies using aliased names
    deps = get_upstream_dependencies("ata-orders-v1")
    assert set(deps) == {NODE_ORDERS_API, NODE_INVENTORY_API, NODE_INVENTORY_DB}


def test_blast_radius_inventory_api_includes_all_dependents():
    """Verify get_blast_radius('ata-inventory-api') includes both proxies and orders API."""
    dependents = get_blast_radius(NODE_INVENTORY_API)
    assert set(dependents) == {
        NODE_APIGEE_INVENTORY,
        NODE_ORDERS_API,
        NODE_APIGEE_ORDERS,
    }


