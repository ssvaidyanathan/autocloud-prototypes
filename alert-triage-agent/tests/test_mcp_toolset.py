"""Unit tests asserting MCP toolset read-only boundary and obsolete tool exclusion."""

import asyncio
import pytest

from unittest.mock import patch

from agent.app.tools.mcp_toolset import (
    READ_ONLY_TOOL_ALLOWLIST,
    OBSOLETE_TOOLS,
    read_only_tool_filter,
)
from tools.app.server import mcp, get_apigee_metrics


@pytest.mark.asyncio
async def test_mcp_registered_tools_allowlist():
    """Assert all tools registered on ata-tools FastMCP server match READ_ONLY_TOOL_ALLOWLIST."""
    registered_tools = await mcp.list_tools()
    tool_names = {t.name for t in registered_tools}

    # Expected ata-tools custom tools
    expected_ata_tools = {
        "list_open_alerts",
        "get_topology_graph",
        "get_blast_radius",
        "get_apigee_metrics",
        "get_apigee_logs",
        "get_proxy_bundle",
        "get_change_timeline",
    }

    # 1. Assert all 7 tools are registered
    assert expected_ata_tools.issubset(tool_names), f"Missing registered tools: {expected_ata_tools - tool_names}"

    # 2. Assert every registered tool is in READ_ONLY_TOOL_ALLOWLIST
    for tool in registered_tools:
        assert tool.name in READ_ONLY_TOOL_ALLOWLIST, f"Tool {tool.name} is not in READ_ONLY_TOOL_ALLOWLIST"
        assert read_only_tool_filter(tool) is True

    # 3. Assert zero mutation verbs in any registered tool name
    mutating_verbs = ["create", "update", "delete", "set", "patch", "restart", "deploy", "write", "drop", "kill"]
    for name in tool_names:
        for verb in mutating_verbs:
            assert verb not in name.lower(), f"Mutating verb '{verb}' found in tool name '{name}'"


def test_mandatory_allowlist_entries():
    """Assert READ_ONLY_TOOL_ALLOWLIST includes all mandatory tools specified in Stage 4 brief."""
    mandatory = [
        "list_open_alerts",
        "get_topology_graph",
        "get_blast_radius",
        "get_apigee_metrics",
        "get_apigee_logs",
        "get_proxy_bundle",
        "get_change_timeline",
        "run_incident_search",
    ]
    for tool_name in mandatory:
        assert tool_name in READ_ONLY_TOOL_ALLOWLIST, f"Mandatory tool {tool_name} missing from allowlist"


def test_obsolete_tools_strictly_excluded():
    """Assert obsolete tools (get_active_incidents, grep_incidents) are excluded."""
    assert "get_active_incidents" not in READ_ONLY_TOOL_ALLOWLIST
    assert "grep_incidents" not in READ_ONLY_TOOL_ALLOWLIST

    assert "get_active_incidents" in OBSOLETE_TOOLS
    assert "grep_incidents" in OBSOLETE_TOOLS

    assert read_only_tool_filter("get_active_incidents") is False
    assert read_only_tool_filter("grep_incidents") is False


def test_mutating_actions_blocked():
    """Assert arbitrary mutation actions fail the tool filter."""
    disallowed = [
        "create_service",
        "update_traffic",
        "delete_revision",
        "restart_instance",
        "patch_database",
    ]
    for action in disallowed:
        assert read_only_tool_filter(action) is False


def test_get_apigee_metrics_distribution_metric_parsing():
    """Assert get_apigee_metrics extracts mean and count for DISTRIBUTION metrics (target/latencies)."""
    from google.cloud import monitoring_v3

    ts = monitoring_v3.TimeSeries()
    ts.metric.labels["proxy_name"] = "ata-orders-v1"
    ts.resource.labels["project_id"] = "cloud-assist-fde-2"

    pt = monitoring_v3.Point()
    pt.interval.end_time = {"seconds": 1700000000}
    pt.value.distribution_value.mean = 142.5
    pt.value.distribution_value.count = 58
    ts.points.append(pt)

    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_time_series.return_value = [ts]

        result = get_apigee_metrics(
            proxy_name="ata-orders-v1",
            metric_type="target/latencies",
            window_minutes=15,
            project_id="cloud-assist-fde-2",
        )

        assert "error" not in result
        assert result["proxy_name"] == "ata-orders-v1"
        assert result["metric_type"] == "apigee.googleapis.com/target/latencies"
        assert result["total_series"] == 1
        assert len(result["series"]) == 1
        points = result["series"][0]["points"]
        assert len(points) == 1
        assert points[0]["value"] == {"mean": 142.5, "count": 58}


def test_get_apigee_metrics_scalar_int64_zero_handling():
    """Assert get_apigee_metrics explicitly preserves scalar int64_value == 0 without falling through to double."""
    from google.cloud import monitoring_v3

    ts = monitoring_v3.TimeSeries()
    ts.metric.labels["proxy_name"] = "ata-orders-v1"

    pt1 = monitoring_v3.Point()
    pt1.interval.end_time = {"seconds": 1700000000}
    pt1.value.int64_value = 0

    pt2 = monitoring_v3.Point()
    pt2.interval.end_time = {"seconds": 1700000060}
    pt2.value.double_value = 42.5

    ts.points.extend([pt1, pt2])

    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.list_time_series.return_value = [ts]

        result = get_apigee_metrics(
            proxy_name="ata-orders-v1",
            metric_type="proxy/request_count",
            window_minutes=10,
            project_id="cloud-assist-fde-2",
        )

        assert "error" not in result
        points = result["series"][0]["points"]
        assert len(points) == 2
        # Check point 1 is integer 0, not float 0.0
        assert points[0]["value"] == 0
        assert isinstance(points[0]["value"], int)
        assert not isinstance(points[0]["value"], bool)
        # Check point 2 is float 42.5
        assert points[1]["value"] == 42.5
        assert isinstance(points[1]["value"], float)

