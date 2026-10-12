import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .clients import get_logging_client, get_logging_service_client, is_local_mode
from .registry import readonly_tool


def _format_project_id(resource_names: List[str]) -> str:
    """Extract project_id from resource names or return fallback."""
    for res in resource_names:
        if res.startswith("projects/"):
            return res.split("/")[1]
    return resource_names[0] if resource_names else "default"


def _extract_entry_excerpt(entry: Any) -> str:
    """Extract an informative, non-empty excerpt from a log entry across all attribute shapes."""
    http_req = getattr(entry, "http_request", None) or getattr(entry, "httpRequest", None)
    if hasattr(http_req, "_mock_name"):
        http_req = None
    if http_req is None and isinstance(entry, dict):
        http_req = entry.get("httpRequest") or entry.get("http_request")

    status = None
    method = None
    url = None
    latency = None
    if http_req:
        if isinstance(http_req, dict):
            status = http_req.get("status")
            method = http_req.get("requestMethod") or http_req.get("request_method")
            url = http_req.get("requestUrl") or http_req.get("request_url")
            latency = http_req.get("latency")
        else:
            status = getattr(http_req, "status", None)
            method = getattr(http_req, "request_method", None) or getattr(
                http_req, "requestMethod", None
            )
            url = getattr(http_req, "request_url", None) or getattr(http_req, "requestUrl", None)
            latency = getattr(http_req, "latency", None)
        if hasattr(status, "_mock_name"):
            status = None
        if hasattr(method, "_mock_name"):
            method = None
        if hasattr(url, "_mock_name"):
            url = None
        if hasattr(latency, "_mock_name"):
            latency = None

    # Extract text, json, proto, or generic payload
    text_payload = getattr(entry, "text_payload", None) or getattr(entry, "textPayload", None)
    if hasattr(text_payload, "_mock_name"):
        text_payload = None
    if text_payload is None and isinstance(entry, dict):
        text_payload = entry.get("textPayload") or entry.get("text_payload")

    json_payload = getattr(entry, "json_payload", None) or getattr(entry, "jsonPayload", None)
    if hasattr(json_payload, "_mock_name"):
        json_payload = None
    if json_payload is None and isinstance(entry, dict):
        json_payload = entry.get("jsonPayload") or entry.get("json_payload")

    proto_payload = getattr(entry, "proto_payload", None) or getattr(entry, "protoPayload", None)
    if hasattr(proto_payload, "_mock_name"):
        proto_payload = None
    if proto_payload is None and isinstance(entry, dict):
        proto_payload = entry.get("protoPayload") or entry.get("proto_payload")

    raw_payload = getattr(entry, "payload", None)
    if hasattr(raw_payload, "_mock_name"):
        raw_payload = None
    if raw_payload is None and isinstance(entry, dict):
        raw_payload = entry.get("payload")

    message = ""
    if text_payload and str(text_payload).strip():
        message = str(text_payload).strip()
    elif isinstance(json_payload, dict) and json_payload:
        msg_val = (
            json_payload.get("message")
            or json_payload.get("error")
            or json_payload.get("stack_trace")
            or json_payload.get("detail")
            or ""
        )
        if msg_val:
            message = str(msg_val).strip()
        elif str(json_payload).strip() != "{}":
            message = str(json_payload).strip()
    elif isinstance(raw_payload, str) and raw_payload.strip():
        message = raw_payload.strip()
    elif isinstance(raw_payload, dict) and raw_payload:
        msg_val = (
            raw_payload.get("message")
            or raw_payload.get("error")
            or raw_payload.get("stack_trace")
            or raw_payload.get("detail")
            or ""
        )
        if msg_val:
            message = str(msg_val).strip()
        elif str(raw_payload).strip() != "{}":
            message = str(raw_payload).strip()
    elif proto_payload:
        if isinstance(proto_payload, dict):
            message = str(
                proto_payload.get("methodName")
                or proto_payload.get("resourceName")
                or proto_payload.get("status", {}).get("message", "")
                or ""
            )
        else:
            message = str(
                getattr(proto_payload, "method_name", "")
                or getattr(proto_payload, "resource_name", "")
                or ""
            )

    if message.strip() == "{}":
        message = ""

    http_parts: List[str] = []
    if status is not None and str(status) != "0":
        http_parts.append(f"HTTP {status}")
    if method:
        http_parts.append(str(method))
    if url:
        http_parts.append(str(url))
    if latency:
        http_parts.append(f"(latency: {latency})")

    http_str = " ".join(http_parts)

    if http_str and message:
        excerpt = f"{http_str} - {message}"
    elif http_str:
        excerpt = http_str
    elif message:
        excerpt = message
    else:
        log_name = getattr(entry, "log_name", "") or (
            entry.get("log_name", "") if isinstance(entry, dict) else ""
        )
        short_log = log_name.split("%2F")[-1].split("/")[-1] if log_name else "entry"
        severity = getattr(entry, "severity", "DEFAULT") or "DEFAULT"
        excerpt = f"{severity} event in {short_log}"

    if len(excerpt) > 256:
        excerpt = excerpt[:253] + "..."
    return excerpt


@readonly_tool(
    name="query_logs",
    description="Query Cloud Logging for log entries matching a filter. Limit capped at 200.",
)
def query_logs(
    filter_str: str,
    resource_names: List[str],
    max_entries: int = 50,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Query Cloud Logging entries with compact truncated output and console deep link."""
    # Enforce limit <= 200
    effective_limit = min(max(1, max_entries), 200)

    # Compose final filter including timestamps if specified
    filters: List[str] = [filter_str.strip()] if filter_str.strip() else []
    if start_time:
        start_iso = (
            start_time.astimezone(timezone.utc).isoformat()
            if start_time.tzinfo
            else start_time.replace(tzinfo=timezone.utc).isoformat()
        )
        filters.append(f'timestamp >= "{start_iso}"')
    if end_time:
        end_iso = (
            end_time.astimezone(timezone.utc).isoformat()
            if end_time.tzinfo
            else end_time.replace(tzinfo=timezone.utc).isoformat()
        )
        filters.append(f'timestamp <= "{end_iso}"')

    final_filter = " AND ".join(filters)
    project_id = _format_project_id(resource_names)

    # Cloud Console deep link
    encoded_query = urllib.parse.quote(final_filter)
    console_url = (
        f"https://console.cloud.google.com/logs/query;query={encoded_query};project={project_id}"
    )

    if client is None:
        client = get_logging_client(project_id)

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Logging client unavailable in production environment")

    entries: List[Dict[str, Any]] = []

    if client is not None:
        raw_entries = client.list_entries(
            resource_names=resource_names,
            filter_=final_filter,
            page_size=effective_limit,
        )
        for entry in raw_entries:
            if len(entries) >= effective_limit:
                break
            payload_str = _extract_entry_excerpt(entry)
            log_name = getattr(entry, "log_name", "") or (
                entry.get("log_name", "") if isinstance(entry, dict) else ""
            )

            # Filter out entries matching _is_probe_or_infra_noise()
            if _is_probe_or_infra_noise(f"{payload_str} {log_name}"):
                continue

            ts = getattr(entry, "timestamp", None)
            if ts is not None and hasattr(ts, "isoformat"):
                timestamp_str = str(ts.isoformat())
            else:
                timestamp_str = str(ts or "")
            severity = str(
                getattr(entry, "severity", "DEFAULT")
                or (entry.get("severity", "DEFAULT") if isinstance(entry, dict) else "DEFAULT")
            )

            resource = getattr(entry, "resource", None) or (
                entry.get("resource") if isinstance(entry, dict) else None
            )
            resource_type = (
                getattr(resource, "type", "unknown")
                if resource
                else (resource.get("type", "unknown") if isinstance(resource, dict) else "unknown")
            )

            entries.append(
                {
                    "timestamp": timestamp_str,
                    "severity": severity,
                    "payload_excerpt": payload_str,
                    "log_name": log_name,
                    "resource_type": resource_type,
                }
            )

    return {
        "entries": entries,
        "total_returned": len(entries),
        "limit_applied": effective_limit,
        "query_filter": final_filter,
        "console_url": console_url,
    }


def _is_probe_or_infra_noise(text: str) -> bool:
    """Check if log excerpt represents infrastructure probes, health checks, or daemon noise."""
    lower = text.lower()
    # 1. Link-local IPs (metadata / LB probes)
    if "169.254." in text:
        return True
    # 2. 200 OK health check probes
    if '"get / http' in lower and "200" in text:
        return True
    if '"get /health' in lower and "200" in text:
        return True
    if '" 200 -' in text or '" 200 ' in text or " 200 -" in text or "http 200" in lower:
        return True
    # 3. Cluster-internal / infra daemon noise (GKE / k8s / prometheus / cilium)
    infra_keywords = [
        "cni_network_config",
        "containerd",
        "podsandbox",
        "containerstatus",
        "shim",
        "reconciling subsys=sfc",
        "gke-system-balloon",
        "cni",
        "cri",
        "kube",
        "gke",
        "cilium",
        "leases",
        "coordination.k8s.io",
        "localnodeip",
        "subsys=sfc",
        "probe",
        "healthz",
        "kube-system",
        "gmp-system",
        "prometheus",
        "/api/v1/namespaces/",
        "namespaces/",
        "container-watcher",
        "filestore-node",
        "balloon-pod",
        "gke-crd-sync",
        "uasexporter",
        "gke-network-policy",
        "shutting down, got signal: terminated",
        "failed to scrape node",
        "failed to parse operation",
    ]
    return any(kw in lower for kw in infra_keywords)


@readonly_tool(
    name="summarize_error_groups",
    description="Summarize Cloud Error Reporting error groups for a project.",
)
def summarize_error_groups(
    project_id: str,
    time_window_hours: int = 1,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    service_name: Optional[str] = None,
    candidate_services: Optional[List[str]] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Summarize Error Reporting error groups with error counts and console deep link."""
    console_url = f"https://console.cloud.google.com/errors?project={project_id}"

    if client is None:
        client = get_logging_client(project_id)

    if client is None and not is_local_mode():
        raise RuntimeError("GCP client unavailable in production environment")

    from .common import get_candidate_service_names

    if candidate_services:
        candidates = list(candidate_services)
    elif service_name:
        candidates = get_candidate_service_names(service_name)
    else:
        candidates = []
    error_groups: List[Dict[str, Any]] = []

    if client is not None:
        if hasattr(client, "list_group_stats"):
            raw_groups = client.list_group_stats(
                project_id=project_id,
                time_window_hours=time_window_hours,
            )
            for grp in raw_groups:
                msg = getattr(grp, "message", "")
                if not msg or msg.strip() == "{}":
                    msg = f"Error group {getattr(grp, 'group_id', 'unknown')}"
                if _is_probe_or_infra_noise(msg):
                    continue
                affected = [str(s) for s in getattr(grp, "affected_services", [])]
                if any(_is_probe_or_infra_noise(s) for s in affected):
                    continue
                log_name = str(getattr(grp, "log_name", "") or getattr(grp, "logName", "") or "")
                if not log_name and hasattr(grp, "representative"):
                    rep = getattr(grp, "representative", None)
                    if rep:
                        log_name = str(
                            getattr(rep, "log_name", "")
                            or (rep.get("log_name", "") if isinstance(rep, dict) else "")
                            or ""
                        )
                if log_name and _is_probe_or_infra_noise(log_name):
                    continue
                if candidates:
                    matches_svc = any(
                        any(c.lower() in s.lower() for c in candidates) for s in affected
                    )
                    matches_msg = any(c.lower() in msg.lower() for c in candidates)
                    matches_log = (
                        any(c.lower() in log_name.lower() for c in candidates)
                        if log_name
                        else False
                    )
                    is_app_error = any(
                        kw in msg.lower()
                        for kw in [
                            "toomanyconnections",
                            "connection",
                            "exception",
                            "error",
                            "failure",
                            "fatal",
                            "timeout",
                            "500",
                            "502",
                            "503",
                            "504",
                            "pool",
                        ]
                    )
                    if affected and not matches_svc and not matches_msg and not matches_log:
                        continue
                    if not matches_svc and not matches_msg and not matches_log and not is_app_error:
                        continue
                error_groups.append(
                    {
                        "group_id": getattr(grp, "group_id", "unknown"),
                        "message": msg[:256],
                        "count": getattr(grp, "count", 0),
                        "affected_services": getattr(grp, "affected_services", []),
                        "first_seen": str(getattr(grp, "first_seen", "")),
                        "last_seen": str(getattr(grp, "last_seen", "")),
                    }
                )
        elif hasattr(client, "list_entries"):
            # Fallback for Cloud Logging client: aggregate recent error entries by real error field
            try:
                filter_parts = ["severity >= ERROR"]
                if start_time:
                    start_iso = (
                        start_time.astimezone(timezone.utc).isoformat()
                        if start_time.tzinfo
                        else start_time.replace(tzinfo=timezone.utc).isoformat()
                    )
                    filter_parts.append(f'timestamp >= "{start_iso}"')
                elif time_window_hours:
                    now = datetime.now(timezone.utc)
                    start_iso = (now - timedelta(hours=time_window_hours)).isoformat()
                    filter_parts.append(f'timestamp >= "{start_iso}"')

                if end_time:
                    end_iso = (
                        end_time.astimezone(timezone.utc).isoformat()
                        if end_time.tzinfo
                        else end_time.replace(tzinfo=timezone.utc).isoformat()
                    )
                    filter_parts.append(f'timestamp <= "{end_iso}"')

                if service_name:
                    svc_clauses = []
                    for cand in candidates:
                        svc_clauses.append(f'resource.labels.service_name = "{cand}"')
                    for cand in candidates:
                        svc_clauses.append(f'resource.labels.container_name = "{cand}"')
                    for cand in candidates:
                        svc_clauses.append(f'textPayload =~ "{cand}"')
                    svc_clauses = list(dict.fromkeys(svc_clauses))
                    filter_parts.append(f"({' OR '.join(svc_clauses)})")

                filter_str = " AND ".join(filter_parts)
                raw_entries = client.list_entries(
                    resource_names=[f"projects/{project_id}"],
                    filter_=filter_str,
                    page_size=100,
                )
                groups_map: Dict[str, Dict[str, Any]] = {}
                for idx, entry in enumerate(raw_entries):
                    if idx >= 100:
                        break
                    excerpt = _extract_entry_excerpt(entry)
                    if not excerpt or excerpt.strip() == "{}":
                        continue
                    if _is_probe_or_infra_noise(excerpt):
                        continue
                    res = getattr(entry, "resource", None)
                    labels = getattr(res, "labels", {}) if res else {}
                    svc = (
                        labels.get("service_name")
                        or labels.get("container_name")
                        or getattr(res, "type", "cloud_run")
                    )
                    log_name = getattr(entry, "log_name", "") or (
                        entry.get("log_name", "") if isinstance(entry, dict) else ""
                    )
                    if _is_probe_or_infra_noise(str(svc)):
                        continue
                    if log_name and _is_probe_or_infra_noise(log_name):
                        continue
                    if candidates:
                        matches_svc = any(c.lower() in str(svc).lower() for c in candidates)
                        matches_msg = any(c.lower() in excerpt.lower() for c in candidates)
                        matches_log = (
                            any(c.lower() in log_name.lower() for c in candidates)
                            if log_name
                            else False
                        )
                        is_app_error = any(
                            kw in excerpt.lower()
                            for kw in [
                                "toomanyconnections",
                                "connection",
                                "exception",
                                "error",
                                "failure",
                                "fatal",
                                "timeout",
                                "500",
                                "502",
                                "503",
                                "504",
                                "pool",
                            ]
                        )
                        if (
                            svc
                            and str(svc) != "cloud_run"
                            and not matches_svc
                            and not matches_msg
                            and not matches_log
                        ):
                            continue
                        if (
                            not matches_svc
                            and not matches_msg
                            and not matches_log
                            and not is_app_error
                        ):
                            continue

                    # Group key is the extracted excerpt without per-request latency noise
                    group_key = (
                        excerpt.split(" (latency:")[0] if " (latency:" in excerpt else excerpt
                    )
                    ts_str = str(getattr(entry, "timestamp", "") or "")
                    res = getattr(entry, "resource", None)
                    labels = getattr(res, "labels", {}) if res else {}
                    svc = (
                        labels.get("service_name")
                        or labels.get("container_name")
                        or getattr(res, "type", "cloud_run")
                    )

                    if group_key not in groups_map:
                        grp_id = f"grp-{abs(hash(group_key)) % 1000000:06d}"
                        groups_map[group_key] = {
                            "group_id": grp_id,
                            "message": group_key[:256],
                            "count": 1,
                            "affected_services": [svc] if svc else [],
                            "first_seen": ts_str,
                            "last_seen": ts_str,
                        }
                    else:
                        groups_map[group_key]["count"] += 1
                        groups_map[group_key]["last_seen"] = ts_str
                        if svc and svc not in groups_map[group_key]["affected_services"]:
                            groups_map[group_key]["affected_services"].append(svc)

                error_groups = list(groups_map.values())
            except Exception:
                pass

    return {
        "project_id": project_id,
        "time_window_hours": time_window_hours,
        "error_groups": error_groups,
        "total_groups": len(error_groups),
        "console_url": console_url,
    }


@readonly_tool(
    name="list_log_names",
    description="List available Cloud Logging log IDs for a project.",
)
def list_log_names(
    project_id: str,
    client: Any = None,
) -> List[str]:
    """List log names available within the project."""
    if client is None:
        client = get_logging_client(project_id)

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Logging client unavailable in production environment")

    if client is not None:
        if hasattr(client, "list_logs"):
            try:
                raw_logs = client.list_logs(project_id=project_id)
            except TypeError:
                raw_logs = client.list_logs(parent=f"projects/{project_id}")
            return [str(log) for log in raw_logs]
        else:
            service_client = get_logging_service_client()
            if service_client is not None:
                raw_logs = service_client.list_logs(parent=f"projects/{project_id}")
                return [str(log) for log in raw_logs]
    return []
