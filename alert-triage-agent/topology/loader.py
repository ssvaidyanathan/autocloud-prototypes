"""Topology loader and validator for Alert Triage Agent.

Loads and validates TOPOLOGY.md as the single source of truth for application
components, aliases, and dependencies.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set
from collections import deque


class TopologyValidationError(ValueError):
    """Raised when TOPOLOGY.md fails validation (cycles, duplicate IDs, unknown types, etc.)."""
    pass


# TOPOLOGY.md writes stack-specific names as {prefix}-<name>; the loader substitutes NAME_PREFIX.
PREFIX_PLACEHOLDER = "{prefix}"
# Same rule as the Terraform name_prefix validation.
NAME_PREFIX_PATTERN = r"[a-z][a-z0-9]{1,9}"
_LEFTOVER_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def resolve_name_prefix(name_prefix: Optional[str] = None) -> str:
    """NAME_PREFIX (argument, else the NAME_PREFIX env var), validated. Raises when unset/invalid."""
    value = name_prefix if name_prefix is not None else os.environ.get("NAME_PREFIX")
    if not value:
        raise TopologyValidationError(
            "NAME_PREFIX is not set: TOPOLOGY.md component names are {prefix}-relative and cannot be resolved"
        )
    if not re.fullmatch(NAME_PREFIX_PATTERN, value):
        raise TopologyValidationError(
            f"NAME_PREFIX {value!r} is invalid: it must match ^{NAME_PREFIX_PATTERN}$ (the Terraform name_prefix rule)"
        )
    return value


ALLOWED_TYPES = {
    "apigee-proxy",
    "apigee proxy",
    "cloud-run",
    "cloud run",
    "cloud-sql",
    "cloud sql",
}


@dataclass
class Component:
    id: str
    type: str
    aliases: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Dependency:
    from_node: str
    to_node: str
    relation: str = ""


def find_topology_file(path: Optional[str] = None) -> Path:
    """Resolve TOPOLOGY.md path from argument, TOPOLOGY_FILE env var, or standard locations."""
    if path:
        p = Path(path).resolve()
        if p.is_file():
            return p
        raise FileNotFoundError(f"Specified topology file '{path}' not found")

    env_path = os.environ.get("TOPOLOGY_FILE")
    if env_path:
        p = Path(env_path).resolve()
        if p.is_file():
            return p
        raise FileNotFoundError(f"TOPOLOGY_FILE environment path '{env_path}' not found")

    candidates = [
        Path(__file__).resolve().parent / "TOPOLOGY.md",
        Path(__file__).resolve().parent.parent / "topology" / "TOPOLOGY.md",
        Path(__file__).resolve().parent.parent.parent / "topology" / "TOPOLOGY.md",
        Path("/app/topology/TOPOLOGY.md"),
        Path("/app/app/TOPOLOGY.md"),
        Path.cwd() / "topology" / "TOPOLOGY.md",
        Path.cwd() / "alert-triage-agent" / "topology" / "TOPOLOGY.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError("Could not find TOPOLOGY.md in standard locations")


class TopologyLoader:
    """Parses, validates, and queries the application topology."""

    def __init__(self, file_path: Optional[str | Path] = None, name_prefix: Optional[str] = None):
        self.source_path = find_topology_file(str(file_path) if file_path else None)
        self.name_prefix = resolve_name_prefix(name_prefix)
        self.components: Dict[str, Component] = {}
        self.edges: List[Dependency] = []
        self.alias_map: Dict[str, str] = {}
        self._load_and_validate()

    def _parse_markdown_table(self, text: str) -> List[List[str]]:
        rows = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line.startswith("|") or not line.endswith("|"):
                continue
            cells = [c.strip() for c in line[1:-1].split("|")]
            # Check if this is a markdown separator row (e.g. |---|---|)
            if all(set(c).issubset({"-", ":"}) and len(c) > 0 for c in cells):
                continue
            rows.append(cells)
        return rows

    def _load_and_validate(self) -> None:
        content = self.source_path.read_text(encoding="utf-8").replace(PREFIX_PLACEHOLDER, self.name_prefix)

        # Split into sections based on ## headers
        sections: Dict[str, str] = {}
        current_section = None
        current_lines: List[str] = []

        for line in content.splitlines():
            if line.startswith("## "):
                if current_section is not None:
                    sections[current_section] = "\n".join(current_lines)
                current_section = line[3:].strip().lower()
                current_lines = []
            elif current_section is not None:
                current_lines.append(line)

        if current_section is not None:
            sections[current_section] = "\n".join(current_lines)

        components_text = sections.get("components", "")
        dependencies_text = sections.get("dependencies", "")

        for section_name, section_text in (("Components", components_text), ("Dependencies", dependencies_text)):
            leftover = _LEFTOVER_PLACEHOLDER.search(section_text)
            if leftover:
                raise TopologyValidationError(
                    f"Unsubstituted placeholder '{leftover.group(0)}' in the {section_name} section of "
                    f"{self.source_path} (only {PREFIX_PLACEHOLDER} is supported)"
                )

        if not components_text:
            raise TopologyValidationError(f"Missing '## Components' section in {self.source_path}")
        if not dependencies_text:
            raise TopologyValidationError(f"Missing '## Dependencies' section in {self.source_path}")

        # Parse Components table
        comp_rows = self._parse_markdown_table(components_text)
        if len(comp_rows) < 2:
            raise TopologyValidationError(f"No component entries found in {self.source_path}")

        comp_headers = [h.lower() for h in comp_rows[0]]
        # Find column indices
        id_idx = -1
        type_idx = -1
        alias_idx = -1
        notes_idx = -1

        for idx, h in enumerate(comp_headers):
            if h in ("component", "id"):
                id_idx = idx
            elif h in ("type",):
                type_idx = idx
            elif "also known as" in h or "alias" in h:
                alias_idx = idx
            elif "note" in h:
                notes_idx = idx

        if id_idx == -1:
            raise TopologyValidationError(f"Components table missing 'Component' or 'id' column in {self.source_path}")
        if type_idx == -1:
            raise TopologyValidationError(f"Components table missing 'Type' column in {self.source_path}")

        for row in comp_rows[1:]:
            if len(row) <= max(id_idx, type_idx):
                continue
            comp_id = row[id_idx].strip()
            if not comp_id:
                continue
            if comp_id in self.components:
                raise TopologyValidationError(f"Duplicate component ID '{comp_id}' in {self.source_path}")

            comp_type = row[type_idx].strip()
            # Normalize and check allowed types
            norm_type = comp_type.lower()
            if norm_type not in ALLOWED_TYPES:
                raise TopologyValidationError(
                    f"Unknown component type '{comp_type}' for component '{comp_id}' in {self.source_path}. "
                    f"Allowed types: {sorted(list(ALLOWED_TYPES))}"
                )

            aliases: List[str] = []
            if alias_idx != -1 and len(row) > alias_idx and row[alias_idx].strip():
                aliases = [a.strip() for a in row[alias_idx].split(",") if a.strip()]

            notes = row[notes_idx].strip() if notes_idx != -1 and len(row) > notes_idx else ""

            self.components[comp_id] = Component(
                id=comp_id,
                type=comp_type,
                aliases=aliases,
                notes=notes,
            )

        # Parse Dependencies table
        dep_rows = self._parse_markdown_table(dependencies_text)
        if len(dep_rows) < 2:
            raise TopologyValidationError(f"No dependency entries found in {self.source_path}")

        dep_headers = [h.lower() for h in dep_rows[0]]
        from_idx = -1
        to_idx = -1
        how_idx = -1

        for idx, h in enumerate(dep_headers):
            if h == "from":
                from_idx = idx
            elif h == "to":
                to_idx = idx
            elif h in ("how", "relation"):
                how_idx = idx

        if from_idx == -1 or to_idx == -1:
            raise TopologyValidationError(f"Dependencies table missing 'From' or 'To' column in {self.source_path}")

        for row in dep_rows[1:]:
            if len(row) <= max(from_idx, to_idx):
                continue
            from_node = row[from_idx].strip()
            to_node = row[to_idx].strip()
            if not from_node or not to_node:
                continue

            # Validate edges reference known IDs
            if from_node not in self.components:
                raise TopologyValidationError(
                    f"Dependency 'From' component '{from_node}' is not declared in Components table ({self.source_path})"
                )
            if to_node not in self.components:
                raise TopologyValidationError(
                    f"Dependency 'To' component '{to_node}' is not declared in Components table ({self.source_path})"
                )

            relation = row[how_idx].strip() if how_idx != -1 and len(row) > how_idx else ""
            self.edges.append(Dependency(from_node=from_node, to_node=to_node, relation=relation))

        # Validate no cycles in dependencies (From depends on To)
        self._validate_acyclic()

        # Build alias map
        for comp_id, comp in self.components.items():
            self.alias_map[comp_id] = comp_id
            for alias in comp.aliases:
                self.alias_map[alias] = comp_id

    def _validate_acyclic(self) -> None:
        """Assert no directed cycles exist in the dependency graph."""
        adj: Dict[str, List[str]] = {c: [] for c in self.components}
        for edge in self.edges:
            adj[edge.from_node].append(edge.to_node)

        visited: Dict[str, int] = {c: 0 for c in self.components}  # 0=unvisited, 1=visiting, 2=visited
        cycle_path: List[str] = []

        def dfs(node: str, path: List[str]) -> bool:
            visited[node] = 1
            path.append(node)
            for neighbor in adj.get(node, []):
                if visited[neighbor] == 1:
                    cycle_idx = path.index(neighbor)
                    cycle_path.extend(path[cycle_idx:] + [neighbor])
                    return True
                if visited[neighbor] == 0:
                    if dfs(neighbor, path):
                        return True
            path.pop()
            visited[node] = 2
            return False

        for comp in self.components:
            if visited[comp] == 0:
                if dfs(comp, []):
                    raise TopologyValidationError(
                        f"Directed cycle detected in topology dependencies: {' -> '.join(cycle_path)}"
                    )

    @property
    def nodes(self) -> List[str]:
        return list(self.components.keys())

    def canonicalize(self, node: Optional[str]) -> Optional[str]:
        """Normalize raw monitoring resource string, labels, or path to a canonical topology node."""
        if not node:
            return node
        node_str = str(node).strip()
        if node_str in self.components:
            return node_str
        if node_str in self.alias_map:
            return self.alias_map[node_str]

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
                if val in self.components:
                    return val
                if val in self.alias_map:
                    return self.alias_map[val]

        m = re.search(r"database_id\s*=\s*['\"]?([^,'\"\s}]+)", node_str)
        if m:
            db_id = m.group(1).strip().split(":")[-1]
            if db_id in self.components:
                return db_id
            if db_id in self.alias_map:
                return self.alias_map[db_id]

        # Path split
        cleaned = node_str.split("/")[-1]
        if cleaned in self.components:
            return cleaned
        if cleaned in self.alias_map:
            return self.alias_map[cleaned]

        return node_str

    def upstream(self, target_node: str) -> List[str]:
        """Returns all components that target_node depends on (callees).
        
        Traverses forward along caller -> callee directed edges.
        Returns dependencies ordered by topological distance (direct callees first).
        """
        node = self.canonicalize(target_node)
        adj: Dict[str, List[str]] = {n: [] for n in self.components}
        for edge in self.edges:
            adj[edge.from_node].append(edge.to_node)

        visited: Set[str] = set()
        ordered: List[str] = []
        queue = deque([node])

        while queue:
            curr = queue.popleft()
            for neighbor in adj.get(curr, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    ordered.append(neighbor)
                    queue.append(neighbor)

        return ordered

    def downstream(self, origin_node: str) -> List[str]:
        """Returns all components that depend on origin_node (callers).
        
        Traverses reverse caller -> callee edges (callee -> callers).
        Returns callers ordered by distance.
        """
        node = self.canonicalize(origin_node)
        rev_adj: Dict[str, List[str]] = {n: [] for n in self.components}
        for edge in self.edges:
            rev_adj[edge.to_node].append(edge.from_node)

        visited: Set[str] = set()
        ordered: List[str] = []
        queue = deque([node])

        while queue:
            curr = queue.popleft()
            for caller in rev_adj.get(curr, []):
                if caller not in visited:
                    visited.add(caller)
                    ordered.append(caller)
                    queue.append(caller)

        return ordered

    def blast_radius(self, origin_node: str) -> List[str]:
        """Returns all downstream consumers impacted if origin_node fails.
        
        Traverses reverse edges (callee -> callers).
        Returns sorted list of impacted components.
        """
        return sorted(self.downstream(origin_node))

    def dependency_paths(self, origin_node: str) -> List[List[str]]:
        """Returns every topology path from an impacted entry point to origin_node.

        Each path follows the caller -> callee edges of TOPOLOGY.md and ends at the origin,
        e.g. ['proxy-a', 'service-a', 'origin']. An entry point is a
        caller of the origin that has no callers itself. Returns [[origin]] when nothing
        depends on the origin, and [] when the origin is not a topology component.
        """
        node = self.canonicalize(origin_node)
        if node not in self.components:
            return []
        callers: Dict[str, List[str]] = {n: [] for n in self.components}
        for edge in self.edges:
            callers[edge.to_node].append(edge.from_node)

        paths: List[List[str]] = []

        def walk(curr: str, chain: List[str]) -> None:
            nexts = [c for c in callers.get(curr, []) if c not in chain]
            if not nexts:
                paths.append(list(reversed(chain)))
                return
            for caller in nexts:
                walk(caller, chain + [caller])

        walk(node, [node])
        return sorted(paths)

    def format_dependency_paths(self, origin_node: str) -> Optional[str]:
        """Renders dependency_paths as 'a -> b -> origin; c -> origin', or None when unknown."""
        paths = self.dependency_paths(origin_node)
        if not paths:
            return None
        return "; ".join(" -> ".join(p) for p in paths)

    def hop_distance(self, node_a: str, node_b: str) -> Optional[int]:
        """Calculates shortest undirected hop distance between node_a and node_b.
        
        Returns 0 if node_a == node_b, or None if nodes are disconnected or unknown.
        """
        canon_a = self.canonicalize(node_a)
        canon_b = self.canonicalize(node_b)
        if canon_a not in self.components or canon_b not in self.components:
            return None
        if canon_a == canon_b:
            return 0

        undirected: Dict[str, List[str]] = {n: [] for n in self.components}
        for edge in self.edges:
            undirected[edge.from_node].append(edge.to_node)
            undirected[edge.to_node].append(edge.from_node)

        visited: Set[str] = {canon_a}
        queue = deque([(canon_a, 0)])

        while queue:
            curr, dist = queue.popleft()
            if curr == canon_b:
                return dist
            for neighbor in undirected.get(curr, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, dist + 1))

        return None

    def get_topology(self, resource: Optional[str] = None) -> Dict[str, Any]:
        """Returns components/edges and the resource's upstream/downstream."""
        canon = self.canonicalize(resource) if resource else None
        return {
            "source_file": str(self.source_path),
            "components": [
                {"id": c.id, "type": c.type, "aliases": c.aliases, "notes": c.notes}
                for c in self.components.values()
            ],
            "nodes": list(self.nodes),
            "edges": [
                {"from": e.from_node, "to": e.to_node, "relation": e.relation}
                for e in self.edges
            ],
            "resource": canon,
            "upstream": self.upstream(canon) if canon else [],
            "downstream": self.downstream(canon) if canon else [],
        }

    def get_relevant_edges(self, origin: str, resource: Optional[str] = None) -> List[Dict[str, str]]:
        """Returns edges in the topology relevant to the origin and affected resource."""
        canon_origin = self.canonicalize(origin)
        canon_res = self.canonicalize(resource) if resource else None
        
        nodes_of_interest = {canon_origin}
        if canon_res:
            nodes_of_interest.add(canon_res)
        nodes_of_interest.update(self.upstream(canon_origin))
        nodes_of_interest.update(self.downstream(canon_origin))

        relevant = []
        for edge in self.edges:
            if edge.from_node in nodes_of_interest and edge.to_node in nodes_of_interest:
                relevant.append({"from": edge.from_node, "to": edge.to_node, "relation": edge.relation})
        return relevant


# Global singleton loader instance
_DEFAULT_LOADER: Optional[TopologyLoader] = None


def get_loader(
    file_path: Optional[str | Path] = None,
    force_reload: bool = False,
    name_prefix: Optional[str] = None,
) -> TopologyLoader:
    """Returns the singleton TopologyLoader, initializing or reloading if required.

    The singleton follows NAME_PREFIX: it is rebuilt when the prefix (argument or env) differs
    from the one it was loaded with.
    """
    global _DEFAULT_LOADER
    stale = _DEFAULT_LOADER is not None and _DEFAULT_LOADER.name_prefix != resolve_name_prefix(name_prefix)
    if _DEFAULT_LOADER is None or force_reload or stale or file_path is not None:
        loader = TopologyLoader(file_path, name_prefix=name_prefix)
        if file_path is None:
            _DEFAULT_LOADER = loader
        return loader
    return _DEFAULT_LOADER


# Module-level convenience functions using the default loader
def canonicalize(node: Optional[str]) -> Optional[str]:
    return get_loader().canonicalize(node)


def _canonicalize_node(node: Optional[str]) -> Optional[str]:
    return canonicalize(node)


def upstream(target_node: str) -> List[str]:
    return get_loader().upstream(target_node)


def get_upstream_dependencies(target_node: str) -> List[str]:
    return upstream(target_node)


def downstream(origin_node: str) -> List[str]:
    return get_loader().downstream(origin_node)


def blast_radius(origin_node: str) -> List[str]:
    return get_loader().blast_radius(origin_node)


def get_blast_radius(origin_node: str) -> List[str]:
    return blast_radius(origin_node)


def hop_distance(node_a: str, node_b: str) -> Optional[int]:
    return get_loader().hop_distance(node_a, node_b)


def calculate_hop_distance(node_a: str, node_b: str) -> Optional[int]:
    return hop_distance(node_a, node_b)


def get_topology(resource: Optional[str] = None) -> Dict[str, Any]:
    return get_loader().get_topology(resource)


def get_topology_graph() -> Dict[str, Any]:
    """Compatibility function returning graph nodes and edges."""
    loader = get_loader()
    return {
        "nodes": list(loader.nodes),
        "edges": [{"from": e.from_node, "to": e.to_node} for e in loader.edges],
    }


# Fail loudly on import if default TOPOLOGY.md is invalid or missing
_DEFAULT_LOADER = get_loader()
