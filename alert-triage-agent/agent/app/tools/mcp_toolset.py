"""MCP Toolset filtering and read-only guardrails for Alert Triage Agent."""

from typing import Any, Set

READ_ONLY_TOOL_ALLOWLIST: Set[str] = {
    # Custom ata-tools FastMCP server tools
    "list_open_alerts",
    "get_topology_graph",
    "get_topology",
    "get_blast_radius",
    "get_apigee_metrics",
    "get_apigee_logs",
    "get_proxy_bundle",
    "get_change_timeline",
    # Constrained incident investigation shell tool & alias
    "run_incident_search",
    "run_incident_shell",
    # Google-managed read-only observability tools
    "list_log_entries",
    "list_log_names",
    "list_time_series",
    "list_alert_policies",
    "list_metric_descriptors",
    "get_service",
    "list_revisions",
    "get_database_instance",
}

# Explicitly excluded obsolete tools to ensure they never pass the allowlist
OBSOLETE_TOOLS: Set[str] = {
    "get_active_incidents",
    "grep_incidents",
}

# Mutating verbs forbidden in any tool name
FORBIDDEN_VERBS: Set[str] = {
    "create",
    "update",
    "delete",
    "set",
    "patch",
    "restart",
    "deploy",
    "drop",
    "kill",
    "mutate",
    "write",
    "modify",
    "insert",
}


def read_only_tool_filter(tool: Any) -> bool:
    """Filter asserting that tool is strictly within READ_ONLY_TOOL_ALLOWLIST.
    
    Accepts tool instance, tool name string, or callable with a .name attribute.
    """
    if hasattr(tool, "name"):
        name = tool.name
    elif isinstance(tool, str):
        name = tool
    elif callable(tool) and hasattr(tool, "__name__"):
        name = tool.__name__
    else:
        name = str(tool)

    if name in OBSOLETE_TOOLS:
        return False

    name_lower = name.lower()
    for verb in FORBIDDEN_VERBS:
        if verb in name_lower and name not in {"run_incident_search", "run_incident_shell"}:
            return False

    return name in READ_ONLY_TOOL_ALLOWLIST
