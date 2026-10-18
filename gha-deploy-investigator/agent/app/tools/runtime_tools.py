import logging
import os
import re
from typing import Any, Optional

from ..config import AgentConfig, get_settings
from .registry import default_query_cache, readonly_tool

logger = logging.getLogger(__name__)

# In-memory mock stores for offline test hermeticity
_mock_revisions: dict[str, dict[str, Any]] = {}
_mock_service_revisions: dict[str, list[dict[str, Any]]] = {}
_mock_logs: list[dict[str, Any]] = []


def reset_runtime_state() -> None:
    """Reset simulated runtime telemetry state."""
    _mock_revisions.clear()
    _mock_service_revisions.clear()
    _mock_logs.clear()
    default_query_cache.clear()


def set_mock_revision(revision_name: str, data: dict[str, Any]) -> None:
    """Register simulated Cloud Run revision data for hermetic tests."""
    _mock_revisions[revision_name] = data


def set_mock_service_revisions(service_name: str, revisions: list[dict[str, Any]]) -> None:
    """Register simulated Cloud Run revisions for a service."""
    _mock_service_revisions[service_name] = revisions


def add_mock_log(entry: dict[str, Any]) -> None:
    """Register simulated Cloud Logging record for hermetic tests."""
    _mock_logs.append(entry)


def _is_production(cfg: Optional[AgentConfig]) -> bool:
    """Determine if execution environment is production."""
    if cfg is None:
        cfg = get_settings()
    env = (getattr(cfg, "app_env", "") or getattr(cfg, "environment", "")).lower()
    return not getattr(cfg, "is_local", False) and env not in (
        "local",
        "dev",
        "development",
        "test",
        "testing",
    )


def build_cloud_logging_filter(
    filter_query: str = "",
    service_name: Optional[str] = None,
    revision_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    severity: Optional[str] = None,
) -> str:
    """Build or repair a valid Cloud Logging filter for Cloud Run container logs.

    Repairs invalid bare revision_name or service_name in raw filter strings
    (converting them to resource.labels.revision_name and resource.labels.service_name).
    If structured parameters are provided, constructs a canonical Cloud Logging filter.
    """
    raw = (filter_query or "").strip()
    if raw:
        # Repair invalid bare revision_name or service_name in raw filter
        repaired = raw
        repaired = re.sub(
            r"(?<!resource\.labels\.)\brevision_name\s*=\s*",
            "resource.labels.revision_name=",
            repaired,
        )
        repaired = re.sub(
            r"(?<!resource\.labels\.)\bservice_name\s*=\s*",
            "resource.labels.service_name=",
            repaired,
        )
        return repaired

    clauses: list[str] = ['resource.type="cloud_run_revision"']
    if service_name:
        clauses.append(f'resource.labels.service_name="{service_name}"')
    if revision_name:
        clauses.append(f'resource.labels.revision_name="{revision_name}"')
    if start_time:
        clauses.append(f'timestamp>="{start_time}"')
    if end_time:
        clauses.append(f'timestamp<="{end_time}"')
    if severity:
        clauses.append(f'severity>="{severity}"')

    return " ".join(clauses)


def _format_log_entries(raw_entries: Any) -> list[dict[str, Any]]:
    """Format raw google.cloud.logging LogEntry objects or dicts into standard dicts."""
    results: list[dict[str, Any]] = []
    for entry in raw_entries:
        if isinstance(entry, dict):
            results.append(entry)
            continue
        ts_val = getattr(entry, "timestamp", None)
        ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val or "")
        payload = getattr(entry, "payload", None)
        text_payload = (
            payload if isinstance(payload, str) else getattr(entry, "text_payload", None)
        )
        json_payload = (
            payload
            if isinstance(payload, (dict, list))
            else getattr(entry, "json_payload", None)
        )
        results.append(
            {
                "timestamp": ts_str,
                "severity": getattr(entry, "severity", "DEFAULT"),
                "text_payload": text_payload,
                "json_payload": json_payload,
                "log_name": getattr(entry, "log_name", ""),
                "resource": getattr(entry, "resource", {}),
            }
        )
    return results


def _extract_rev_from_filter(filter_str: str) -> Optional[str]:
    m = re.search(r'revision_name=["\']?([a-zA-Z0-9_-]+)["\']?', filter_str)
    return m.group(1) if m else None


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
            if _is_production(cfg):
                raise RuntimeError(
                    f"Error querying revision '{revision_name}' with injected client "
                    f"in production: {exc}"
                ) from exc
            logger.warning("Error querying client for revision %s: %s", revision_name, exc)

    if _is_production(cfg):
        try:
            from google.api_core.exceptions import NotFound
            from google.cloud import run_v2

            project_id = (
                cfg.gcp_project
                or os.getenv("GOOGLE_CLOUD_PROJECT")
                or os.getenv("GCP_PROJECT")
            )
            real_client = run_v2.RevisionsClient()
            rev_name = (
                f"projects/{project_id}/locations/{region}/services/{service_name}/revisions/{revision_name}"
            )
            try:
                rev = real_client.get_revision(name=rev_name)
                conditions = [
                    {
                        "type": c.type_,
                        "state": getattr(c.state, "name", str(c.state)),
                        "reason": getattr(c.reason, "name", str(c.reason)),
                        "message": c.message,
                    }
                    for c in rev.conditions
                ]
                containers = [
                    {
                        "name": c.name,
                        "image": c.image,
                        "resources": {
                            "limits": dict(c.resources.limits)
                            if hasattr(c.resources, "limits")
                            else {},
                        },
                    }
                    for c in rev.containers
                ]
                has_oom = any(
                    "137" in cond.message
                    or "signal 9" in cond.message
                    or "memory limit" in cond.message.lower()
                    for cond in rev.conditions
                )
                return {
                    "service_name": service_name,
                    "revision_name": revision_name,
                    "region": region,
                    "status": "Ready"
                    if any(
                        c.type_ == "Ready"
                        and getattr(c.state, "name", "") == "CONDITION_SUCCEEDED"
                        for c in rev.conditions
                    )
                    else "NotReady",
                    "conditions": conditions,
                    "containers": containers,
                    "container_statuses": [
                        {
                            "name": c.name,
                            "last_exit_code": 137 if has_oom else 0,
                        }
                        for c in rev.containers
                    ],
                }
            except NotFound:
                logger.info(
                    "Revision %s not found in Cloud Run (failed pre-revision)", revision_name
                )
                return {
                    "service_name": service_name,
                    "revision_name": revision_name,
                    "region": region,
                    "status": "NotFound",
                    "conditions": [],
                    "container_statuses": [],
                }
        except Exception as exc:
            logger.error(
                "Failed to describe Cloud Run revision '%s' in production: %s", revision_name, exc
            )
            raise RuntimeError(
                f"Cloud Run revision lookup failed in production for '{revision_name}': {exc}"
            ) from exc

    # In local/test mode without custom mock, provide default healthy status
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


@readonly_tool(
    name="query_cloud_logging",
    description=(
        "Query GCP Cloud Logging for Cloud Run container stderr/stdout entries. "
        "Supports structured parameters (service_name, revision_name, start_time, "
        "end_time, severity) or a raw filter_query string (which will be validated "
        "and automatically repaired if needed)."
    ),
)
def query_cloud_logging(
    filter_query: str = "",
    service_name: Optional[str] = None,
    revision_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 50,
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> list[dict[str, Any]]:
    """Query Cloud Logging entries with filter query and limit."""
    cfg = config or get_settings()
    effective_filter = build_cloud_logging_filter(
        filter_query=filter_query,
        service_name=service_name,
        revision_name=revision_name,
        start_time=start_time,
        end_time=end_time,
        severity=severity,
    )

    # If hermetic mock logs are loaded, filter and return matching logs
    if _mock_logs:
        results = []
        for log in _mock_logs:
            payload_str = str(log.get("text_payload", "")) + str(log.get("json_payload", ""))
            query_tokens = [t.strip().strip('"') for t in effective_filter.split() if "=" not in t]
            if not query_tokens or any(t.lower() in payload_str.lower() for t in query_tokens):
                results.append(log)
            if len(results) >= limit:
                break
        return results

    if client is not None and hasattr(client, "list_entries"):
        try:
            raw_entries = client.list_entries(filter_=effective_filter, page_size=limit)
            return _format_log_entries(raw_entries)
        except Exception as exc:
            if _is_production(cfg):
                raise RuntimeError(f"Cloud Logging query failed in production: {exc}") from exc
            logger.warning("Error querying logging client: %s", exc)
            return []

    if _is_production(cfg):
        try:
            from google.cloud import logging as gcp_logging

            project_id = (
                cfg.gcp_project
                or os.getenv("GOOGLE_CLOUD_PROJECT")
                or os.getenv("GCP_PROJECT")
            )
            real_client = gcp_logging.Client(project=project_id)
            raw_entries = real_client.list_entries(filter_=effective_filter, page_size=limit)
            entries = _format_log_entries(raw_entries)
            target_rev = revision_name or _extract_rev_from_filter(effective_filter)
            if target_rev and not entries:
                logger.warning(
                    "Cloud Logging returned empty log entries for revision '%s' with filter '%s'",
                    target_rev,
                    effective_filter,
                )
            return entries
        except Exception as exc:
            logger.error("Cloud Logging client construction or query failed in production: %s", exc)
            raise RuntimeError(f"Cloud Logging failed in production: {exc}") from exc

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
    cfg = config or get_settings()

    if client is not None and hasattr(client, "get_service"):
        try:
            return client.get_service(service_name=service_name, region=region)
        except Exception as exc:
            if _is_production(cfg):
                raise RuntimeError(
                    f"Error querying service '{service_name}' with injected client "
                    f"in production: {exc}"
                ) from exc
            logger.warning("Error querying client for service %s: %s", service_name, exc)

    if _is_production(cfg):
        try:
            from google.api_core.exceptions import NotFound
            from google.cloud import run_v2

            project_id = (
                cfg.gcp_project
                or os.getenv("GOOGLE_CLOUD_PROJECT")
                or os.getenv("GCP_PROJECT")
            )
            real_client = run_v2.ServicesClient()
            svc_name = f"projects/{project_id}/locations/{region}/services/{service_name}"
            try:
                svc = real_client.get_service(name=svc_name)
                return {
                    "service_name": service_name,
                    "region": region,
                    "status": "Ready",
                    "uri": getattr(svc, "uri", ""),
                    "latest_created_revision": getattr(
                        svc, "latest_created_revision", ""
                    ).split("/")[-1],
                    "latest_ready_revision": getattr(
                        svc, "latest_ready_revision", ""
                    ).split("/")[-1],
                    "conditions": [
                        {
                            "type": c.type_,
                            "state": getattr(c.state, "name", str(c.state)),
                            "message": c.message,
                        }
                        for c in getattr(svc, "conditions", [])
                    ],
                }
            except NotFound:
                logger.info("Service %s not found in Cloud Run", service_name)
                return {
                    "service_name": service_name,
                    "region": region,
                    "status": "NotFound",
                    "conditions": [],
                }
        except Exception as exc:
            logger.error(
                "Failed to describe Cloud Run service '%s' in production: %s",
                service_name,
                exc,
            )
            raise RuntimeError(
                f"Cloud Run service lookup failed in production for '{service_name}': {exc}"
            ) from exc

    return {
        "service_name": service_name,
        "region": region,
        "status": "Ready",
    }


@readonly_tool(
    name="list_service_revisions",
    description="List Cloud Run revisions for a service with creation timestamps and status",
)
def list_service_revisions(
    service_name: str,
    region: str = "us-central1",
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> list[dict[str, Any]]:
    """List Cloud Run revisions for a service."""
    cfg = config or get_settings()

    if service_name in _mock_service_revisions:
        return _mock_service_revisions[service_name]

    matching = [
        dict(rev, revision_name=rev_name)
        for rev_name, rev in _mock_revisions.items()
        if rev.get("service_name") == service_name or service_name in rev_name
    ]
    if matching:
        return matching

    if client is not None and hasattr(client, "list_revisions"):
        try:
            return list(client.list_revisions(service_name=service_name, region=region))
        except Exception as exc:
            if _is_production(cfg):
                raise RuntimeError(
                    f"Error listing revisions for '{service_name}' with injected client "
                    f"in production: {exc}"
                ) from exc
            logger.warning("Error querying client for revisions of %s: %s", service_name, exc)

    if _is_production(cfg):
        try:
            from google.cloud import run_v2

            project_id = (
                cfg.gcp_project
                or os.getenv("GOOGLE_CLOUD_PROJECT")
                or os.getenv("GCP_PROJECT")
            )
            real_client = run_v2.RevisionsClient()
            parent = f"projects/{project_id}/locations/{region}/services/{service_name}"
            raw_revs = list(real_client.list_revisions(parent=parent))
            results = []
            for r in raw_revs:
                rev_name = r.name.split("/")[-1] if hasattr(r, "name") else str(r)
                results.append(
                    {
                        "service_name": service_name,
                        "revision_name": rev_name,
                        "region": region,
                        "create_time": str(getattr(r, "create_time", "")),
                        "conditions": [
                            {
                                "type": c.type_,
                                "state": getattr(c.state, "name", str(c.state)),
                                "message": c.message,
                            }
                            for c in getattr(r, "conditions", [])
                        ],
                    }
                )
            return results
        except Exception as exc:
            logger.error(
                "Failed to list revisions for service '%s' in production: %s", service_name, exc
            )
            raise RuntimeError(
                f"Cloud Run list revisions failed in production for '{service_name}': {exc}"
            ) from exc

    return []


__all__ = [
    "add_mock_log",
    "build_cloud_logging_filter",
    "describe_cloud_run_revision",
    "describe_service",
    "list_service_revisions",
    "query_cloud_logging",
    "reset_runtime_state",
    "set_mock_revision",
    "set_mock_service_revisions",
]
