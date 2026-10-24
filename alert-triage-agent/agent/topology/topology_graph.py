"""Topology Graph Definition and Traversal for Alert Triage Agent."""

import re
from typing import Dict, List, Set, Optional
from collections import deque


# Canonical node names
NODE_APIGEE_ORDERS = "ata-apigee-orders-v1"
NODE_APIGEE_INVENTORY = "ata-apigee-inventory-v1"
NODE_ORDERS_API = "ata-orders-api"
NODE_INVENTORY_API = "ata-inventory-api"
NODE_INVENTORY_DB = "ata-inventory-db"

# Alternate and short names
NODE_ORDERS_V1 = "ata-orders-v1"
NODE_INVENTORY_V1 = "ata-inventory-v1"

NODES = [
    NODE_APIGEE_ORDERS,
    NODE_APIGEE_INVENTORY,
    NODE_ORDERS_API,
    NODE_INVENTORY_API,
    NODE_INVENTORY_DB,
]

# Aliases mapping alternate names and label formats to canonical nodes
NODE_ALIASES: Dict[str, str] = {
    NODE_ORDERS_V1: NODE_APIGEE_ORDERS,
    NODE_INVENTORY_V1: NODE_APIGEE_INVENTORY,
    "orders-v1": NODE_APIGEE_ORDERS,
    "inventory-v1": NODE_APIGEE_INVENTORY,
    NODE_APIGEE_ORDERS: NODE_APIGEE_ORDERS,
    NODE_APIGEE_INVENTORY: NODE_APIGEE_INVENTORY,
    NODE_ORDERS_API: NODE_ORDERS_API,
    "orders-api": NODE_ORDERS_API,
    NODE_INVENTORY_API: NODE_INVENTORY_API,
    "inventory-api": NODE_INVENTORY_API,
    NODE_INVENTORY_DB: NODE_INVENTORY_DB,
    "inventory-db": NODE_INVENTORY_DB,
}

# Directed caller -> callee edges
# Each edge (u, v) means u depends on / calls v
DIRECTED_EDGES = [
    (NODE_APIGEE_ORDERS, NODE_ORDERS_API),
    (NODE_APIGEE_INVENTORY, NODE_INVENTORY_API),
    (NODE_ORDERS_API, NODE_INVENTORY_API),
    (NODE_INVENTORY_API, NODE_INVENTORY_DB),
]


def _canonicalize_node(node: str) -> str:
    """Normalize raw monitoring resource string, labels, or path to a canonical topology node."""
    if not node:
        return node
    node_str = str(node).strip()
    if node_str in NODES:
        return node_str
    if node_str in NODE_ALIASES:
        return NODE_ALIASES[node_str]

    # Check key=value regexes
    for pattern in (
        r"service_name\s*=\s*['\"]?([^,'\"\s}]+)",
        r"configuration_name\s*=\s*['\"]?([^,'\"\s}]+)",
        r"(?:proxy_name|apiproxy|proxy)\s*=\s*['\"]?([^,'\"\s}]+)",
        r"instance_id\s*=\s*['\"]?([^,'\"\s}]+)",
    ):
        m = re.search(pattern, node_str)
        if m:
            val = m.group(1).strip()
            if val in NODES:
                return val
            if val in NODE_ALIASES:
                return NODE_ALIASES[val]

    m = re.search(r"database_id\s*=\s*['\"]?([^,'\"\s}]+)", node_str)
    if m:
        db_id = m.group(1).strip().split(":")[-1]
        if db_id in NODES:
            return db_id
        if db_id in NODE_ALIASES:
            return NODE_ALIASES[db_id]

    # Path split
    cleaned = node_str.split("/")[-1]
    if cleaned in NODES:
        return cleaned
    if cleaned in NODE_ALIASES:
        return NODE_ALIASES[cleaned]

    return node_str


def get_topology_graph() -> Dict[str, any]:
    """Returns the full directed topology DAG with nodes and edges."""
    return {
        "nodes": list(NODES),
        "edges": [{"from": u, "to": v} for u, v in DIRECTED_EDGES],
    }


def get_upstream_dependencies(target_node: str) -> List[str]:
    """Returns all components that the target_node depends on (callees).
    
    Traverses forward along caller -> callee directed edges.
    Returns dependencies ordered by topological distance (direct callees first).
    """
    node = _canonicalize_node(target_node)
    adjacency: Dict[str, List[str]] = {n: [] for n in NODES}
    for u, v in DIRECTED_EDGES:
        adjacency[u].append(v)

    visited: Set[str] = set()
    ordered: List[str] = []
    queue = deque([node])

    while queue:
        curr = queue.popleft()
        for neighbor in adjacency.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                ordered.append(neighbor)
                queue.append(neighbor)

    return ordered



def get_blast_radius(origin_node: str) -> List[str]:
    """Returns all downstream consumers impacted if origin_node fails.
    
    If component X fails, all components that call/depend on X (directly
    or transitively) are impacted. This traverses reverse edges
    (callee -> caller).
    """
    node = _canonicalize_node(origin_node)
    # Reverse adjacency: callee -> callers
    reverse_adjacency: Dict[str, List[str]] = {n: [] for n in NODES}
    for u, v in DIRECTED_EDGES:
        reverse_adjacency[v].append(u)

    visited: Set[str] = set()
    queue = deque([node])

    while queue:
        curr = queue.popleft()
        for caller in reverse_adjacency.get(curr, []):
            if caller not in visited:
                visited.add(caller)
                queue.append(caller)

    return sorted(list(visited))


def calculate_hop_distance(node_a: str, node_b: str) -> Optional[int]:
    """Calculates the shortest undirected hop distance between node_a and node_b.
    
    Returns 0 if node_a == node_b, or None if nodes are disconnected.
    """
    node_a = _canonicalize_node(node_a)
    node_b = _canonicalize_node(node_b)
    if node_a not in NODES or node_b not in NODES:
        return None
    if node_a == node_b:
        return 0

    # Build undirected adjacency
    undirected: Dict[str, List[str]] = {n: [] for n in NODES}
    for u, v in DIRECTED_EDGES:
        undirected[u].append(v)
        undirected[v].append(u)

    visited: Set[str] = {node_a}
    queue = deque([(node_a, 0)])

    while queue:
        curr, dist = queue.popleft()
        if curr == node_b:
            return dist
        for neighbor in undirected.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, dist + 1))

    return None
