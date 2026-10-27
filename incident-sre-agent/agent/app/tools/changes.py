import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from .clients import (
    get_cloud_build_client,
    get_cloud_run_revisions_client,
    get_cloud_run_services_client,
    get_logging_client,
    is_local_mode,
)
from .registry import readonly_tool


def _safe_str(val: Any) -> str:
    """Extract string value safely ignoring MagicMock auto-created attributes."""
    if val is None or hasattr(val, "_mock_name"):
        return ""
    return str(val).strip()


CLOUD_BUILD_STATUS_MAP: Dict[int, str] = {
    0: "STATUS_UNKNOWN",
    1: "QUEUED",
    2: "WORKING",
    3: "SUCCESS",
    4: "FAILURE",
    5: "INTERNAL_ERROR",
    6: "TIMEOUT",
    7: "CANCELLED",
    8: "EXPIRED",
}


def normalize_cloud_build_status(raw_status: Any) -> str:
    """Normalize Cloud Build status (int, enum, or string) to standard human-readable name."""
    if raw_status is None or hasattr(raw_status, "_mock_name"):
        return "STATUS_UNKNOWN"
    if hasattr(raw_status, "name") and isinstance(raw_status.name, str):
        return raw_status.name
    if isinstance(raw_status, int) and not isinstance(raw_status, bool):
        return CLOUD_BUILD_STATUS_MAP.get(raw_status, f"STATUS_{raw_status}")
    if isinstance(raw_status, str):
        cleaned = raw_status.strip()
        if cleaned.isdigit():
            return CLOUD_BUILD_STATUS_MAP.get(int(cleaned), f"STATUS_{cleaned}")
        if cleaned:
            if "." in cleaned:
                cleaned = cleaned.split(".")[-1]
            return cleaned
    return "STATUS_UNKNOWN"


@readonly_tool(
    name="query_audit_logs",
    description="Query Cloud Audit Logs for admin activity, deploys, and config changes.",
)
def query_audit_logs(
    project_id: Optional[str] = None,
    start_time: Optional[Union[datetime, str]] = None,
    end_time: Optional[Union[datetime, str]] = None,
    service_name: Optional[str] = None,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """Query Cloud Audit Logs activity for admin changes."""
    from datetime import timedelta

    from .common import normalize_datetime, validate_service
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    if service_name:
        validate_service(service_name)

    end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)
    start_time_dt = normalize_datetime(start_time) or (end_time_dt - timedelta(hours=1))

    if client is None:
        client = get_logging_client(pinned_project)

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Logging client unavailable in production environment")

    start_iso = start_time_dt.isoformat()
    end_iso = end_time_dt.isoformat()

    filter_parts = [
        f'logName = "projects/{pinned_project}/logs/cloudaudit.googleapis.com%2Factivity"',
        f'timestamp >= "{start_iso}"',
        f'timestamp <= "{end_iso}"',
    ]
    candidates: List[str] = []
    if service_name:
        from .common import get_candidate_service_names

        candidates = get_candidate_service_names(service_name)
        svc_clauses = []
        for cand in candidates:
            svc_clauses.append(f'protoPayload.serviceName = "{cand}"')
            svc_clauses.append(f'protoPayload.resourceName =~ "{cand}"')
        svc_clauses.append('protoPayload.serviceName = "k8s.io"')
        svc_clauses = list(dict.fromkeys(svc_clauses))
        filter_parts.append(f"({' OR '.join(svc_clauses)})")

    final_filter = " AND ".join(filter_parts)
    encoded_filter = urllib.parse.quote(final_filter)
    console_url = f"https://console.cloud.google.com/logs/query;query={encoded_filter};project={pinned_project}"

    results: List[Dict[str, Any]] = []

    if client is not None:
        raw_entries = client.list_entries(
            resource_names=[f"projects/{pinned_project}"],
            filter_=final_filter,
            page_size=50,
        )

        for idx, entry in enumerate(raw_entries):
            if idx >= 50:
                break
            proto_payload = None
            if "proto_payload" in getattr(entry, "__dict__", {}):
                proto_payload = entry.proto_payload
            elif hasattr(entry, "payload") and not hasattr(entry.payload, "_mock_name"):
                proto_payload = entry.payload
            elif hasattr(entry, "proto_payload"):
                proto_payload = entry.proto_payload
            elif hasattr(entry, "payload"):
                proto_payload = entry.payload
            elif isinstance(entry, dict):
                proto_payload = entry.get("protoPayload") or entry.get("proto_payload")

            method_name = ""
            resource_name = ""
            principal_email = ""
            event_service_name = ""
            status_code = 0

            if isinstance(proto_payload, dict):
                method_name = _safe_str(
                    proto_payload.get("methodName") or proto_payload.get("method_name")
                )
                resource_name = _safe_str(
                    proto_payload.get("resourceName") or proto_payload.get("resource_name")
                )
                event_service_name = _safe_str(
                    proto_payload.get("serviceName") or proto_payload.get("service_name")
                )
                auth_info = (
                    proto_payload.get("authenticationInfo")
                    or proto_payload.get("authentication_info")
                    or {}
                )
                if isinstance(auth_info, dict):
                    principal_email = _safe_str(
                        auth_info.get("principalEmail") or auth_info.get("principal_email")
                    )
                else:
                    principal_email = _safe_str(
                        getattr(auth_info, "principal_email", None)
                        or getattr(auth_info, "principalEmail", None)
                    )
                status_obj = proto_payload.get("status", {})
                raw_code = (
                    status_obj.get("code", 0)
                    if isinstance(status_obj, dict)
                    else getattr(status_obj, "code", 0)
                )
                status_code = (
                    int(raw_code)
                    if isinstance(raw_code, (int, float)) and not hasattr(raw_code, "_mock_name")
                    else 0
                )
            elif proto_payload is not None:
                method_name = _safe_str(
                    getattr(proto_payload, "method_name", None)
                    or getattr(proto_payload, "methodName", None)
                )
                resource_name = _safe_str(
                    getattr(proto_payload, "resource_name", None)
                    or getattr(proto_payload, "resourceName", None)
                )
                event_service_name = _safe_str(
                    getattr(proto_payload, "service_name", None)
                    or getattr(proto_payload, "serviceName", None)
                )
                auth_info = getattr(proto_payload, "authentication_info", None) or getattr(
                    proto_payload, "authenticationInfo", None
                )
                if isinstance(auth_info, dict):
                    principal_email = _safe_str(
                        auth_info.get("principalEmail") or auth_info.get("principal_email")
                    )
                elif auth_info is not None:
                    principal_email = _safe_str(
                        getattr(auth_info, "principal_email", None)
                        or getattr(auth_info, "principalEmail", None)
                    )
                status_obj = getattr(proto_payload, "status", None)
                raw_code = getattr(status_obj, "code", 0) if status_obj else 0
                status_code = (
                    int(raw_code)
                    if isinstance(raw_code, (int, float)) and not hasattr(raw_code, "_mock_name")
                    else 0
                )

            # Fallbacks if proto_payload attributes were missing
            if not method_name and isinstance(entry, dict):
                method_name = _safe_str(entry.get("methodName") or entry.get("method_name"))
            if not resource_name and isinstance(entry, dict):
                resource_name = _safe_str(entry.get("resourceName") or entry.get("resource_name"))
            if not event_service_name and isinstance(entry, dict):
                event_service_name = _safe_str(
                    entry.get("serviceName") or entry.get("service_name")
                )
            if not principal_email:
                principal_email = _safe_str(
                    getattr(entry, "principal_email", None)
                    or (entry.get("principal_email") if isinstance(entry, dict) else None)
                )

            ts = getattr(entry, "timestamp", None)
            if ts is not None and hasattr(ts, "isoformat"):
                ts_str = str(ts.isoformat())
            else:
                ts_str = str(ts or "")

            # Filter out internal GKE autoscaling, network controller, and K8s coordination noise
            infra_noise = [
                "kube-system",
                "system:cluster-autoscaler",
                "cluster-autoscaler",
                "gke-system",
                "anet-controller-manager",
                "coordination.k8s.io",
                "system:serviceaccount:kube-system",
                "leases",
                "endpointslices",
            ]
            check_fields = (
                f"{resource_name} {event_service_name} {principal_email} {method_name}".lower()
            )
            if any(k.lower() in check_fields for k in infra_noise):
                continue

            # Ensure returned audit events match candidate services in resource_name
            # or k8s.io audit activity
            if (
                candidates
                and event_service_name != "k8s.io"
                and not any(cand.lower() in resource_name.lower() for cand in candidates)
            ):
                continue

            results.append(
                {
                    "timestamp": ts_str,
                    "principal_email": principal_email,
                    "method_name": method_name,
                    "resource_name": resource_name,
                    "status_code": status_code,
                    "console_url": console_url,
                }
            )

    return results


@readonly_tool(
    name="list_cloud_run_revisions",
    description="List recent revisions for a Cloud Run service with creation times and traffic.",
)
def list_cloud_run_revisions(
    project_id: Optional[str] = None,
    service_name: Optional[str] = None,
    region: Optional[str] = None,
    client: Any = None,
    services_client: Any = None,
) -> List[Dict[str, Any]]:
    """List recent Cloud Run revisions and active traffic allocations."""
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

    console_url = (
        f"https://console.cloud.google.com/run/detail/{region}/{resolved_service}/revisions"
        f"?project={pinned_project}"
    )

    if client is None:
        client = get_cloud_run_revisions_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud Run revisions client unavailable in production environment")

    if services_client is None:
        services_client = get_cloud_run_services_client()

    traffic_map: Dict[str, int] = {}
    if services_client is not None:
        service_path = f"projects/{pinned_project}/locations/{region}/services/{resolved_service}"
        try:
            svc = services_client.get_service(name=service_path)
            traffic_list = getattr(svc, "traffic_statuses", None) or getattr(svc, "traffic", None)
            if not traffic_list and isinstance(svc, dict):
                traffic_list = (
                    svc.get("traffic_statuses")
                    or svc.get("trafficStatuses")
                    or svc.get("traffic", [])
                )
            if traffic_list:
                for t in traffic_list:
                    rev_ref = getattr(t, "revision", "") or (
                        t.get("revision", "") if isinstance(t, dict) else ""
                    )
                    pct = getattr(t, "percent", 0) or (
                        t.get("percent", 0) if isinstance(t, dict) else 0
                    )
                    if rev_ref:
                        traffic_map[rev_ref] = int(pct)
                        traffic_map[rev_ref.split("/")[-1]] = int(pct)
        except Exception:
            pass

    revisions: List[Dict[str, Any]] = []

    if client is not None:
        from .common import get_candidate_service_names

        candidates = get_candidate_service_names(resolved_service)
        raw_revisions: Any = []
        for cand in candidates:
            parent = f"projects/{pinned_project}/locations/{region}/services/{cand}"
            try:
                raw_revisions = list(client.list_revisions(parent=parent))
                if raw_revisions:
                    break
            except Exception:
                continue

        for rev in raw_revisions:
            rev_name = getattr(rev, "name", "")
            create_time = getattr(rev, "create_time", None)
            if create_time is not None and hasattr(create_time, "isoformat"):
                create_time_str = str(create_time.isoformat())
            else:
                create_time_str = str(create_time or "")

            # Robust author extraction across creator field, annotations, and metadata
            author = getattr(rev, "creator", "")
            if not author:
                annotations = getattr(rev, "annotations", None)
                if isinstance(annotations, dict):
                    author = annotations.get("run.googleapis.com/creator", "") or annotations.get(
                        "serving.knative.dev/creator", ""
                    )
                elif annotations is not None and hasattr(annotations, "get"):
                    author = annotations.get("run.googleapis.com/creator", "") or annotations.get(
                        "serving.knative.dev/creator", ""
                    )
            if not author and hasattr(rev, "metadata"):
                meta = getattr(rev, "metadata", None)
                if meta:
                    author = getattr(meta, "author", "") or getattr(meta, "creator", "")
                    annos = getattr(meta, "annotations", {})
                    if isinstance(annos, dict) and not author:
                        author = annos.get("run.googleapis.com/creator", "") or annos.get(
                            "serving.knative.dev/creator", ""
                        )
            if not author and isinstance(rev, dict):
                author = rev.get("creator", "") or rev.get("author", "")
                annos = rev.get("annotations", {})
                if isinstance(annos, dict) and not author:
                    author = annos.get("run.googleapis.com/creator", "") or annos.get(
                        "serving.knative.dev/creator", ""
                    )

            short_name = rev_name.split("/")[-1] if "/" in rev_name else rev_name
            raw_pct = getattr(rev, "traffic_percent", 0)
            default_pct = (
                int(raw_pct)
                if isinstance(raw_pct, (int, float)) and not hasattr(raw_pct, "_mock_name")
                else 0
            )
            traffic_percent = traffic_map.get(rev_name, traffic_map.get(short_name, default_pct))

            template = getattr(rev, "template", None)
            containers = getattr(
                template,
                "containers",
                getattr(rev, "containers", []),
            )
            image = ""
            if containers and len(containers) > 0:
                c0 = containers[0]
                if not isinstance(c0, dict):
                    image = getattr(c0, "image", "")
                else:
                    image = c0.get("image", "")

            revisions.append(
                {
                    "name": short_name,
                    "full_name": rev_name,
                    "creation_timestamp": create_time_str,
                    "author": author,
                    "traffic_percent": traffic_percent,
                    "image": image,
                    "console_url": console_url,
                }
            )

    return revisions


@readonly_tool(
    name="list_cloud_builds",
    description="List recent Cloud Build builds, status, start/finish times, and commit SHAs.",
)
def list_cloud_builds(
    project_id: Optional[str] = None,
    limit: int = 10,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List recent Cloud Build builds with commit info and console links."""
    from .registry import get_pinned_project_id

    pinned_project = get_pinned_project_id(project_id)
    console_url = f"https://console.cloud.google.com/cloud-build/builds?project={pinned_project}"

    if client is None:
        client = get_cloud_build_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud Build client unavailable in production environment")

    builds: List[Dict[str, Any]] = []

    if client is not None:
        try:
            raw_builds = client.list_builds(
                project_id=pinned_project,
                page_size=limit,
            )
        except (TypeError, ValueError):
            raw_builds = client.list_builds(
                request={"project_id": pinned_project, "page_size": limit}
            )

        for idx, b in enumerate(raw_builds):
            if idx >= limit:
                break
            build_id = _safe_str(
                b.get("id") or b.get("build_id")
                if isinstance(b, dict)
                else getattr(b, "id", getattr(b, "build_id", ""))
            )
            raw_status = (
                b.get("status") if isinstance(b, dict) else getattr(b, "status", "STATUS_UNKNOWN")
            )
            status = normalize_cloud_build_status(raw_status)
            create_time = (
                b.get("create_time") if isinstance(b, dict) else getattr(b, "create_time", None)
            )
            finish_time = (
                b.get("finish_time") if isinstance(b, dict) else getattr(b, "finish_time", None)
            )
            if create_time is not None and hasattr(create_time, "isoformat"):
                create_str = str(create_time.isoformat())
            else:
                create_str = str(create_time or "")
            if finish_time is not None and hasattr(finish_time, "isoformat"):
                finish_str = str(finish_time.isoformat())
            else:
                finish_str = str(finish_time or "")

            commit_sha = ""
            if isinstance(b, dict):
                commit_sha = _safe_str(b.get("commit_sha"))
                if not commit_sha:
                    sp = b.get("source_provenance") or b.get("sourceProvenance") or {}
                    rs = sp.get("resolved_repo_source") or sp.get("resolvedRepoSource") or {}
                    commit_sha = _safe_str(rs.get("commit_sha") or rs.get("commitSha"))
            else:
                source_prov = getattr(b, "source_provenance", None) or getattr(
                    b, "sourceProvenance", None
                )
                if source_prov:
                    resolved_source = getattr(source_prov, "resolved_repo_source", None) or getattr(
                        source_prov, "resolvedRepoSource", None
                    )
                    if resolved_source:
                        commit_sha = _safe_str(getattr(resolved_source, "commit_sha", ""))
                if not commit_sha:
                    commit_sha = _safe_str(getattr(b, "commit_sha", ""))

            log_url = _safe_str(
                b.get("log_url") if isinstance(b, dict) else getattr(b, "log_url", "")
            )

            # Extract images, substitutions, and tags
            images: List[str] = []
            raw_images = b.get("images") if isinstance(b, dict) else getattr(b, "images", None)
            if raw_images and not hasattr(raw_images, "_mock_name"):
                try:
                    for img in raw_images:
                        img_str = _safe_str(img)
                        if img_str and img_str not in images:
                            images.append(img_str)
                except (TypeError, AttributeError):
                    pass

            results = b.get("results") if isinstance(b, dict) else getattr(b, "results", None)
            if results and not hasattr(results, "_mock_name"):
                res_images = (
                    results.get("images")
                    if isinstance(results, dict)
                    else getattr(results, "images", None)
                )
                if res_images and not hasattr(res_images, "_mock_name"):
                    try:
                        for img in res_images:
                            if isinstance(img, str):
                                img_name = img.strip()
                            elif isinstance(img, dict):
                                img_name = _safe_str(img.get("name"))
                            else:
                                img_name = _safe_str(getattr(img, "name", ""))
                            if img_name and img_name not in images:
                                images.append(img_name)
                    except (TypeError, AttributeError):
                        pass

            substitutions: Dict[str, str] = {}
            raw_subs = (
                b.get("substitutions") if isinstance(b, dict) else getattr(b, "substitutions", None)
            )
            if raw_subs and not hasattr(raw_subs, "_mock_name"):
                if hasattr(raw_subs, "items"):
                    try:
                        for k, v in raw_subs.items():
                            k_str = _safe_str(k)
                            v_str = _safe_str(v)
                            if k_str:
                                substitutions[k_str] = v_str
                    except (TypeError, AttributeError):
                        pass

            tags: List[str] = []
            raw_tags = b.get("tags") if isinstance(b, dict) else getattr(b, "tags", None)
            if raw_tags and not hasattr(raw_tags, "_mock_name"):
                try:
                    for tag in raw_tags:
                        tag_str = _safe_str(tag)
                        if tag_str and tag_str not in tags:
                            tags.append(tag_str)
                except (TypeError, AttributeError):
                    pass

            builds.append(
                {
                    "build_id": build_id,
                    "status": status,
                    "create_time": create_str,
                    "finish_time": finish_str,
                    "commit_sha": commit_sha,
                    "log_url": log_url,
                    "images": images,
                    "substitutions": substitutions,
                    "tags": tags,
                    "console_url": console_url,
                }
            )

    return builds


def _extract_revision_spec(rev: Any) -> Dict[str, Any]:
    """Extract container image, env var names (ONLY names), limits, probes, and SA."""
    spec: Dict[str, Any] = {
        "image": "",
        "env_var_names": set(),
        "cpu_limit": "",
        "memory_limit": "",
        "concurrency": 0,
        "probes": {},
        "service_account": "",
    }
    if rev is None:
        return spec

    template = (
        getattr(rev, "template", None)
        or (rev.get("template") if isinstance(rev, dict) else None)
        or rev
    )
    containers = (
        getattr(template, "containers", None)
        or (template.get("containers") if isinstance(template, dict) else None)
        or getattr(rev, "containers", None)
        or (rev.get("containers") if isinstance(rev, dict) else [])
    )
    if hasattr(containers, "_mock_name"):
        containers = []

    sa = (
        getattr(template, "service_account", None)
        or getattr(template, "service_account_name", None)
        or getattr(template, "serviceAccountName", None)
        or (template.get("service_account") if isinstance(template, dict) else None)
        or (template.get("serviceAccountName") if isinstance(template, dict) else None)
        or getattr(rev, "service_account", None)
        or getattr(rev, "serviceAccountName", None)
        or ""
    )
    if sa and not hasattr(sa, "_mock_name"):
        spec["service_account"] = str(sa).strip()

    concurrency = (
        getattr(template, "max_instance_request_concurrency", None)
        or getattr(template, "container_concurrency", None)
        or getattr(template, "containerConcurrency", None)
        or (
            template.get("max_instance_request_concurrency") if isinstance(template, dict) else None
        )
        or (template.get("containerConcurrency") if isinstance(template, dict) else None)
        or getattr(rev, "max_instance_request_concurrency", 0)
    )
    if isinstance(concurrency, (int, float)) and not hasattr(concurrency, "_mock_name"):
        spec["concurrency"] = int(concurrency)

    if containers and len(containers) > 0:
        images: List[str] = []
        cpu_limits: List[str] = []
        mem_limits: List[str] = []
        all_env_names: set[str] = set()
        probes_map: Dict[str, Any] = {}

        for c0 in containers:
            img = (
                getattr(c0, "image", None)
                or (c0.get("image") if isinstance(c0, dict) else None)
                or ""
            )
            if img and not hasattr(img, "_mock_name"):
                img_str = str(img).strip()
                if img_str and img_str not in images:
                    images.append(img_str)

            resources = getattr(c0, "resources", None) or (
                c0.get("resources") if isinstance(c0, dict) else None
            )
            if resources is not None:
                limits = getattr(resources, "limits", None) or (
                    resources.get("limits") if isinstance(resources, dict) else None
                )
                if limits is not None and not hasattr(limits, "_mock_name"):
                    if isinstance(limits, dict) or hasattr(limits, "get"):
                        cpu_val = limits.get("cpu", "")
                        mem_val = limits.get("memory", "")
                    else:
                        cpu_val = getattr(limits, "cpu", "")
                        mem_val = getattr(limits, "memory", "")
                    if cpu_val and not hasattr(cpu_val, "_mock_name"):
                        cpu_limits.append(str(cpu_val).strip())
                    if mem_val and not hasattr(mem_val, "_mock_name"):
                        mem_limits.append(str(mem_val).strip())

            # CRITICAL SECURITY GUARANTEE: Extract ONLY variable names, never values
            env_items = getattr(c0, "env", None) or (c0.get("env") if isinstance(c0, dict) else [])
            if env_items and not hasattr(env_items, "_mock_name"):
                for item in env_items:
                    if isinstance(item, dict):
                        name_val = item.get("name")
                    else:
                        name_val = getattr(item, "name", None)
                    if name_val and not hasattr(name_val, "_mock_name"):
                        all_env_names.add(str(name_val).strip())

            probe_keys = [
                ("startup_probe", ["startup_probe", "startupProbe"]),
                ("liveness_probe", ["liveness_probe", "livenessProbe"]),
                ("readiness_probe", ["readiness_probe", "readinessProbe"]),
            ]
            for canon_name, attrs in probe_keys:
                p_obj = None
                for attr in attrs:
                    val = getattr(c0, attr, None) or (
                        c0.get(attr) if isinstance(c0, dict) else None
                    )
                    if val is not None:
                        if (
                            hasattr(val, "http_get")
                            or hasattr(val, "httpGet")
                            or isinstance(val, dict)
                        ):
                            p_obj = val
                            break
                if p_obj is not None:
                    http_get = (
                        getattr(p_obj, "http_get", None)
                        or getattr(p_obj, "httpGet", None)
                        or (p_obj.get("httpGet") if isinstance(p_obj, dict) else None)
                        or (p_obj.get("http_get") if isinstance(p_obj, dict) else None)
                    )
                    path = ""
                    port = 0
                    if http_get is not None:
                        raw_path = getattr(http_get, "path", "") or (
                            http_get.get("path", "") if isinstance(http_get, dict) else ""
                        )
                        if isinstance(raw_path, str) and not hasattr(raw_path, "_mock_name"):
                            path = str(raw_path).strip()
                        raw_port = getattr(http_get, "port", 0) or (
                            http_get.get("port", 0) if isinstance(http_get, dict) else 0
                        )
                        if isinstance(raw_port, (int, float)) and not hasattr(
                            raw_port, "_mock_name"
                        ):
                            port = int(raw_port)

                    period = 0
                    raw_period = (
                        getattr(p_obj, "period_seconds", None)
                        or getattr(p_obj, "periodSeconds", None)
                        or (p_obj.get("periodSeconds", 0) if isinstance(p_obj, dict) else 0)
                    )
                    if isinstance(raw_period, (int, float)) and not hasattr(
                        raw_period, "_mock_name"
                    ):
                        period = int(raw_period)

                    timeout = 0
                    raw_timeout = (
                        getattr(p_obj, "timeout_seconds", None)
                        or getattr(p_obj, "timeoutSeconds", None)
                        or (p_obj.get("timeoutSeconds", 0) if isinstance(p_obj, dict) else 0)
                    )
                    if isinstance(raw_timeout, (int, float)) and not hasattr(
                        raw_timeout, "_mock_name"
                    ):
                        timeout = int(raw_timeout)

                    failure = 0
                    raw_failure = (
                        getattr(p_obj, "failure_threshold", None)
                        or getattr(p_obj, "failureThreshold", None)
                        or (p_obj.get("failureThreshold", 0) if isinstance(p_obj, dict) else 0)
                    )
                    if isinstance(raw_failure, (int, float)) and not hasattr(
                        raw_failure, "_mock_name"
                    ):
                        failure = int(raw_failure)

                    if path or port or period or timeout or failure:
                        if canon_name not in probes_map:
                            probes_map[canon_name] = {
                                "path": path,
                                "port": port,
                                "period_seconds": int(period)
                                if isinstance(period, (int, float))
                                and not hasattr(period, "_mock_name")
                                else 0,
                                "timeout_seconds": int(timeout)
                                if isinstance(timeout, (int, float))
                                and not hasattr(timeout, "_mock_name")
                                else 0,
                                "failure_threshold": int(failure)
                                if isinstance(failure, (int, float))
                                and not hasattr(failure, "_mock_name")
                                else 0,
                            }

        spec["image"] = ", ".join(images) if images else ""
        spec["cpu_limit"] = ", ".join(cpu_limits) if cpu_limits else ""
        spec["memory_limit"] = ", ".join(mem_limits) if mem_limits else ""
        spec["env_var_names"] = all_env_names
        spec["probes"] = probes_map

    return spec


@readonly_tool(
    name="diff_revisions",
    description=(
        "Diff two Cloud Run revisions comparing image, env var names "
        "(values redacted), limits, probes, and service account."
    ),
)
def diff_revisions(
    service_name: str,
    rev_a: str,
    rev_b: str,
    project_id: Optional[str] = None,
    region: Optional[str] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Diff two Cloud Run revisions strictly exposing env var names only and redacting secrets."""
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

    console_url = (
        f"https://console.cloud.google.com/run/detail/{region}/{resolved_service}/revisions"
        f"?project={pinned_project}"
    )

    if client is None:
        client = get_cloud_run_revisions_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Cloud Run revisions client unavailable in production environment")

    def _fetch_revision(rev_target: str) -> Any:
        if client is None:
            return None
        short_name = rev_target.split("/")[-1] if "/" in rev_target else rev_target
        # Try direct invocation with target or candidate path
        for candidate_name in [
            rev_target,
            f"projects/{pinned_project}/locations/{region}/services/{resolved_service}/revisions/{short_name}",
            short_name,
        ]:
            try:
                try:
                    res = client.get_revision(name=candidate_name)
                except (TypeError, ValueError):
                    res = client.get_revision(request={"name": candidate_name})
                if res is not None:
                    return res
            except Exception:
                pass
        try:
            res = client.get_revision(rev_target)
            if res is not None:
                return res
        except Exception:
            pass

        from .common import get_candidate_service_names

        for cand in get_candidate_service_names(resolved_service):
            cand_name = (
                f"projects/{pinned_project}/locations/{region}/"
                f"services/{cand}/revisions/{short_name}"
            )
            try:
                try:
                    res = client.get_revision(name=cand_name)
                except (TypeError, ValueError):
                    res = client.get_revision(request={"name": cand_name})
                if res is not None:
                    return res
            except Exception:
                pass
        return None

    rev_a_obj = _fetch_revision(rev_a)
    rev_b_obj = _fetch_revision(rev_b)

    spec_a = _extract_revision_spec(rev_a_obj)
    spec_b = _extract_revision_spec(rev_b_obj)

    image_changed = spec_a["image"] != spec_b["image"]
    env_vars_added = sorted(list(spec_b["env_var_names"] - spec_a["env_var_names"]))
    env_vars_removed = sorted(list(spec_a["env_var_names"] - spec_b["env_var_names"]))
    limits_changed = (
        spec_a["cpu_limit"] != spec_b["cpu_limit"]
        or spec_a["memory_limit"] != spec_b["memory_limit"]
        or spec_a["concurrency"] != spec_b["concurrency"]
    )
    probes_changed = spec_a["probes"] != spec_b["probes"]
    sa_changed = spec_a["service_account"] != spec_b["service_account"]

    summary_parts = []
    if image_changed:
        summary_parts.append(f"Image changed ({spec_a['image']} -> {spec_b['image']})")
    else:
        summary_parts.append("Image unchanged")

    if env_vars_added or env_vars_removed:
        if env_vars_added:
            summary_parts.append(f"Env vars added: {env_vars_added}")
        if env_vars_removed:
            summary_parts.append(f"Env vars removed: {env_vars_removed}")
    else:
        summary_parts.append("Env vars unchanged")

    if limits_changed:
        summary_parts.append(
            f"Limits changed (CPU: {spec_a['cpu_limit']}->{spec_b['cpu_limit']}, "
            f"Mem: {spec_a['memory_limit']}->{spec_b['memory_limit']}, "
            f"Concurrency: {spec_a['concurrency']}->{spec_b['concurrency']})"
        )
    else:
        summary_parts.append("Limits unchanged")

    if probes_changed:
        summary_parts.append("Probes changed")
    else:
        summary_parts.append("Probes unchanged")

    if sa_changed:
        summary_parts.append(
            f"Service account changed ({spec_a['service_account']} -> {spec_b['service_account']})"
        )
    else:
        summary_parts.append("Service account unchanged")

    diff_summary = "; ".join(summary_parts)

    differences: Dict[str, Any] = {
        "image_changed": image_changed,
        "image_details": {"rev_a": spec_a["image"], "rev_b": spec_b["image"]},
        "env_vars_added": env_vars_added,
        "env_vars_removed": env_vars_removed,
        "limits_changed": limits_changed,
        "limits_details": {
            "rev_a": {
                "cpu": spec_a["cpu_limit"],
                "memory": spec_a["memory_limit"],
                "concurrency": spec_a["concurrency"],
            },
            "rev_b": {
                "cpu": spec_b["cpu_limit"],
                "memory": spec_b["memory_limit"],
                "concurrency": spec_b["concurrency"],
            },
        },
        "probes_changed": probes_changed,
        "probes_details": {"rev_a": spec_a["probes"], "rev_b": spec_b["probes"]},
        "service_account_changed": sa_changed,
        "service_account_details": {
            "rev_a": spec_a["service_account"],
            "rev_b": spec_b["service_account"],
        },
    }

    return {
        "service_name": resolved_service,
        "rev_a": rev_a,
        "rev_b": rev_b,
        "diff_summary": diff_summary,
        "differences": differences,
        "console_url": console_url,
    }
