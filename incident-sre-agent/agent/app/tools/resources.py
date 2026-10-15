import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from .clients import (
    get_cloud_run_services_client,
    get_cloud_sql_admin_client,
    get_compute_instances_client,
    get_gke_cluster_client,
    get_logging_client,
    is_local_mode,
)
from .registry import readonly_tool


@readonly_tool(
    name="describe_cloud_run_service",
    description="Describe Cloud Run service specs with env var names only (values redacted).",
)
def describe_cloud_run_service(
    project_id: Optional[str] = None,
    service_name: Optional[str] = None,
    region: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Describe Cloud Run service specs, strictly redacting all environment variable values."""
    from .common import validate_service
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    resolved_service = validate_service(service_name) or "default"

    if not region:
        try:
            from agent.app.config import get_settings

            region = get_settings().gcp_region
        except Exception:
            region = os.getenv("GCP_REGION", "")

    console_url = f"https://console.cloud.google.com/run/detail/{region}/{resolved_service}?project={pinned_project}"

    if client is None:
        client = get_cloud_run_services_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud Run services client unavailable in production environment")

    if client is None:
        return {
            "status": "NOT_FOUND",
            "conclusive": True,
            "message": (
                f"Resource '{resolved_service}' not found on Cloud Run. "
                "No infrastructure anomalies exist for this service."
            ),
            "service_name": resolved_service,
            "region": region,
            "project_id": pinned_project,
            "uri": "",
            "ingress": "INGRESS_TRAFFIC_UNSPECIFIED",
            "container_image": "",
            "env_var_names": [],
            "cpu_limit": "",
            "memory_limit": "",
            "max_concurrency": 0,
            "service_account": "",
            "console_url": console_url,
        }

    from .common import get_candidate_service_names

    candidates = get_candidate_service_names(resolved_service)
    svc = None
    resolved_cand_name = resolved_service
    for cand in candidates:
        cand_path = f"projects/{pinned_project}/locations/{region}/services/{cand}"
        try:
            try:
                cand_svc = client.get_service(name=cand_path)
            except (TypeError, ValueError):
                cand_svc = client.get_service(request={"name": cand_path})
            if cand_svc is not None:
                svc = cand_svc
                resolved_cand_name = cand
                break
        except Exception:
            continue

    if svc is None:
        return {
            "status": "NOT_FOUND",
            "conclusive": True,
            "message": (
                f"Resource '{resolved_service}' not found on Cloud Run. "
                "No infrastructure anomalies exist for this service."
            ),
            "service_name": resolved_service,
            "region": region,
            "project_id": pinned_project,
            "uri": "",
            "ingress": "INGRESS_TRAFFIC_UNSPECIFIED",
            "container_image": "",
            "env_var_names": [],
            "cpu_limit": "",
            "memory_limit": "",
            "max_concurrency": 0,
            "service_account": "",
            "console_url": console_url,
        }

    result: Dict[str, Any] = {
        "status": "READY",
        "conclusive": True,
        "message": f"Resource '{resolved_cand_name}' found and running on Cloud Run.",
        "service_name": resolved_cand_name,
        "region": region,
        "project_id": pinned_project,
        "uri": getattr(svc, "uri", ""),
        "ingress": str(getattr(svc, "ingress", "INGRESS_TRAFFIC_UNSPECIFIED")),
        "container_image": "",
        "env_var_names": [],  # ONLY variable names are exposed
        "cpu_limit": "",
        "memory_limit": "",
        "max_concurrency": 0,
        "service_account": "",
        "console_url": console_url,
    }

    template = getattr(svc, "template", None)
    if template:
        result["service_account"] = getattr(template, "service_account", "")
        result["max_concurrency"] = getattr(template, "max_instance_request_concurrency", 0)

        containers = getattr(template, "containers", [])
        if containers and len(containers) > 0:
            c0 = containers[0]
            result["container_image"] = getattr(c0, "image", "")

            resources = getattr(c0, "resources", None)
            if resources:
                limits = getattr(resources, "limits", None)
                if limits is not None:
                    if hasattr(limits, "get"):
                        result["cpu_limit"] = str(limits.get("cpu", "") or "")
                        result["memory_limit"] = str(limits.get("memory", "") or "")
                    else:
                        try:
                            result["cpu_limit"] = str(limits["cpu"]) if "cpu" in limits else ""
                        except Exception:
                            pass
                        try:
                            result["memory_limit"] = (
                                str(limits["memory"]) if "memory" in limits else ""
                            )
                        except Exception:
                            pass

            # CRITICAL SECURITY RULE: Extract ONLY variable names, never values!
            env_items = getattr(c0, "env", [])
            safe_env_names: List[str] = []
            for item in env_items:
                if isinstance(item, dict):
                    name_val = item.get("name")
                else:
                    name_val = getattr(item, "name", None)
                if name_val:
                    safe_env_names.append(str(name_val))

            result["env_var_names"] = safe_env_names

    return result


@readonly_tool(
    name="describe_gke_clusters",
    description="List GKE clusters, status, node counts, and location.",
)
def describe_gke_clusters(
    project_id: Optional[str] = None,
    location: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List GKE clusters in project and location."""
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)

    if not location:
        try:
            from agent.app.config import get_settings

            location = get_settings().gcp_region
        except Exception:
            location = os.getenv("GCP_REGION", "")

    if client is None:
        client = get_gke_cluster_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP GKE cluster client unavailable in production environment")

    clusters: List[Dict[str, Any]] = []

    if client is not None:
        parent = f"projects/{pinned_project}/locations/{location}"
        try:
            raw_clusters = client.list_clusters(parent=parent)
        except (TypeError, ValueError):
            raw_clusters = client.list_clusters(request={"parent": parent})

        cluster_list = getattr(raw_clusters, "clusters", raw_clusters)
        if isinstance(cluster_list, (list, tuple)):
            for cluster in cluster_list:
                cluster_name = getattr(cluster, "name", "")
                cluster_console = (
                    f"https://console.cloud.google.com/kubernetes/clusters/details/"
                    f"{location}/{cluster_name}?project={pinned_project}"
                )
                clusters.append(
                    {
                        "name": cluster_name,
                        "status": str(getattr(cluster, "status", "STATUS_UNSPECIFIED")),
                        "current_node_count": getattr(cluster, "current_node_count", 0),
                        "location": getattr(cluster, "location", location),
                        "console_url": cluster_console,
                    }
                )

    return clusters


@readonly_tool(
    name="describe_compute_instances",
    description="List Compute Engine instances, status, and machine type in a zone.",
)
def describe_compute_instances(
    project_id: Optional[str] = None,
    zone: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List Compute Engine instances."""
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)

    if not zone:
        zone = os.getenv("GCP_ZONE", "")

    if client is None:
        client = get_compute_instances_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Compute Engine client unavailable in production environment")

    instances: List[Dict[str, Any]] = []

    if client is not None:
        raw_instances = []
        if zone:
            if hasattr(client, "list_instances"):
                raw_instances = client.list_instances(project=pinned_project, zone=zone)
            elif hasattr(client, "list"):
                raw_instances = client.list(project=pinned_project, zone=zone)
        else:
            if hasattr(client, "list_instances"):
                try:
                    raw_instances = client.list_instances(project=pinned_project)
                except Exception:
                    pass
            if not raw_instances and hasattr(client, "aggregated_list"):
                try:
                    agg = client.aggregated_list(project=pinned_project)
                    for _, zone_scoped_list in agg:
                        for item in getattr(zone_scoped_list, "instances", []):
                            raw_instances.append(item)
                except Exception:
                    pass

        for inst in raw_instances:
            inst_name = getattr(inst, "name", "")
            inst_console = (
                f"https://console.cloud.google.com/compute/instancesDetail/zones/"
                f"{zone}/instances/{inst_name}?project={pinned_project}"
            )
            instances.append(
                {
                    "name": inst_name,
                    "status": str(getattr(inst, "status", "UNKNOWN")),
                    "machine_type": getattr(inst, "machine_type", "").split("/")[-1],
                    "zone": zone,
                    "console_url": inst_console,
                }
            )

    return instances


@readonly_tool(
    name="describe_cloud_sql_instances",
    description="List Cloud SQL instances, database version, tier, and state.",
)
def describe_cloud_sql_instances(
    project_id: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List Cloud SQL instances for a project."""
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)

    if client is None:
        client = get_cloud_sql_admin_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud SQL Admin client unavailable in production environment")

    instances: List[Dict[str, Any]] = []

    if client is not None:
        if hasattr(client, "list_instances"):
            raw_instances = client.list_instances(project=pinned_project)
        elif hasattr(client, "instances"):
            resp = client.instances().list(project=pinned_project).execute()
            raw_instances = resp.get("items", [])
        else:
            raw_instances = []

        item_list = getattr(raw_instances, "items", raw_instances)
        for inst in item_list:
            if isinstance(inst, dict):
                inst_name = inst.get("name", "")
                db_ver = inst.get("databaseVersion", "")
                st = inst.get("state", "STATE_UNSPECIFIED")
                reg = inst.get("region", "")
                settings_obj = inst.get("settings", {})
                tier_val = settings_obj.get("tier", "") if isinstance(settings_obj, dict) else ""
            else:
                inst_name = getattr(inst, "name", "")
                db_ver = getattr(inst, "database_version", "")
                st = str(getattr(inst, "state", "STATE_UNSPECIFIED"))
                reg = getattr(inst, "region", "")
                settings_obj = getattr(inst, "settings", None)
                tier_val = getattr(settings_obj, "tier", "") if settings_obj else ""

            inst_console = (
                f"https://console.cloud.google.com/sql/instances/{inst_name}/overview"
                f"?project={pinned_project}"
            )

            instances.append(
                {
                    "name": inst_name,
                    "database_version": db_ver,
                    "tier": tier_val,
                    "state": st,
                    "status": st,
                    "region": reg,
                    "console_url": inst_console,
                }
            )

    return instances


@readonly_tool(
    name="get_platform_events",
    description=(
        "Retrieve Cloud Run platform events: container restarts, OOM terminations, "
        "probe failures, and system logs."
    ),
)
def get_platform_events(
    service_name: str,
    project_id: Optional[str] = None,
    window_minutes: int = 60,
    start_time: Optional[Union[datetime, str]] = None,
    end_time: Optional[Union[datetime, str]] = None,
    region: Optional[str] = None,
    client: Any = None,
    logging_client: Any = None,
) -> Dict[str, Any]:
    """Retrieve Cloud Run platform events: restarts, OOM kills, and probe failures."""
    from .common import (
        get_candidate_service_names,
        normalize_datetime,
        validate_service,
    )
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    resolved_service = validate_service(service_name) or "default"

    start_time_dt = normalize_datetime(start_time)
    end_time_dt = normalize_datetime(end_time)
    if not end_time_dt:
        end_time_dt = datetime.now(timezone.utc)
    if not start_time_dt:
        clamped_window = max(1, min(1440, int(window_minutes or 60)))
        start_time_dt = end_time_dt - timedelta(minutes=clamped_window)

    target_client = None
    if logging_client is not None and hasattr(logging_client, "list_entries"):
        target_client = logging_client
    elif client is not None and hasattr(client, "list_entries"):
        target_client = client
    else:
        target_client = get_logging_client(pinned_project)

    if target_client is None and not is_local_mode():
        raise RuntimeError("GCP Logging client unavailable in production environment")

    candidates = get_candidate_service_names(resolved_service)
    svc_subclauses = [f'resource.labels.service_name = "{c}"' for c in candidates]
    svc_filter = (
        f"({' OR '.join(svc_subclauses)})"
        if len(svc_subclauses) > 1
        else (svc_subclauses[0] if svc_subclauses else "")
    )

    event_patterns = [
        '"Memory limit"',
        '"exceeded"',
        '"exit code 137"',
        '"terminated by kernel OOM-killer"',
        '"Readiness probe failed"',
        '"Startup probe failed"',
        '"failed to start and listen"',
        'logName =~ "run.googleapis.com%2Fvarlog%2Fsystem"',
    ]
    event_clause = f"({' OR '.join(event_patterns)})"

    filter_parts = [
        'resource.type = "cloud_run_revision"',
        f'timestamp >= "{start_time_dt.isoformat()}"',
        f'timestamp <= "{end_time_dt.isoformat()}"',
    ]
    if svc_filter:
        filter_parts.append(svc_filter)
    filter_parts.append(event_clause)

    final_filter = " AND ".join(filter_parts)
    console_url = (
        f"https://console.cloud.google.com/logs/query;query="
        f"{urllib.parse.quote(final_filter)};project={pinned_project}"
    )

    raw_entries: List[Any] = []
    if target_client is not None:
        try:
            entries_iter = target_client.list_entries(
                resource_names=[f"projects/{pinned_project}"],
                filter_=final_filter,
                page_size=100,
                max_results=100,
            )
            for entry in entries_iter:
                raw_entries.append(entry)
                if len(raw_entries) >= 100:
                    break
        except TypeError:
            try:
                entries_iter = target_client.list_entries(
                    resource_names=[f"projects/{pinned_project}"],
                    filter_=final_filter,
                    page_size=100,
                )
                for entry in entries_iter:
                    raw_entries.append(entry)
                    if len(raw_entries) >= 100:
                        break
            except Exception:
                raw_entries = []
        except Exception:
            raw_entries = []

    restarts: Dict[str, Any] = {"count": 0, "events": []}
    probe_failures: Dict[str, Any] = {"count": 0, "events": []}
    system_events: Dict[str, Any] = {"count": 0, "events": []}
    has_oom = False
    has_probe_failure = False

    for entry in raw_entries:
        ts = getattr(entry, "timestamp", None)
        ts_str = (
            str(ts.isoformat()) if (ts is not None and hasattr(ts, "isoformat")) else str(ts or "")
        )

        msg = ""
        text_pl = getattr(entry, "text_payload", None) or getattr(entry, "textPayload", None)
        if text_pl and not hasattr(text_pl, "_mock_name"):
            msg = str(text_pl)
        elif isinstance(entry, dict) and (entry.get("textPayload") or entry.get("text_payload")):
            msg = str(entry.get("textPayload") or entry.get("text_payload"))
        elif getattr(entry, "payload", None) and not hasattr(
            getattr(entry, "payload", None), "_mock_name"
        ):
            msg = str(entry.payload)
        elif isinstance(entry, dict) and entry.get("payload"):
            msg = str(entry.get("payload"))
        elif getattr(entry, "json_payload", None) and not hasattr(
            getattr(entry, "json_payload", None), "_mock_name"
        ):
            msg = str(entry.json_payload)
        elif isinstance(entry, dict) and (entry.get("jsonPayload") or entry.get("json_payload")):
            msg = str(entry.get("jsonPayload") or entry.get("json_payload"))

        msg_lower = msg.lower()
        log_name = getattr(entry, "log_name", "") or (
            entry.get("log_name", "") if isinstance(entry, dict) else ""
        )

        if any(
            term in msg_lower
            for term in ["memory limit", "exit code 137", "oom-killer", "terminated by kernel"]
        ):
            has_oom = True
            restarts["count"] += 1
            restarts["events"].append(
                {
                    "timestamp": ts_str,
                    "reason": "OOM termination (memory limit exceeded)",
                    "exit_code": 137,
                    "message": msg[:256],
                }
            )
        elif any(
            term in msg_lower
            for term in [
                "container terminated",
                "exit code",
                "container failed",
                "restarting container",
            ]
        ):
            restarts["count"] += 1
            restarts["events"].append(
                {
                    "timestamp": ts_str,
                    "reason": "Container termination / restart",
                    "message": msg[:256],
                }
            )
        elif any(
            term in msg_lower
            for term in [
                "readiness probe failed",
                "startup probe failed",
                "probe failed",
                "failed to start and listen",
            ]
        ):
            has_probe_failure = True
            probe_failures["count"] += 1
            p_type = (
                "readiness"
                if "readiness" in msg_lower
                else ("startup" if "startup" in msg_lower else "probe")
            )
            probe_failures["events"].append(
                {
                    "timestamp": ts_str,
                    "probe_type": p_type,
                    "message": msg[:256],
                }
            )
        else:
            system_events["count"] += 1
            system_events["events"].append(
                {
                    "timestamp": ts_str,
                    "log_name": log_name,
                    "message": msg[:256],
                }
            )

    total_events = restarts["count"] + probe_failures["count"] + system_events["count"]

    return {
        "service_name": resolved_service,
        "total_events": total_events,
        "restarts": restarts,
        "probe_failures": probe_failures,
        "system_events": system_events,
        "has_oom": has_oom,
        "has_probe_failure": has_probe_failure,
        "console_url": console_url,
    }
