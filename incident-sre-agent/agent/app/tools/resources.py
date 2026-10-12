import os
from typing import Any, Dict, List, Optional

from .clients import (
    get_cloud_run_services_client,
    get_cloud_sql_admin_client,
    get_compute_instances_client,
    get_gke_cluster_client,
    is_local_mode,
)
from .registry import readonly_tool


@readonly_tool(
    name="describe_cloud_run_service",
    description="Describe Cloud Run service specs with env var names only (values redacted).",
)
def describe_cloud_run_service(
    project_id: str,
    service_name: str,
    region: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Describe Cloud Run service specs, strictly redacting all environment variable values."""
    if not region:
        try:
            from agent.app.config import get_settings

            region = get_settings().gcp_region
        except Exception:
            region = os.getenv("GCP_REGION", "")

    console_url = (
        f"https://console.cloud.google.com/run/detail/{region}/{service_name}?project={project_id}"
    )

    if client is None:
        client = get_cloud_run_services_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud Run services client unavailable in production environment")

    result: Dict[str, Any] = {
        "service_name": service_name,
        "region": region,
        "project_id": project_id,
        "uri": "",
        "ingress": "INGRESS_TRAFFIC_UNSPECIFIED",
        "container_image": "",
        "env_var_names": [],  # ONLY variable names are exposed
        "cpu_limit": "",
        "memory_limit": "",
        "max_concurrency": 0,
        "service_account": "",
        "console_url": console_url,
    }

    if client is not None:
        from .common import get_candidate_service_names

        candidates = get_candidate_service_names(service_name)
        svc = None
        resolved_service_name = service_name
        for cand in candidates:
            cand_path = f"projects/{project_id}/locations/{region}/services/{cand}"
            try:
                try:
                    cand_svc = client.get_service(name=cand_path)
                except (TypeError, ValueError):
                    cand_svc = client.get_service(request={"name": cand_path})
                if cand_svc is not None:
                    svc = cand_svc
                    resolved_service_name = cand
                    break
            except Exception:
                continue

        if svc is not None:
            result["service_name"] = resolved_service_name
            result["uri"] = getattr(svc, "uri", "")
            result["ingress"] = str(getattr(svc, "ingress", "INGRESS_TRAFFIC_UNSPECIFIED"))

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
    project_id: str,
    location: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List GKE clusters in project and location."""
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
        parent = f"projects/{project_id}/locations/{location}"
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
                    f"{location}/{cluster_name}?project={project_id}"
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
    project_id: str,
    zone: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List Compute Engine instances."""
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
                raw_instances = client.list_instances(project=project_id, zone=zone)
            elif hasattr(client, "list"):
                raw_instances = client.list(project=project_id, zone=zone)
        else:
            if hasattr(client, "list_instances"):
                try:
                    raw_instances = client.list_instances(project=project_id)
                except Exception:
                    pass
            if not raw_instances and hasattr(client, "aggregated_list"):
                try:
                    agg = client.aggregated_list(project=project_id)
                    for _, zone_scoped_list in agg:
                        for item in getattr(zone_scoped_list, "instances", []):
                            raw_instances.append(item)
                except Exception:
                    pass

        for inst in raw_instances:
            inst_name = getattr(inst, "name", "")
            inst_console = (
                f"https://console.cloud.google.com/compute/instancesDetail/zones/"
                f"{zone}/instances/{inst_name}?project={project_id}"
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
    project_id: str,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List Cloud SQL instances for a project."""
    if client is None:
        client = get_cloud_sql_admin_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud SQL Admin client unavailable in production environment")

    instances: List[Dict[str, Any]] = []

    if client is not None:
        if hasattr(client, "list_instances"):
            raw_instances = client.list_instances(project=project_id)
        elif hasattr(client, "instances"):
            resp = client.instances().list(project=project_id).execute()
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
                f"?project={project_id}"
            )
            instances.append(
                {
                    "name": inst_name,
                    "database_version": db_ver,
                    "tier": tier_val,
                    "state": st,
                    "region": reg,
                    "console_url": inst_console,
                }
            )

    return instances
