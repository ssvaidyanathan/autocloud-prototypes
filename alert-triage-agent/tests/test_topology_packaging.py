"""Unit tests verifying packaging and runtime discovery of TOPOLOGY.md."""

from pathlib import Path
import subprocess
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_topology_md_packaged_in_agent_tree():
    """Assert TOPOLOGY.md is present in both agent/topology and root topology dirs."""
    root_topo = REPO_ROOT / "topology" / "TOPOLOGY.md"
    agent_topo = REPO_ROOT / "agent" / "topology" / "TOPOLOGY.md"

    assert root_topo.is_file(), f"TOPOLOGY.md missing from {root_topo}"
    assert agent_topo.is_file(), f"TOPOLOGY.md missing from {agent_topo}"
    assert root_topo.read_text(encoding="utf-8") == agent_topo.read_text(encoding="utf-8")


def test_agent_pyproject_declares_topology_package_data():
    """Assert agent/pyproject.toml declares package data for topology markdown files."""
    pyproject_path = REPO_ROOT / "agent" / "pyproject.toml"
    assert pyproject_path.exists()

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    pkg_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    assert "topology" in pkg_data, "tool.setuptools.package-data must declare 'topology'"
    assert "*.md" in pkg_data["topology"] or "TOPOLOGY.md" in pkg_data["topology"]


def test_packaged_topology_loads_at_runtime():
    """Verify that topology loader successfully resolves and parses the packaged file."""
    from topology.loader import find_topology_file, TopologyLoader

    resolved = find_topology_file()
    assert resolved.exists()
    assert resolved.name == "TOPOLOGY.md"

    loader = TopologyLoader(resolved)
    assert len(loader.components) == 5
    assert len(loader.edges) == 4
    assert "ata-orders-api" in loader.components
    assert "ata-inventory-api" in loader.components
    assert "ata-inventory-db" in loader.components


def test_container_layout_loads_packaged_topology():
    """Verify that running with PYTHONPATH=/app (agent dir) discovers and loads TOPOLOGY.md."""
    agent_dir = str(REPO_ROOT / "agent")
    cmd = [
        sys.executable,
        "-c",
        f"""
import sys
sys.path = ['{agent_dir}'] + [p for p in sys.path if 'site-packages' in p or 'lib/python' in p]

import topology
topo = topology.get_topology()
assert len(topo['components']) == 5
assert len(topo['edges']) == 4
assert 'ata-inventory-api' in topo['nodes']
print('PACKAGED TOPOLOGY LOADED IN CONTAINER LAYOUT')
""",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Failed container layout load: {proc.stderr}\n{proc.stdout}"
    assert "PACKAGED TOPOLOGY LOADED IN CONTAINER LAYOUT" in proc.stdout
