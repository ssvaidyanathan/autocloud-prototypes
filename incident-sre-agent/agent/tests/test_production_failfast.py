"""Tests for production fail-fast assertions.

APP_ENV != 'local' must raise, never return empty data.
"""

from datetime import datetime, timezone

import pytest
from agent.app.dispatcher import Dispatcher
from agent.app.tools.changes import (
    list_cloud_builds,
    list_cloud_run_revisions,
    query_audit_logs,
)
from agent.app.tools.clients import clear_client_cache
from agent.app.tools.logs import (
    list_log_names,
    query_logs,
    summarize_error_groups,
)
from agent.app.tools.metrics import (
    compare_to_baseline,
    list_alert_policies,
    list_open_alerts,
    query_time_series,
)
from agent.app.tools.resources import (
    describe_cloud_run_service,
    describe_cloud_sql_instances,
    describe_compute_instances,
    describe_gke_clusters,
)
from google.auth.exceptions import DefaultCredentialsError


def _mock_no_credentials(*args: object, **kwargs: object) -> None:
    raise DefaultCredentialsError("Mock ADC credentials missing")  # type: ignore[no-untyped-call]


def test_production_dispatcher_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    clear_client_cache()
    monkeypatch.setattr("google.auth.default", _mock_no_credentials)

    with pytest.raises(RuntimeError, match="Dispatcher initialization failed in production"):
        Dispatcher()


def test_production_tools_fail_fast_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    clear_client_cache()
    monkeypatch.setattr("google.auth.default", _mock_no_credentials)

    now = datetime.now(timezone.utc)

    # 1. Logs tools fail fast
    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        query_logs(filter_str="severity >= ERROR", resource_names=["projects/test"], client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        summarize_error_groups(project_id="test", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        list_log_names(project_id="test", client=None)

    # 2. Metrics tools fail fast
    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        query_time_series(
            filter_str="metric.type=test",
            project_id="test",
            start_time=now,
            end_time=now,
            client=None,
        )

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        compare_to_baseline(
            filter_str="metric.type=test",
            project_id="test",
            current_start=now,
            current_end=now,
            baseline_start=now,
            baseline_end=now,
            client=None,
        )

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        list_alert_policies(project_id="test", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        list_open_alerts(project_id="test", client=None)

    # 3. Changes tools fail fast
    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        query_audit_logs(project_id="test", start_time=now, end_time=now, client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        list_cloud_run_revisions(project_id="test", service_name="shop-api", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        list_cloud_builds(project_id="test", client=None)

    # 4. Resources tools fail fast
    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        describe_cloud_run_service(project_id="test", service_name="shop-api", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        describe_gke_clusters(project_id="test", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        describe_compute_instances(project_id="test", client=None)

    with pytest.raises(RuntimeError, match="unavailable in production environment"):
        describe_cloud_sql_instances(project_id="test", client=None)


def test_local_mode_allows_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    clear_client_cache()

    now = datetime.now(timezone.utc)

    # In local mode, missing clients do not raise RuntimeError; return offline stubs/empty results
    res_logs = query_logs(
        filter_str="severity >= ERROR", resource_names=["projects/test"], client=None
    )
    assert res_logs["entries"] == []
    assert res_logs["total_returned"] == 0

    res_metrics = query_time_series(
        filter_str="metric.type=test", project_id="test", start_time=now, end_time=now, client=None
    )
    assert res_metrics["points"] == []
    assert res_metrics["total_points"] == 0

    res_audit = query_audit_logs(project_id="test", start_time=now, end_time=now, client=None)
    assert res_audit == []

    res_cr = describe_cloud_run_service(project_id="test", service_name="shop-api", client=None)
    assert res_cr["service_name"] == "shop-api"
    assert res_cr["env_var_names"] == []

    res_gke = describe_gke_clusters(project_id="test", client=None)
    assert res_gke == []

    res_compute = describe_compute_instances(project_id="test", client=None)
    assert res_compute == []

    res_sql = describe_cloud_sql_instances(project_id="test", client=None)
    assert res_sql == []
