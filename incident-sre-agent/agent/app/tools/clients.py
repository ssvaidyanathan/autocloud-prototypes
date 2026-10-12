"""GCP Client Factories using Application Default Credentials (ADC).

Includes fail-fast production assertions.
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Global client cache
_client_cache: Dict[str, Any] = {}


def is_local_mode() -> bool:
    """Check if the current runtime is configured for local offline development."""
    app_env = os.getenv("APP_ENV", "").strip().lower()
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    return app_env == "local" or env == "local"


def clear_client_cache() -> None:
    """Clear all cached client instances (primarily for testing)."""
    _client_cache.clear()


def _handle_factory_error(client_name: str, exc: Exception) -> None:
    """Handle client creation error: fail-fast in production, log in local mode."""
    if not is_local_mode():
        raise RuntimeError(
            f"GCP client '{client_name}' unavailable in production environment: {exc}"
        ) from exc
    logger.debug("Local mode: skipping initialization of real client %s (%s)", client_name, exc)


def get_logging_client(project_id: Optional[str] = None) -> Optional[Any]:
    """Factory for google.cloud.logging.Client."""
    if is_local_mode():
        return None

    resolved_project = (
        project_id
        or os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    if not resolved_project:
        try:
            from agent.app.config import get_settings

            resolved_project = get_settings().gcp_project
        except Exception:
            pass

    if not resolved_project:
        _handle_factory_error(
            "Cloud Logging Client",
            ValueError("No GCP project provided or configured for Cloud Logging Client"),
        )
        return None

    cache_key = f"logging_client_{resolved_project}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.logging

        client = google.cloud.logging.Client(project=resolved_project)  # type: ignore[no-untyped-call]
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Logging Client", exc)
        return None


def get_logging_service_client() -> Optional[Any]:
    """Factory for google.cloud.logging_v2.services.logging_service_v2.LoggingServiceV2Client."""
    if is_local_mode():
        return None

    cache_key = "logging_service_v2_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        from google.cloud.logging_v2.services.logging_service_v2 import LoggingServiceV2Client

        client = LoggingServiceV2Client()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Logging Service V2 Client", exc)
        return None


def get_monitoring_metric_client() -> Optional[Any]:
    """Factory for google.cloud.monitoring_v3.MetricServiceClient."""
    if is_local_mode():
        return None

    cache_key = "monitoring_metric_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.monitoring_v3

        client = google.cloud.monitoring_v3.MetricServiceClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Monitoring MetricServiceClient", exc)
        return None


def get_monitoring_alert_client() -> Optional[Any]:
    """Factory for google.cloud.monitoring_v3.AlertPolicyServiceClient."""
    if is_local_mode():
        return None

    cache_key = "monitoring_alert_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.monitoring_v3

        client = google.cloud.monitoring_v3.AlertPolicyServiceClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Monitoring AlertPolicyServiceClient", exc)
        return None


def get_cloud_run_services_client() -> Optional[Any]:
    """Factory for google.cloud.run_v2.ServicesClient."""
    if is_local_mode():
        return None

    cache_key = "cloud_run_services_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.run_v2

        client = google.cloud.run_v2.ServicesClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Run ServicesClient", exc)
        return None


def get_cloud_run_revisions_client() -> Optional[Any]:
    """Factory for google.cloud.run_v2.RevisionsClient."""
    if is_local_mode():
        return None

    cache_key = "cloud_run_revisions_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.run_v2

        client = google.cloud.run_v2.RevisionsClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Run RevisionsClient", exc)
        return None


def get_compute_instances_client() -> Optional[Any]:
    """Factory for google.cloud.compute_v1.InstancesClient."""
    if is_local_mode():
        return None

    cache_key = "compute_instances_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.compute_v1

        client = google.cloud.compute_v1.InstancesClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Compute Engine InstancesClient", exc)
        return None


def get_gke_cluster_client() -> Optional[Any]:
    """Factory for google.cloud.container_v1.ClusterManagerClient."""
    if is_local_mode():
        return None

    cache_key = "gke_cluster_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.container_v1

        client = google.cloud.container_v1.ClusterManagerClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("GKE ClusterManagerClient", exc)
        return None


def get_cloud_sql_admin_client() -> Optional[Any]:
    """Factory for Cloud SQL Admin API via googleapiclient.discovery."""
    if is_local_mode():
        return None

    cache_key = "cloud_sql_admin_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.auth
        import googleapiclient.discovery  # type: ignore[import-untyped]

        credentials, _ = google.auth.default()
        client = googleapiclient.discovery.build(
            "sqladmin",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud SQL Admin Client", exc)
        return None


def get_cloud_build_client() -> Optional[Any]:
    """Factory for google.cloud.devtools.cloudbuild_v1.CloudBuildClient."""
    if is_local_mode():
        return None

    cache_key = "cloud_build_client"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.cloud.devtools.cloudbuild_v1

        client = google.cloud.devtools.cloudbuild_v1.CloudBuildClient()
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Cloud Build Client", exc)
        return None


def get_genai_client(
    project_id: Optional[str] = None,
    location: Optional[str] = None,
) -> Optional[Any]:
    """Factory for google.genai.Client configured for Vertex AI."""
    if is_local_mode():
        return None

    resolved_project = (
        project_id
        or os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    resolved_location = location or os.getenv("VERTEX_LOCATION", "global")
    if not resolved_project:
        try:
            from agent.app.config import get_settings

            settings = get_settings()
            resolved_project = settings.gcp_project
            if not location:
                resolved_location = settings.vertex_location
        except Exception:
            pass

    if not resolved_project:
        _handle_factory_error(
            "Vertex AI Gemini Client",
            ValueError("No GCP project provided or configured for Vertex AI Gemini Client"),
        )
        return None

    cache_key = f"genai_client_{resolved_project}_{resolved_location}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    try:
        import google.genai

        client = google.genai.Client(
            vertexai=True,
            project=resolved_project,
            location=resolved_location,
        )
        _client_cache[cache_key] = client
        return client
    except Exception as exc:
        _handle_factory_error("Vertex AI Gemini Client", exc)
        return None
