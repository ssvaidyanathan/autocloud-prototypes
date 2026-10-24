"""Topology Graph Interface for Alert Triage Agent.

Delegates to topology.loader for parsing, validating, and traversing TOPOLOGY.md.
No hardcoded service names or topology edges exist in this module.
"""

from typing import Any, Dict, List, Optional
try:
    from .loader import (
        TopologyLoader,
        TopologyValidationError,
        get_loader,
        canonicalize,
        _canonicalize_node,
        upstream,
        get_upstream_dependencies,
        downstream,
        blast_radius,
        get_blast_radius,
        hop_distance,
        calculate_hop_distance,
        get_topology,
        get_topology_graph,
    )
except ImportError:
    from loader import (
        TopologyLoader,
        TopologyValidationError,
        get_loader,
        canonicalize,
        _canonicalize_node,
        upstream,
        get_upstream_dependencies,
        downstream,
        blast_radius,
        get_blast_radius,
        hop_distance,
        calculate_hop_distance,
        get_topology,
        get_topology_graph,
    )

# Dynamic graph properties loaded from TOPOLOGY.md
def get_nodes() -> List[str]:
    return get_loader().nodes


def get_node_aliases() -> Dict[str, str]:
    return get_loader().alias_map


def get_directed_edges() -> List[tuple[str, str]]:
    return [(e.from_node, e.to_node) for e in get_loader().edges]


# Convenience module-level references (dynamic)
NODES = get_nodes()
NODE_ALIASES = get_node_aliases()
DIRECTED_EDGES = get_directed_edges()

__all__ = [
    "TopologyLoader",
    "TopologyValidationError",
    "get_loader",
    "canonicalize",
    "_canonicalize_node",
    "upstream",
    "get_upstream_dependencies",
    "downstream",
    "blast_radius",
    "get_blast_radius",
    "hop_distance",
    "calculate_hop_distance",
    "get_topology",
    "get_topology_graph",
    "NODES",
    "NODE_ALIASES",
    "DIRECTED_EDGES",
]
