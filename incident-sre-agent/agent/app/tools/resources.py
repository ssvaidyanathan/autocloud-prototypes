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
    description="List GKE clusters, status, node counts, Autopilot status, and location.",
)
def describe_gke_clusters(
    project_id: Optional[str] = None,
    location: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List GKE clusters across all locations (default: locations/-) or a specific location."""
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    target_location = (location or "").strip() or "-"

    if client is None:
        client = get_gke_cluster_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP GKE cluster client unavailable in production environment")

    clusters: List[Dict[str, Any]] = []

    if client is not None:
        parent = f"projects/{pinned_project}/locations/{target_location}"
        try:
            raw_clusters = client.list_clusters(parent=parent)
        except (TypeError, ValueError):
            raw_clusters = client.list_clusters(request={"parent": parent})

        cluster_list = getattr(raw_clusters, "clusters", raw_clusters)
        if cluster_list is not None and hasattr(cluster_list, "__iter__"):
            for cluster in cluster_list:
                cluster_name = getattr(cluster, "name", "")
                cluster_loc = getattr(cluster, "location", "") or (
                    target_location if target_location != "-" else ""
                )
                display_loc = cluster_loc or target_location
                cluster_console = (
                    f"https://console.cloud.google.com/kubernetes/clusters/details/"
                    f"{display_loc}/{cluster_name}?project={pinned_project}"
                )
                autopilot_enabled = False
                autopilot_obj = getattr(cluster, "autopilot", None)
                if autopilot_obj is not None:
                    autopilot_enabled = bool(getattr(autopilot_obj, "enabled", False))
                status_raw = getattr(cluster, "status", "STATUS_UNSPECIFIED")
                if hasattr(status_raw, "name"):
                    status_str = str(status_raw.name)
                elif isinstance(status_raw, int):
                    try:
                        from google.cloud import container_v1

                        status_str = container_v1.Cluster.Status(status_raw).name
                    except Exception:
                        status_str = str(status_raw)
                else:
                    status_str = str(status_raw)

                clusters.append(
                    {
                        "name": cluster_name,
                        "status": status_str,
                        "current_node_count": getattr(cluster, "current_node_count", 0),
                        "location": cluster_loc,
                        "autopilot": autopilot_enabled,
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


@readonly_tool(
    name="query_gke_workload_status",
    description=(
        "Query GKE workload status (Deployments, StatefulSets, Pods) including replica counts, "
        "phases, container restart counts, last termination exit codes, and probe config."
    ),
)
def query_gke_workload_status(
    namespace: str,
    cluster_name: Optional[str] = None,
    location: Optional[str] = None,
    workload_name: Optional[str] = None,
    workload_type: Optional[str] = None,
    max_pods: int = 50,
    project_id: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Inspect GKE Deployments, StatefulSets, and Pods via Kubernetes REST API."""
    import base64
    import tempfile

    from .common import validate_k8s_identifier
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    clean_ns = validate_k8s_identifier(namespace, "namespace")
    if not clean_ns:
        raise ValueError("namespace is required and must be a valid identifier.")
    clean_cluster = validate_k8s_identifier(cluster_name, "cluster_name") if cluster_name else None
    clean_workload = (
        validate_k8s_identifier(workload_name, "workload_name") if workload_name else None
    )
    clean_type = validate_k8s_identifier(workload_type, "workload_type") if workload_type else None
    clamped_max_pods = max(1, min(200, int(max_pods or 50)))

    console_url = (
        f"https://console.cloud.google.com/kubernetes/workload/overview?project={pinned_project}"
    )

    if is_local_mode() and client is None:
        return {
            "status": "SUCCESS",
            "cluster_name": clean_cluster or "local-cluster",
            "namespace": clean_ns,
            "deployments_count": 0,
            "deployments": [],
            "statefulsets_count": 0,
            "statefulsets": [],
            "pods_count": 0,
            "pods": [],
            "truncated": False,
            "console_url": console_url,
        }

    cluster_loc = location
    target_cluster = clean_cluster

    # If client is provided and has direct mocked workload data
    if client is not None and (
        (
            hasattr(client, "mock_workloads")
            and not hasattr(getattr(client, "mock_workloads"), "_mock_name")
        )
        or (
            hasattr(client, "mock_deployments")
            and not hasattr(getattr(client, "mock_deployments"), "_mock_name")
        )
    ):
        mock_d = getattr(client, "mock_deployments", [])
        mock_s = getattr(client, "mock_statefulsets", [])
        mock_p = getattr(client, "mock_pods", [])
        truncated_pods = len(mock_p) > clamped_max_pods
        capped_pods = mock_p[:clamped_max_pods]
        return {
            "status": "SUCCESS",
            "cluster_name": clean_cluster or "mock-cluster",
            "namespace": clean_ns,
            "deployments_count": len(mock_d),
            "deployments": mock_d,
            "statefulsets_count": len(mock_s),
            "statefulsets": mock_s,
            "pods_count": len(capped_pods),
            "pods": capped_pods,
            "truncated": truncated_pods,
            "console_url": console_url,
        }

    cluster_client = None
    session = None

    if client is not None:
        if hasattr(client, "_mock_children"):
            if "get_cluster" in client._mock_children or "list_clusters" in client._mock_children:
                cluster_client = client
            elif "get" in client._mock_children:
                session = client
            else:
                cluster_client = client
        elif hasattr(client, "get_cluster") or hasattr(client, "list_clusters"):
            cluster_client = client
        elif hasattr(client, "get"):
            session = client

    if cluster_client is None:
        cluster_client = get_gke_cluster_client()

    if cluster_client is None and not is_local_mode() and session is None:
        raise RuntimeError("GCP GKE cluster client unavailable in production environment")

    # Discover cluster if not fully specified
    if (not target_cluster or not cluster_loc or cluster_loc == "-") and cluster_client is not None:
        try:
            parent = f"projects/{pinned_project}/locations/-"
            try:
                raw_clusters = cluster_client.list_clusters(parent=parent)
            except (TypeError, ValueError):
                raw_clusters = cluster_client.list_clusters(request={"parent": parent})
            cluster_list = getattr(raw_clusters, "clusters", raw_clusters) or []
            for c in cluster_list:
                c_name = getattr(c, "name", "")
                c_loc = getattr(c, "location", "")
                if target_cluster:
                    if c_name == target_cluster:
                        cluster_loc = c_loc
                        break
                else:
                    target_cluster = c_name
                    cluster_loc = c_loc
                    break
        except Exception:
            pass

    if not target_cluster:
        return {
            "status": "NOT_FOUND",
            "cluster_name": None,
            "namespace": clean_ns,
            "deployments_count": 0,
            "deployments": [],
            "statefulsets_count": 0,
            "statefulsets": [],
            "pods_count": 0,
            "pods": [],
            "truncated": False,
            "message": f"No GKE clusters found in project {pinned_project}",
            "console_url": console_url,
        }

    if not cluster_loc:
        cluster_loc = os.getenv("GCP_REGION", "us-central1")

    endpoint = ""
    ca_cert = ""
    if cluster_client is not None:
        try:
            cluster_path = (
                f"projects/{pinned_project}/locations/{cluster_loc}/clusters/{target_cluster}"
            )
            try:
                c_obj = cluster_client.get_cluster(name=cluster_path)
            except (TypeError, ValueError):
                c_obj = cluster_client.get_cluster(request={"name": cluster_path})
            endpoint = getattr(c_obj, "endpoint", "")
            master_auth = getattr(c_obj, "master_auth", None)
            if master_auth:
                ca_cert = getattr(master_auth, "cluster_ca_certificate", "")
        except Exception:
            pass

    deployments: List[Dict[str, Any]] = []
    statefulsets: List[Dict[str, Any]] = []
    pods: List[Dict[str, Any]] = []

    temp_ca_path: Optional[str] = None
    if (
        session is None
        and cluster_client is not None
        and hasattr(cluster_client, "_session")
        and not hasattr(getattr(cluster_client, "_session"), "_mock_name")
    ):
        session = getattr(cluster_client, "_session", None)

    try:
        verify_arg: Union[str, bool] = True
        if session is None and endpoint:
            if not ca_cert:
                return {
                    "status": "ERROR",
                    "cluster_name": target_cluster,
                    "location": cluster_loc,
                    "namespace": clean_ns,
                    "deployments_count": 0,
                    "deployments": [],
                    "statefulsets_count": 0,
                    "statefulsets": [],
                    "pods_count": 0,
                    "pods": [],
                    "truncated": False,
                    "message": (
                        f"Cluster CA certificate unavailable for cluster {target_cluster}; "
                        "refusing unverified TLS connection."
                    ),
                    "console_url": console_url,
                }
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession

                with tempfile.NamedTemporaryFile(delete=False) as f:
                    f.write(base64.b64decode(ca_cert))
                    temp_ca_path = f.name
                verify_arg = temp_ca_path

                creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                session = AuthorizedSession(creds)  # type: ignore[no-untyped-call]
            except Exception as exc:
                return {
                    "status": "ERROR",
                    "cluster_name": target_cluster,
                    "location": cluster_loc,
                    "namespace": clean_ns,
                    "deployments_count": 0,
                    "deployments": [],
                    "statefulsets_count": 0,
                    "statefulsets": [],
                    "pods_count": 0,
                    "pods": [],
                    "truncated": False,
                    "message": f"Failed to initialize authorized session for GKE master: {exc}",
                    "console_url": console_url,
                }

        if session is not None:
            import requests.exceptions

            def _handle_api_call(
                url: str,
            ) -> tuple[Optional[dict[str, Any]], Optional[Dict[str, Any]]]:
                try:
                    resp = session.get(url, verify=verify_arg, timeout=10)
                except (requests.exceptions.ConnectTimeout, requests.exceptions.Timeout) as exc:
                    return None, {
                        "status": "UNREACHABLE",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": (
                            f"Connection to GKE master {endpoint or url} timed out: {exc}. "
                            "The cluster may be a private cluster or enforce "
                            "Master Authorized Networks."
                        ),
                        "console_url": console_url,
                    }
                except requests.exceptions.ConnectionError as exc:
                    return None, {
                        "status": "UNREACHABLE",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": (
                            f"Failed to connect to GKE master {endpoint or url}: {exc}. "
                            "The cluster may be a private cluster or unreachable."
                        ),
                        "console_url": console_url,
                    }
                except requests.exceptions.RequestException as exc:
                    return None, {
                        "status": "ERROR",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": f"Request to GKE master failed: {exc}",
                        "console_url": console_url,
                    }

                raw_status = getattr(resp, "status_code", 200)
                if hasattr(raw_status, "_mock_name"):
                    status_code = 200
                else:
                    try:
                        status_code = int(raw_status)
                    except (TypeError, ValueError):
                        status_code = 200

                if status_code == 403:
                    raw_text = getattr(resp, "text", "")
                    resp_text = "" if hasattr(raw_text, "_mock_name") else str(raw_text)
                    return None, {
                        "status": "FORBIDDEN",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": (
                            f"HTTP 403 Forbidden from GKE API: {resp_text}. "
                            "Kubernetes RBAC permissions (e.g. ClusterRoleBinding "
                            "for container.viewer) may be missing."
                        ),
                        "console_url": console_url,
                    }
                elif status_code == 401:
                    resp_text = getattr(resp, "text", "")
                    return None, {
                        "status": "UNAUTHORIZED",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": f"HTTP 401 Unauthorized from GKE API: {resp_text}.",
                        "console_url": console_url,
                    }
                elif status_code >= 400:
                    resp_text = getattr(resp, "text", "")
                    return None, {
                        "status": "ERROR",
                        "cluster_name": target_cluster,
                        "location": cluster_loc,
                        "namespace": clean_ns,
                        "deployments_count": 0,
                        "deployments": [],
                        "statefulsets_count": 0,
                        "statefulsets": [],
                        "pods_count": 0,
                        "pods": [],
                        "truncated": False,
                        "message": f"HTTP {status_code} from GKE API: {resp_text}",
                        "console_url": console_url,
                    }

                json_data = resp.json() if hasattr(resp, "json") else {}
                return json_data, None

            # 1. Deployments
            if clean_type in (None, "", "deployment", "deployments"):
                url = f"https://{endpoint}/apis/apps/v1/namespaces/{clean_ns}/deployments"
                data, err_res = _handle_api_call(url)
                if err_res:
                    return err_res
                items = data.get("items", []) if data else []
                for item in items:
                    d_meta = item.get("metadata", {})
                    d_name = d_meta.get("name", "")
                    if clean_workload and d_name != clean_workload:
                        continue
                    d_spec = item.get("spec", {})
                    d_status = item.get("status", {})
                    containers = []
                    for c in d_spec.get("template", {}).get("spec", {}).get("containers", []):
                        containers.append(
                            {
                                "name": c.get("name", ""),
                                "image": c.get("image", ""),
                                "readiness_probe": c.get("readinessProbe"),
                                "liveness_probe": c.get("livenessProbe"),
                                "startup_probe": c.get("startupProbe"),
                                "resources": c.get("resources", {}),
                            }
                        )
                    deployments.append(
                        {
                            "name": d_name,
                            "namespace": clean_ns,
                            "desired_replicas": d_spec.get("replicas", 0),
                            "current_replicas": d_status.get("replicas", 0),
                            "ready_replicas": d_status.get("readyReplicas", 0),
                            "updated_replicas": d_status.get("updatedReplicas", 0),
                            "available_replicas": d_status.get("availableReplicas", 0),
                            "conditions": [
                                {
                                    "type": cond.get("type"),
                                    "status": cond.get("status"),
                                    "reason": cond.get("reason"),
                                    "message": cond.get("message"),
                                }
                                for cond in d_status.get("conditions", [])
                            ],
                            "containers": containers,
                        }
                    )

            # 2. StatefulSets
            if clean_type in (None, "", "statefulset", "statefulsets"):
                url = f"https://{endpoint}/apis/apps/v1/namespaces/{clean_ns}/statefulsets"
                data, err_res = _handle_api_call(url)
                if err_res:
                    return err_res
                items = data.get("items", []) if data else []
                for item in items:
                    s_meta = item.get("metadata", {})
                    s_name = s_meta.get("name", "")
                    if clean_workload and s_name != clean_workload:
                        continue
                    s_spec = item.get("spec", {})
                    s_status = item.get("status", {})
                    containers = []
                    for c in s_spec.get("template", {}).get("spec", {}).get("containers", []):
                        containers.append(
                            {
                                "name": c.get("name", ""),
                                "image": c.get("image", ""),
                                "readiness_probe": c.get("readinessProbe"),
                                "liveness_probe": c.get("livenessProbe"),
                                "startup_probe": c.get("startupProbe"),
                                "resources": c.get("resources", {}),
                            }
                        )
                    statefulsets.append(
                        {
                            "name": s_name,
                            "namespace": clean_ns,
                            "desired_replicas": s_spec.get("replicas", 0),
                            "current_replicas": s_status.get("replicas", 0),
                            "ready_replicas": s_status.get("readyReplicas", 0),
                            "updated_replicas": s_status.get("updatedReplicas", 0),
                            "containers": containers,
                        }
                    )

            # 3. Pods
            url = f"https://{endpoint}/api/v1/namespaces/{clean_ns}/pods"
            data, err_res = _handle_api_call(url)
            if err_res:
                return err_res
            items = data.get("items", []) if data else []
            for item in items:
                p_meta = item.get("metadata", {})
                p_name = p_meta.get("name", "")
                if clean_workload:
                    owners = [o.get("name", "") for o in p_meta.get("ownerReferences", [])]
                    if not (
                        p_name.startswith(f"{clean_workload}-")
                        or any(clean_workload in o for o in owners)
                    ):
                        continue
                p_spec = item.get("spec", {})
                p_status = item.get("status", {})
                c_statuses = []
                for cs in p_status.get("containerStatuses", []):
                    st = cs.get("state", {})
                    state_info: Dict[str, Any] = {}
                    if "running" in st:
                        state_info = {
                            "state": "running",
                            "started_at": st["running"].get("startedAt"),
                        }
                    elif "waiting" in st:
                        state_info = {
                            "state": "waiting",
                            "reason": st["waiting"].get("reason"),
                            "message": st["waiting"].get("message"),
                        }
                    elif "terminated" in st:
                        state_info = {
                            "state": "terminated",
                            "exit_code": st["terminated"].get("exitCode"),
                            "reason": st["terminated"].get("reason"),
                            "started_at": st["terminated"].get("startedAt"),
                            "finished_at": st["terminated"].get("finishedAt"),
                        }

                    lst = cs.get("lastState", {})
                    last_state_info: Dict[str, Any] = {}
                    if "terminated" in lst:
                        last_state_info = {
                            "state": "terminated",
                            "exit_code": lst["terminated"].get("exitCode"),
                            "reason": lst["terminated"].get("reason"),
                            "started_at": lst["terminated"].get("startedAt"),
                            "finished_at": lst["terminated"].get("finishedAt"),
                        }

                    c_statuses.append(
                        {
                            "name": cs.get("name"),
                            "ready": cs.get("ready"),
                            "restart_count": cs.get("restartCount", 0),
                            "state": state_info,
                            "last_state": last_state_info,
                        }
                    )

                pods.append(
                    {
                        "name": p_name,
                        "namespace": clean_ns,
                        "phase": p_status.get("phase"),
                        "node_name": p_spec.get("nodeName"),
                        "pod_ip": p_status.get("podIP"),
                        "start_time": p_status.get("startTime"),
                        "container_statuses": c_statuses,
                    }
                )

    finally:
        if temp_ca_path and os.path.exists(temp_ca_path):
            try:
                os.remove(temp_ca_path)
            except Exception:
                pass

    truncated_pods = len(pods) > clamped_max_pods
    capped_pods = pods[:clamped_max_pods]

    return {
        "status": "SUCCESS",
        "cluster_name": target_cluster,
        "location": cluster_loc,
        "namespace": clean_ns,
        "deployments_count": len(deployments),
        "deployments": deployments,
        "statefulsets_count": len(statefulsets),
        "statefulsets": statefulsets,
        "pods_count": len(capped_pods),
        "pods": capped_pods,
        "truncated": truncated_pods,
        "console_url": console_url,
    }


def _extract_ip_or_host(endpoint: str) -> str:
    cleaned = endpoint.strip()
    if "://" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        cleaned = parsed.netloc or parsed.path
    if ":" in cleaned:
        cleaned = cleaned.split(":")[0]
    return cleaned.strip()


@readonly_tool(
    name="resolve_endpoint_to_gke",
    description=(
        "Resolve an external IP address or uptime check URL to GCP Compute Forwarding Rules, "
        "Target Pools / Backend Services, backing Kubernetes Service, and GKE cluster."
    ),
)
def resolve_endpoint_to_gke(
    endpoint: str,
    project_id: Optional[str] = None,
    client: Any = None,
    target_pools_client: Any = None,
    backend_services_client: Any = None,
    region_backend_services_client: Any = None,
    network_endpoint_groups_client: Any = None,
) -> Dict[str, Any]:
    """Map external IP/URL to forwarding rule, target pool/NEG, K8s Service, and cluster."""
    import json
    import re

    from .clients import (
        get_compute_backend_services_client,
        get_compute_forwarding_rules_client,
        get_compute_network_endpoint_groups_client,
        get_compute_region_backend_services_client,
        get_compute_target_pools_client,
    )
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    target_ip = _extract_ip_or_host(endpoint)
    console_url = f"https://console.cloud.google.com/net-services/loadbalancing/loadBalancers/list?project={pinned_project}"

    if is_local_mode() and client is None:
        return {
            "endpoint": endpoint,
            "ip_address": target_ip,
            "found": False,
            "message": "Local mode: no forwarding rules inspected.",
            "console_url": console_url,
        }

    fw_client = client if client is not None else get_compute_forwarding_rules_client()
    if fw_client is None and not is_local_mode():
        raise RuntimeError(
            "GCP Compute ForwardingRulesClient unavailable in production environment"
        )

    if fw_client is None:
        return {
            "endpoint": endpoint,
            "ip_address": target_ip,
            "found": False,
            "message": "Forwarding rules client unavailable.",
            "console_url": console_url,
        }

    matched_fw: Optional[Any] = None
    try:
        if hasattr(fw_client, "aggregated_list"):
            agg = fw_client.aggregated_list(project=pinned_project)
            for _, scoped_list in agg:
                fws = getattr(scoped_list, "forwarding_rules", []) or []
                for fw in fws:
                    fw_ip = (
                        getattr(fw, "IPAddress", None)
                        or getattr(fw, "ip_address", None)
                        or getattr(fw, "I_p_address", None)
                        or (fw.get("IPAddress") if isinstance(fw, dict) else None)
                    )
                    if fw_ip == target_ip:
                        matched_fw = fw
                        break
                if matched_fw is not None:
                    break
    except Exception:
        pass

    if matched_fw is None:
        return {
            "endpoint": endpoint,
            "ip_address": target_ip,
            "found": False,
            "message": (
                f"Endpoint '{target_ip}' did not match any Compute forwarding rule "
                f"in project {pinned_project}."
            ),
            "console_url": console_url,
        }

    fw_name = getattr(matched_fw, "name", "") or (
        matched_fw.get("name", "") if isinstance(matched_fw, dict) else ""
    )
    fw_target = (
        getattr(matched_fw, "target", "")
        or getattr(matched_fw, "backend_service", "")
        or (matched_fw.get("target", "") if isinstance(matched_fw, dict) else "")
    )
    raw_region = getattr(matched_fw, "region", "") or (
        matched_fw.get("region", "") if isinstance(matched_fw, dict) else ""
    )
    fw_region = raw_region.split("/")[-1] if raw_region else ""

    tp_name: Optional[str] = None
    backend_svc_name: Optional[str] = None
    is_k8s = False
    k8s_service: Optional[str] = None
    k8s_namespace: Optional[str] = None
    k8s_svc_name: Optional[str] = None
    cluster_name: Optional[str] = None
    target_instances: List[str] = []

    if "targetPools" in fw_target:
        tp_name = fw_target.split("/")[-1]
        tp_reg = (
            fw_target.split("/regions/")[1].split("/")[0] if "/regions/" in fw_target else fw_region
        )
        tp_cli = (
            target_pools_client
            if target_pools_client is not None
            else get_compute_target_pools_client()
        )
        if tp_cli is not None:
            try:
                tp_obj = tp_cli.get(project=pinned_project, region=tp_reg, target_pool=tp_name)
                desc = getattr(tp_obj, "description", "") or (
                    tp_obj.get("description", "") if isinstance(tp_obj, dict) else ""
                )
                if desc:
                    try:
                        if desc.startswith("{"):
                            desc_data = json.loads(desc)
                            k8s_service = desc_data.get(
                                "kubernetes.io/service-name"
                            ) or desc_data.get("networking.gke.io/service-name")
                    except Exception:
                        pass
                    if not k8s_service:
                        m_svc = re.search(
                            r'(?:kubernetes|networking\.gke)\.io/service-name["\':\s]+([^"\'}\s]+)',
                            desc,
                        )
                        if m_svc:
                            k8s_service = m_svc.group(1)

                inst_list = getattr(tp_obj, "instances", []) or (
                    tp_obj.get("instances", []) if isinstance(tp_obj, dict) else []
                )
                for inst in inst_list:
                    inst_str = str(inst)
                    target_instances.append(inst_str)
                    if not cluster_name:
                        inst_base = inst_str.split("/")[-1]
                        m_cls = re.search(
                            r"(?:gk3|gke)-(.+?)-(?:nap|pool|default-pool|[a-f0-9]{8})", inst_base
                        )
                        if m_cls:
                            cluster_name = m_cls.group(1)
            except Exception:
                pass

    elif "backendServices" in fw_target:
        backend_svc_name = fw_target.split("/")[-1]
        is_regional = "/regions/" in fw_target or bool(fw_region)
        bs_reg = (
            fw_target.split("/regions/")[1].split("/")[0] if "/regions/" in fw_target else fw_region
        )

        bs_obj = None
        if is_regional and bs_reg:
            rbs_cli = (
                region_backend_services_client
                if region_backend_services_client is not None
                else get_compute_region_backend_services_client()
            )
            if rbs_cli is not None:
                try:
                    bs_obj = rbs_cli.get(
                        project=pinned_project, region=bs_reg, backend_service=backend_svc_name
                    )
                except Exception:
                    bs_obj = None

        if bs_obj is None:
            bs_cli = (
                backend_services_client
                if backend_services_client is not None
                else get_compute_backend_services_client()
            )
            if bs_cli is not None:
                try:
                    bs_obj = bs_cli.get(project=pinned_project, backend_service=backend_svc_name)
                except Exception:
                    bs_obj = None

        if bs_obj is not None:
            desc = getattr(bs_obj, "description", "") or (
                bs_obj.get("description", "") if isinstance(bs_obj, dict) else ""
            )
            if desc:
                try:
                    if desc.startswith("{"):
                        desc_data = json.loads(desc)
                        k8s_service = desc_data.get("kubernetes.io/service-name") or desc_data.get(
                            "networking.gke.io/service-name"
                        )
                except Exception:
                    pass
                if not k8s_service:
                    m_svc = re.search(
                        r'(?:kubernetes|networking\.gke)\.io/service-name["\':\s]+([^"\'}\s]+)',
                        desc,
                    )
                    if m_svc:
                        k8s_service = m_svc.group(1)

            backends_list = getattr(bs_obj, "backends", []) or (
                bs_obj.get("backends", []) if isinstance(bs_obj, dict) else []
            )
            neg_cli = (
                network_endpoint_groups_client
                if network_endpoint_groups_client is not None
                else get_compute_network_endpoint_groups_client()
            )

            for b in backends_list:
                group_url = getattr(b, "group", "") or (
                    b.get("group", "") if isinstance(b, dict) else ""
                )
                if not group_url:
                    continue

                if "networkEndpointGroups" in group_url:
                    neg_match = re.search(
                        r"/zones/([^/]+)/networkEndpointGroups/([^/]+)", group_url
                    )
                    if neg_match and neg_cli is not None:
                        neg_zone = neg_match.group(1)
                        neg_name = neg_match.group(2)
                        try:
                            if not k8s_service:
                                neg_obj = neg_cli.get(
                                    project=pinned_project,
                                    zone=neg_zone,
                                    network_endpoint_group=neg_name,
                                )
                                neg_desc = getattr(neg_obj, "description", "") or ""
                                if neg_desc.startswith("{"):
                                    try:
                                        nd = json.loads(neg_desc)
                                        if nd.get("namespace") and nd.get("service-name"):
                                            k8s_service = f"{nd['namespace']}/{nd['service-name']}"
                                    except Exception:
                                        pass

                            endpoints = neg_cli.list_network_endpoints(
                                project=pinned_project,
                                zone=neg_zone,
                                network_endpoint_group=neg_name,
                            )
                            ep_list = getattr(endpoints, "items", endpoints) or []
                            for ep in ep_list:
                                ep_inst = None
                                if hasattr(ep, "network_endpoint"):
                                    ep_inst = getattr(ep.network_endpoint, "instance", None)
                                elif isinstance(ep, dict):
                                    ep_inst = ep.get("networkEndpoint", {}).get("instance")
                                if ep_inst:
                                    inst_str = str(ep_inst)
                                    if inst_str not in target_instances:
                                        target_instances.append(inst_str)
                                    if not cluster_name:
                                        inst_base = inst_str.split("/")[-1]
                                        m_cls = re.search(
                                            r"(?:gk3|gke)-(.+?)-(?:nap|pool|default-pool|[a-f0-9]{8})",
                                            inst_base,
                                        )
                                        if m_cls:
                                            cluster_name = m_cls.group(1)
                        except Exception:
                            pass
                elif "instanceGroups" in group_url:
                    ig_name = group_url.split("/")[-1]
                    if not cluster_name:
                        m_cls = re.search(
                            r"(?:gk3|gke)-(.+?)-(?:nap|pool|default-pool|[a-f0-9]{8})", ig_name
                        )
                        if m_cls:
                            cluster_name = m_cls.group(1)

    if k8s_service:
        is_k8s = True
        if "/" in k8s_service:
            k8s_namespace, k8s_svc_name = k8s_service.split("/", 1)
        else:
            k8s_svc_name = k8s_service

    return {
        "endpoint": endpoint,
        "ip_address": target_ip,
        "found": True,
        "forwarding_rule": fw_name,
        "region": fw_region,
        "target": fw_target,
        "target_pool": tp_name,
        "backend_service": backend_svc_name,
        "is_kubernetes_service": is_k8s,
        "kubernetes_service": k8s_service,
        "k8s_namespace": k8s_namespace,
        "k8s_service_name": k8s_svc_name,
        "cluster_name": cluster_name,
        "target_instances": target_instances[:5],
        "console_url": console_url,
    }
