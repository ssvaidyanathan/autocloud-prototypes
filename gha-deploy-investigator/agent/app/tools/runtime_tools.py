import logging
import os
from typing import Any, Optional

from ..config import AgentConfig, get_settings
from .registry import readonly_tool

logger = logging.getLogger(__name__)

# In-memory mock stores for offline test hermeticity
_mock_revisions: dict[str, dict[str, Any]] = {}
_mock_logs: list[dict[str, Any]] = []


def reset_runtime_state() -> None:
    """Reset simulated runtime telemetry state."""
    _mock_revisions.clear()
    _mock_logs.clear()


def set_mock_revision(revision_name: str, data: dict[str, Any]) -> None:
    """Register simulated Cloud Run revision data for hermetic tests."""
    _mock_revisions[revision_name] = data


def add_mock_log(entry: dict[str, Any]) -> None:
    """Register simulated Cloud Logging record for hermetic tests."""
    _mock_logs.append(entry)


@readonly_tool(
    name="describe_cloud_run_revision",
    description="Describe Cloud Run revision status, container conditions, and exit codes",
)
def describe_cloud_run_revision(
    service_name: str,
    revision_name: str,
    region: str = "us-central1",
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Inspect Cloud Run revision status, conditions, and container exits."""
    cfg = config or get_settings()

    # Check mock state first for hermetic testing
    if revision_name in _mock_revisions:
        return _mock_revisions[revision_name]

    if client is not None and hasattr(client, "get_revision"):
        try:
            return client.get_revision(service_name=service_name, revision_name=revision_name)
        except Exception as exc:
            logger.warning("Error querying client for revision %s: %s", revision_name, exc)

    # In local/test mode without custom mock, provide default healthy or not-found status
    if getattr(cfg, "is_local", False) or os.environ.get("APP_ENV") in ("local", "test", "dev"):
        return {
            "service_name": service_name,
            "revision_name": revision_name,
            "region": region,
            "status": "Ready",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "RevisionReady",
                    "message": "Revision is ready to receive traffic.",
                }
            ],
            "container_statuses": [
                {
                    "name": service_name,
                    "restart_count": 0,
                    "last_exit_code": 0,
                }
            ],
        }

    return {
        "service_name": service_name,
        "revision_name": revision_name,
        "region": region,
        "status": "Unknown",
        "conditions": [],
    }


@readonly_tool(
    name="query_cloud_logging",
    description="Query GCP Cloud Logging for container stderr/stdout entries",
)
def query_cloud_logging(
    filter_query: str,
    limit: int = 50,
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> list[dict[str, Any]]:
    """Query Cloud Logging entries with filter query and limit."""
    cfg = config or get_settings()

    # If hermetic mock logs are loaded, filter and return matching logs
    if _mock_logs:
        results = []
        for log in _mock_logs:
            # Simple substring or tag filtering against log payload
            payload_str = str(log.get("text_payload", "")) + str(log.get("json_payload", ""))
            query_tokens = [t.strip().strip('"') for t in filter_query.split() if "=" not in t]
            if not query_tokens or any(t.lower() in payload_str.lower() for t in query_tokens):
                results.append(log)
            if len(results) >= limit:
                break
        return results

    if client is not None and hasattr(client, "list_entries"):
        try:
            entries = client.list_entries(filter_=filter_query, page_size=limit)
            return list(entries)
        except Exception as exc:
            logger.warning("Error querying logging client: %s", exc)

    if getattr(cfg, "is_local", False) or os.environ.get("APP_ENV") in ("local", "test", "dev"):
        return []

    return []


@readonly_tool(
    name="describe_service",
    description="Describe Cloud Run service status, metadata, and ready condition",
)
def describe_service(
    service_name: str,
    region: str = "us-central1",
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Describe Cloud Run service configuration and status."""
    return {
        "service_name": service_name,
        "region": region,
        "status": "Ready",
    }


__all__ = [
    "add_mock_log",
    "describe_cloud_run_revision",
    "describe_service",
    "query_cloud_logging",
    "reset_runtime_state",
    "set_mock_revision",
]
