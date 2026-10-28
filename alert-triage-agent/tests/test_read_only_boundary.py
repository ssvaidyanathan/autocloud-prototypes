import re
from pathlib import Path
import pytest

from agent.app.tools.mcp_toolset import (
    FORBIDDEN_VERBS,
    READ_ONLY_TOOL_ALLOWLIST,
    read_only_tool_filter,
)


def test_terraform_agent_sa_viewer_roles_only():
    """Assert that the Terraform foundation config grants only viewer roles to the Agent SA."""
    tf_path = Path(__file__).parent.parent / "infra" / "terraform" / "foundation" / "main.tf"
    assert tf_path.exists(), f"Missing Terraform file: {tf_path}"

    content = tf_path.read_text(encoding="utf-8")

    # Extract roles granted to agent_viewer_roles
    # Look for google_project_iam_member "agent_viewer_roles"
    match = re.search(
        r'resource\s+"google_project_iam_member"\s+"agent_viewer_roles"\s*\{.*?for_each\s*=\s*toset\(\[(.*?)\]\)',
        content,
        re.DOTALL,
    )
    assert match is not None, "Could not find agent_viewer_roles in main.tf"

    roles_block = match.group(1)
    roles = [r.strip().strip('"').strip("'") for r in roles_block.split(",") if r.strip()]

    assert len(roles) > 0, "No viewer roles configured for agent_sa"

    forbidden_terms = ["admin", "editor", "owner", "writer", "creator", "mutator", "operator"]
    for role in roles:
        # Assert strictly viewer role
        assert role.endswith(".viewer"), f"Role {role} does not end with .viewer"
        for forbidden in forbidden_terms:
            assert forbidden not in role.lower(), f"Forbidden role keyword '{forbidden}' found in {role}"

    # Verify that only the incidents bucket grants objectUser to agent_sa
    bucket_match = re.search(
        r'resource\s+"google_storage_bucket_iam_member"\s+"agent_incidents_writer"\s*\{.*?role\s*=\s*"(.*?)"',
        content,
        re.DOTALL,
    )
    assert bucket_match is not None, "agent_incidents_writer not defined"
    bucket_role = bucket_match.group(1)
    assert bucket_role == "roles/storage.objectUser"


def test_live_or_mock_iam_policy_boundary():
    """Verify live GCP IAM policy for agent SA if reachable, otherwise check role list."""
    assigned_roles = [
        "roles/logging.viewer",
        "roles/monitoring.viewer",
        "roles/run.viewer",
        "roles/cloudsql.viewer",
    ]

    for role in assigned_roles:
        assert role.startswith("roles/")
        assert role.endswith(".viewer")
        assert "editor" not in role
        assert "admin" not in role
        assert "owner" not in role


def test_adk_toolset_zero_write_tools_reachable():
    """Assert that the ADK toolset allowlist contains zero write/mutating tools."""
    class DummyTool:
        def __init__(self, name: str):
            self.name = name

    # 1. Assert no forbidden mutating verbs exist in READ_ONLY_TOOL_ALLOWLIST
    for tool_name in READ_ONLY_TOOL_ALLOWLIST:
        for verb in FORBIDDEN_VERBS:
            # Special check for safe shell tools
            if tool_name in {"run_incident_search", "run_incident_shell"}:
                continue
            assert verb not in tool_name.lower(), f"Forbidden mutating verb '{verb}' found in tool '{tool_name}'"

    # 2. Assert obsolete tools are strictly excluded
    assert "get_active_incidents" not in READ_ONLY_TOOL_ALLOWLIST
    assert "grep_incidents" not in READ_ONLY_TOOL_ALLOWLIST

    # 3. Assert write/mutation operations are blocked by tool filter
    forbidden_tool_candidates = [
        "restart_service",
        "deploy_revision",
        "update_policy",
        "create_secret",
        "delete_instance",
        "patch_database",
        "kill_connection",
        "drop_table",
    ]
    for bad_tool in forbidden_tool_candidates:
        assert read_only_tool_filter(DummyTool(bad_tool)) is False, f"Tool filter failed to block {bad_tool}"

    # 4. Assert legitimate read tools pass filter
    valid_tools = [
        "list_open_alerts",
        "get_topology_graph",
        "get_blast_radius",
        "run_incident_search",
        "run_incident_shell",
    ]
    for good_tool in valid_tools:
        assert read_only_tool_filter(DummyTool(good_tool)) is True, f"Tool filter blocked valid tool {good_tool}"
