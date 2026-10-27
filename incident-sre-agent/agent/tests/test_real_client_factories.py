"""Unit tests for real GCP client factories, caching, and ADC error propagation."""

from unittest.mock import MagicMock

import pytest
from agent.app.tools.clients import (
    clear_client_cache,
    get_cloud_build_client,
    get_cloud_run_revisions_client,
    get_cloud_run_services_client,
    get_cloud_sql_admin_client,
    get_compute_backend_services_client,
    get_compute_forwarding_rules_client,
    get_compute_instances_client,
    get_compute_target_pools_client,
    get_genai_client,
    get_gke_cluster_client,
    get_logging_client,
    get_logging_service_client,
    get_monitoring_alert_client,
    get_monitoring_metric_client,
    is_local_mode,
)
from google.auth.exceptions import DefaultCredentialsError


def _mock_no_credentials(*args: object, **kwargs: object) -> None:
    raise DefaultCredentialsError("Simulated missing ADC credentials")  # type: ignore[no-untyped-call]


def test_client_factories_return_none_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    clear_client_cache()

    assert is_local_mode() is True
    assert get_logging_client() is None
    assert get_logging_service_client() is None
    assert get_monitoring_metric_client() is None
    assert get_monitoring_alert_client() is None
    assert get_cloud_run_services_client() is None
    assert get_cloud_run_revisions_client() is None
    assert get_compute_instances_client() is None
    assert get_compute_forwarding_rules_client() is None
    assert get_compute_target_pools_client() is None
    assert get_compute_backend_services_client() is None
    assert get_gke_cluster_client() is None
    assert get_cloud_sql_admin_client() is None
    assert get_cloud_build_client() is None
    assert get_genai_client() is None


def test_client_factories_raise_in_production_when_credentials_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    clear_client_cache()

    monkeypatch.setattr("google.auth.default", _mock_no_credentials)

    # Logging client construction failure
    monkeypatch.setattr(
        "google.cloud.logging.Client",
        MagicMock(side_effect=DefaultCredentialsError("fail")),  # type: ignore[no-untyped-call]
    )
    with pytest.raises(RuntimeError, match="GCP client 'Cloud Logging Client' unavailable"):
        get_logging_client()

    # Monitoring client construction failure
    monkeypatch.setattr(
        "google.cloud.monitoring_v3.MetricServiceClient",
        MagicMock(side_effect=DefaultCredentialsError("fail")),  # type: ignore[no-untyped-call]
    )
    with pytest.raises(
        RuntimeError, match="GCP client 'Cloud Monitoring MetricServiceClient' unavailable"
    ):
        get_monitoring_metric_client()

    # Cloud SQL Admin API failure
    with pytest.raises(RuntimeError, match="GCP client 'Cloud SQL Admin Client' unavailable"):
        get_cloud_sql_admin_client()


def test_client_caching_and_clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    clear_client_cache()

    mock_instance = MagicMock()
    mock_cls = MagicMock(return_value=mock_instance)
    monkeypatch.setattr("google.cloud.run_v2.ServicesClient", mock_cls)

    c1 = get_cloud_run_services_client()
    c2 = get_cloud_run_services_client()

    # Must return the cached instance, only 1 constructor call
    assert c1 is c2
    assert c1 is mock_instance
    assert mock_cls.call_count == 1

    # Clear cache and verify re-instantiation
    clear_client_cache()
    c3 = get_cloud_run_services_client()
    assert c3 is mock_instance
    assert mock_cls.call_count == 2
