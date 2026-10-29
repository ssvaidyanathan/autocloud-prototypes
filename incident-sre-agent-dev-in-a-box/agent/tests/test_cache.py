"""Tests for In-Memory Query Cache, Strict Argument Validation, and Pinned Project ID."""

import asyncio
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from agent.app.tools.common import (
    build_cloud_logging_filter,
    build_cloud_monitoring_filter,
    clamp_lookback_minutes,
    sanitize_text_contains,
    validate_raw_filter,
    validate_service,
    validate_severity,
    validate_status_code_range,
)
from agent.app.tools.logs import query_logs, summarize_error_groups
from agent.app.tools.metrics import query_time_series
from agent.app.tools.registry import (
    QueryCache,
    ToolDefinition,
    get_pinned_project_id,
    readonly_tool,
)

# ============================================================================
# 1. QueryCache Mechanics & Methods (sync & async, inspect, clear)
# ============================================================================


def test_query_cache_key_generation() -> None:
    cache = QueryCache()
    args1 = {"service": "shop-api", "severity": "ERROR", "limit": 10}
    args2 = {"limit": 10, "severity": "ERROR", "service": "shop-api"}

    key1 = cache.compute_key("query_logs", args1)
    key2 = cache.compute_key("query_logs", args2)

    # Sorted JSON serialization ensures identical keys regardless of dict order
    assert key1 == key2
    assert len(key1) == 64  # sha256 hex string


def test_query_cache_excludes_client_kwarg() -> None:
    cache = QueryCache()
    client_a = object()
    client_b = object()

    key_a = cache.compute_key("query_logs", {"service": "shop-api", "client": client_a})
    key_b = cache.compute_key("query_logs", {"service": "shop-api", "client": client_b})

    # The client object is non-serializable and excluded from query identity
    assert key_a == key_b


def test_query_cache_sync_methods() -> None:
    cache = QueryCache()
    key = "test_key_sync"
    data = {"entries": [{"id": 1, "text": "hello"}]}

    assert cache.get_sync(key) is None
    cache.set_sync(key, data)

    cached = cache.get_sync(key)
    assert cached == data

    # Verify deepcopy isolation
    cached["entries"].append({"id": 2})
    cached_after = cache.get_sync(key)
    assert cached_after is not None
    assert len(cached_after["entries"]) == 1

    # Inspect
    info = cache.inspect()
    assert info["total_entries"] == 1
    assert key in info["keys"]

    # Clear sync
    cache.clear_sync()
    assert cache.get_sync(key) is None
    assert cache.inspect()["total_entries"] == 0


@pytest.mark.asyncio
async def test_query_cache_async_methods() -> None:
    cache = QueryCache()
    key = "test_key_async"
    data = {"result": 42}

    assert await cache.get(key) is None
    await cache.set(key, data)

    cached = await cache.get(key)
    assert cached == data

    # Deepcopy check
    assert cached is not data

    await cache.clear()
    assert await cache.get(key) is None


# ============================================================================
# 2. Cache Hits & Concurrent Async Execution Protection
# ============================================================================


def test_tool_definition_sync_cache_hit() -> None:
    cache = QueryCache()
    mock_func = MagicMock(return_value={"data": "fresh_data"})

    tool = ToolDefinition(
        name="test_tool",
        description="test tool description",
        func=mock_func,
        parameters={},
        cache=cache,
    )

    # First call - cache miss
    res1 = tool.execute(service="shop-api", limit=5)
    assert res1 == {"data": "fresh_data"}
    assert mock_func.call_count == 1

    # Second call with identical arguments - cache hit
    res2 = tool.execute(limit=5, service="shop-api")
    assert res2 == {"data": "fresh_data"}
    assert mock_func.call_count == 1

    # Call with different arguments - cache miss
    res3 = tool.execute(service="other-api", limit=5)
    assert res3 == {"data": "fresh_data"}
    assert mock_func.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_async_cache_access() -> None:
    cache = QueryCache()
    call_counter = 0

    async def slow_query(**kwargs: Any) -> Dict[str, Any]:
        nonlocal call_counter
        call_counter += 1
        await asyncio.sleep(0.05)
        return {"count": call_counter, "service": kwargs.get("service")}

    tool = ToolDefinition(
        name="slow_query",
        description="slow query",
        func=slow_query,
        parameters={},
        cache=cache,
    )

    # Run 10 concurrent calls with identical parameters
    results = await asyncio.gather(
        *(tool.execute_async(service="checkout-service") for _ in range(10))
    )

    assert len(results) == 10
    # All callers receive identical results
    assert all(r["service"] == "checkout-service" for r in results)
    # Cache inspected entries
    assert cache.inspect()["total_entries"] == 1


# ============================================================================
# 3. Strict Structured Argument Validation
# ============================================================================


def test_validate_service_success_and_rejection() -> None:
    assert validate_service("shop-api") == "shop-api"
    assert validate_service("inventory_svc-1") == "inventory_svc-1"
    assert validate_service(None) is None
    assert validate_service("   ") is None

    # Invalid characters / raw injection attempts
    with pytest.raises(ValueError, match="Invalid service name"):
        validate_service("shop-api; DROP TABLE")

    with pytest.raises(ValueError, match="Invalid service name"):
        validate_service('shop-api" OR "1"="1')

    with pytest.raises(ValueError, match="Invalid service name"):
        validate_service("shop-api\nseverity=ERROR")


def test_validate_severity_standard_gcp() -> None:
    for sev in (
        "DEBUG",
        "INFO",
        "NOTICE",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "ALERT",
        "EMERGENCY",
    ):
        assert validate_severity(sev) == sev
        assert validate_severity(sev.lower()) == sev

    assert validate_severity(None) is None

    with pytest.raises(ValueError, match="Invalid severity"):
        validate_severity("FATAL")

    with pytest.raises(ValueError, match="Invalid severity"):
        validate_severity("ERROR OR 1=1")


def test_validate_status_code_range() -> None:
    assert validate_status_code_range("500") == "500"
    assert validate_status_code_range("500-599") == "500-599"
    assert validate_status_code_range("5xx") == "500-599"
    assert validate_status_code_range("5XX") == "500-599"
    assert validate_status_code_range("4xx") == "400-499"
    assert validate_status_code_range("404") == "404"
    assert validate_status_code_range(None) is None

    with pytest.raises(ValueError, match="Expected 3-digit HTTP code"):
        validate_status_code_range("99")

    with pytest.raises(ValueError, match="Expected 3-digit HTTP code"):
        validate_status_code_range("600")

    with pytest.raises(ValueError, match="start code 599 > end code 500"):
        validate_status_code_range("599-500")

    with pytest.raises(ValueError, match="Expected 3-digit HTTP code"):
        validate_status_code_range("500; rm -rf /")


def test_sanitize_text_contains() -> None:
    assert sanitize_text_contains('Out of memory: "database connection"') == (
        'Out of memory: \\"database connection\\"'
    )
    assert sanitize_text_contains(None) is None

    with pytest.raises(ValueError, match="cannot contain newlines"):
        sanitize_text_contains("error\nseverity=DEBUG")


def test_clamp_lookback_minutes() -> None:
    assert clamp_lookback_minutes(15) == 15
    assert clamp_lookback_minutes(0) == 1
    assert clamp_lookback_minutes(-10) == 1
    assert clamp_lookback_minutes(60) == 60
    assert clamp_lookback_minutes(120) == 60
    assert clamp_lookback_minutes("30") == 30  # type: ignore[arg-type]
    assert clamp_lookback_minutes(None) is None

    with pytest.raises(ValueError, match="lookback_minutes must be an integer"):
        clamp_lookback_minutes("not-a-number")  # type: ignore[arg-type]


# ============================================================================
# 4. Filter Construction (Logging & Monitoring)
# ============================================================================


def test_build_cloud_logging_filter() -> None:
    # Full structured arguments
    filt = build_cloud_logging_filter(
        service="shop-api",
        severity="ERROR",
        status_code_range="500-599",
        text_contains="connection timeout",
    )
    assert "severity >= ERROR" in filt
    assert 'resource.labels.service_name = "shop-api"' in filt
    assert "httpRequest.status >= 500 AND httpRequest.status <= 599" in filt
    assert '"connection timeout"' in filt

    # Single status code
    single_code_filt = build_cloud_logging_filter(
        service="shop-api",
        status_code_range="503",
    )
    assert "httpRequest.status = 503" in single_code_filt


def test_build_cloud_monitoring_filter() -> None:
    filt = build_cloud_monitoring_filter(
        service="shop-api",
        metric_type="run.googleapis.com/request_count",
        status_code_range="500-599",
    )
    assert 'metric.type = "run.googleapis.com/request_count"' in filt
    assert 'resource.labels.service_name = "shop-api"' in filt
    assert 'metric.labels.response_code_class = "5xx"' in filt


# ============================================================================
# 5. Strictly Pinned Project ID Enforcement
# ============================================================================


def test_get_pinned_project_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "production-incident-project")

    # Resolves to environment variable
    assert get_pinned_project_id() == "production-incident-project"

    # Matching project ID passed explicitly is accepted
    assert get_pinned_project_id("production-incident-project") == "production-incident-project"

    # Attempting to override with an arbitrary project ID is strictly rejected
    with pytest.raises(ValueError, match="Project ID override rejected"):
        get_pinned_project_id("arbitrary-other-project")


def test_tools_reject_arbitrary_project_id_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")

    with pytest.raises(ValueError, match="Project ID override rejected"):
        query_logs(
            service="shop-api",
            project_id="foreign-project",
        )

    with pytest.raises(ValueError, match="Project ID override rejected"):
        query_time_series(
            service="shop-api",
            project_id="foreign-project",
        )

    with pytest.raises(ValueError, match="Project ID override rejected"):
        summarize_error_groups(
            project_id="foreign-project",
        )


# ============================================================================
# 6. Async Tool Caching & Thread-Safe Consistency
# ============================================================================


@pytest.mark.asyncio
async def test_readonly_tool_async_function_caching() -> None:
    call_count = 0

    @readonly_tool(name="sample_async_tool")
    async def sample_async_tool(service: str, count: int = 1, client: Any = None) -> Dict[str, Any]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return {"service": service, "count": count, "call_index": call_count}

    # First call: cache miss, executes function
    res1 = await sample_async_tool(service="frontend", count=5)
    assert res1 == {"service": "frontend", "count": 5, "call_index": 1}
    assert call_count == 1

    # Second call with identical arguments (reordered kwargs): cache hit
    res2 = await sample_async_tool(count=5, service="frontend")
    assert res2 == {"service": "frontend", "count": 5, "call_index": 1}
    assert call_count == 1

    # Deepcopy isolation: mutating returned dictionary does not mutate cached value
    res2["count"] = 99
    res3 = await sample_async_tool(service="frontend", count=5)
    assert res3["count"] == 5

    # Third call with different arguments: cache miss
    res4 = await sample_async_tool(service="backend", count=5)
    assert res4["service"] == "backend"
    assert res4["call_index"] == 2
    assert call_count == 2

    # Invocation via __tool_definition__.execute_async
    tool_def = getattr(sample_async_tool, "__tool_definition__")
    assert tool_def is not None
    res5 = await tool_def.execute_async(service="frontend", count=5)
    assert res5["call_index"] == 1
    assert call_count == 2

    # Invocation of async tool via synchronous execute() raises TypeError
    with pytest.raises(TypeError, match="is an async coroutine function"):
        tool_def.execute(service="frontend", count=5)

    # Hermetic mock parameter bypasses cache
    mock_obj = MagicMock()
    res6 = await sample_async_tool(service="frontend", count=5, client=mock_obj)
    assert res6["call_index"] == 3
    assert call_count == 3


def test_query_cache_sync_async_thread_lock_consistency() -> None:
    import threading

    cache = QueryCache()
    errors: list[str] = []

    def sync_worker(worker_id: int) -> None:
        try:
            for i in range(25):
                key = f"key_{worker_id}_{i}"
                cache.set_sync(key, {"id": i, "worker": worker_id})
                val = cache.get_sync(key)
                if val is None or val["id"] != i:
                    errors.append(f"Mismatch in worker {worker_id} at {i}")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=sync_worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert cache.inspect()["total_entries"] == 100


# ============================================================================
# 7. Raw Filter Validation & Injection Rejection
# ============================================================================


def test_validate_raw_filter() -> None:
    # Valid filter expressions
    assert validate_raw_filter("severity >= ERROR") == "severity >= ERROR"
    assert (
        validate_raw_filter('resource.labels.service_name = "shop-api"')
        == 'resource.labels.service_name = "shop-api"'
    )
    assert validate_raw_filter(None) is None
    assert validate_raw_filter("   ") is None

    # Rejection of newlines
    with pytest.raises(ValueError, match="cannot contain newlines"):
        validate_raw_filter("severity >= ERROR\nresource.type=gce")

    # Rejection of null bytes
    with pytest.raises(ValueError, match="cannot contain null bytes"):
        validate_raw_filter("severity >= ERROR\x00")

    # Rejection of semicolons
    with pytest.raises(ValueError, match="cannot contain semicolons"):
        validate_raw_filter("severity >= ERROR; DROP TABLE logs")

    # Rejection of command substitution
    with pytest.raises(ValueError, match="command substitution"):
        validate_raw_filter("severity >= ERROR`whoami`")

    with pytest.raises(ValueError, match="command substitution"):
        validate_raw_filter("severity >= ERROR$(whoami)")

    # Rejection of boolean/tautology injection
    with pytest.raises(ValueError, match="suspicious boolean injection"):
        validate_raw_filter('severity >= ERROR" OR "1"="1')

    with pytest.raises(ValueError, match="suspicious boolean injection"):
        validate_raw_filter("severity >= ERROR OR 1=1")

    # Rejection of unbalanced quotes
    with pytest.raises(ValueError, match="unbalanced double quotes"):
        validate_raw_filter('severity >= "ERROR')

    with pytest.raises(ValueError, match="unbalanced single quotes"):
        validate_raw_filter("severity >= 'ERROR")


def test_query_logs_rejects_filter_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")

    with pytest.raises(ValueError, match="cannot contain newlines"):
        query_logs(filter_str="severity >= ERROR\nresource.type=gce")

    with pytest.raises(ValueError, match="cannot contain semicolons"):
        query_logs(filter_str="severity >= ERROR; rm -rf /")

    with pytest.raises(ValueError, match="unbalanced double quotes"):
        query_logs(filter_str='severity >= "ERROR')

    with pytest.raises(ValueError, match="suspicious boolean injection"):
        query_logs(filter_str='severity >= ERROR" OR "1"="1')


def test_query_logs_rejects_conflicting_filter_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")

    # Conflict between service and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_logs(
            service="shop-api",
            filter_str="severity >= ERROR",
        )

    # Conflict between severity and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_logs(
            severity="ERROR",
            filter_str='resource.labels.service_name = "shop-api"',
        )

    # Conflict between status_code_range and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_logs(
            status_code_range="500-599",
            filter_str="severity >= ERROR",
        )

    # Conflict between text_contains and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_logs(
            text_contains="timeout",
            filter_str="severity >= ERROR",
        )


def test_query_time_series_rejects_conflicting_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")

    # Conflict between service and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_time_series(
            service="shop-api",
            filter_str='metric.type = "run.googleapis.com/request_count"',
        )

    # Conflict between status_code_range and raw filter_str
    with pytest.raises(ValueError, match="Conflicting filter specification"):
        query_time_series(
            status_code_range="500-599",
            filter_str='metric.type = "run.googleapis.com/request_count"',
        )


def test_query_time_series_rejects_filter_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")

    with pytest.raises(ValueError, match="cannot contain newlines"):
        query_time_series(filter_str='metric.type = "run"\nresource.type=gce')

    with pytest.raises(ValueError, match="cannot contain semicolons"):
        query_time_series(filter_str='metric.type = "run"; DROP TABLE')
