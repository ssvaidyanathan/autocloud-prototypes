import pytest
from agent.app.tools.registry import (
    ToolDefinition,
    ToolRegistry,
    default_registry,
    readonly_tool,
)


def test_tool_registry_registration() -> None:
    registry = ToolRegistry()

    @readonly_tool(name="test_tool", description="Test tool description", registry=registry)
    def sample_tool(arg1: str, count: int = 5) -> str:
        return f"{arg1}:{count}"

    assert "test_tool" in registry.get_tool_names()
    tool_def = registry.get_tool("test_tool")
    assert tool_def is not None
    assert tool_def.name == "test_tool"
    assert tool_def.description == "Test tool description"
    assert tool_def.is_mutation is False
    assert "arg1" in tool_def.parameters
    assert tool_def.parameters["arg1"]["required"] is True
    assert tool_def.parameters["count"]["required"] is False
    assert tool_def.parameters["count"]["default"] == 5

    res = tool_def.execute(arg1="hello", count=3)
    assert res == "hello:3"


def test_registry_blocks_mutation() -> None:
    registry = ToolRegistry()

    def mutating_func() -> None:
        pass

    with pytest.raises(ValueError, match="strictly prohibited"):
        registry.register(mutating_func, name="mutating_tool", is_mutation=True)


def test_default_registry_all_readonly() -> None:
    # Ensure tools are imported and registered
    import agent.app.tools  # noqa: F401

    tools = default_registry.list_tools()
    assert len(tools) >= 14

    for tool in tools:
        assert isinstance(tool, ToolDefinition)
        assert tool.is_mutation is False, f"Tool '{tool.name}' has is_mutation=True!"
        assert not tool.name.startswith("delete_")
        assert not tool.name.startswith("create_")
        assert not tool.name.startswith("update_")
        assert not tool.name.startswith("patch_")
        assert not tool.name.startswith("drop_")

    assert not default_registry.has_mutating_tools()
    default_registry.assert_all_readonly()


def test_default_registry_expected_tools() -> None:
    expected_tools = {
        "query_logs",
        "summarize_error_groups",
        "list_log_names",
        "query_time_series",
        "compare_to_baseline",
        "list_alert_policies",
        "list_open_alerts",
        "query_audit_logs",
        "list_cloud_run_revisions",
        "list_cloud_builds",
        "describe_cloud_run_service",
        "describe_gke_clusters",
        "describe_compute_instances",
        "describe_cloud_sql_instances",
    }
    registered = set(default_registry.get_tool_names())
    assert expected_tools.issubset(registered)
