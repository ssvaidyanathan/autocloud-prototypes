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
            if candidates and not any(cand.lower() in resource_name.lower() for cand in candidates):
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
