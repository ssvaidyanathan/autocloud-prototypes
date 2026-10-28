"""FastMCP Read-Only Telemetry Server for Alert Triage Agent (ata-tools).

Exposes 7 strictly read-only tools for telemetry, topology, and change inspection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import os
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET
import zipfile

from fastmcp import FastMCP
from pydantic import BaseModel, Field
import requests
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

try:
    from tools.app.config import get_tools_config
    from tools.app.contracts.alert import AlertEvent, is_ata_alert_payload
    from tools.app.bundle_parser import parse_proxy_bundle_bytes
except ImportError:
    from app.config import get_tools_config
    from app.contracts.alert import AlertEvent, is_ata_alert_payload
    from app.bundle_parser import parse_proxy_bundle_bytes

from topology.topology_graph import (
    get_topology_graph as topology_get_graph,
    get_topology as topology_get_topology,
    get_blast_radius as topology_get_blast_radius,
    calculate_hop_distance,
)

# Initialize FastMCP service
mcp = FastMCP(
    name="ata-tools",  # prefix-ok: MCP server identifier (not a deployed resource name)
    instructions="Strictly read-only telemetry, topology, and change inspection server for alert triage.",
)


def _get_auth_headers() -> Dict[str, str]:
    """Retrieve Google OAuth2 application default credentials header."""
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        return {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
    except Exception:
        return {"Content-Type": "application/json"}


class OpenAlertsResult(BaseModel):
    """Result model for list_open_alerts."""
    alerts: List[AlertEvent] = Field(default_factory=list)
    next_page_token: Optional[str] = None
    total_count: int = 0

    def __iter__(self):
        return iter(self.alerts)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self.alerts[item]
        return getattr(self, item)

    def __len__(self):
        return len(self.alerts)


@mcp.tool()
def list_open_alerts(
    page_size: int = 50,
    page_token: Optional[str] = None,
    project_id: Optional[str] = None,
    http_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Queries Cloud Monitoring API GET /v3/projects/{project}/alerts?filter=state="OPEN" with pagination.
    
    Parses active alert incidents into AlertEvent contract instances.
    """
    cfg = get_tools_config()
    proj = project_id or cfg.project_id
    url = f"https://monitoring.googleapis.com/v3/projects/{proj}/alerts"
    params: Dict[str, Any] = {
        "filter": 'state="OPEN"',
        "pageSize": page_size,
    }
    if page_token:
        params["pageToken"] = page_token

    if http_client is not None:
        resp = http_client.get(url, params=params)
        data = resp.json()
    else:
        headers = _get_auth_headers()
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

    raw_alerts = data.get("alerts", [])
    scoped_raw_alerts = [raw for raw in raw_alerts if is_ata_alert_payload(raw)]
    parsed_alerts = [AlertEvent.from_monitoring_payload(raw) for raw in scoped_raw_alerts]
    
    return {
        "alerts": [alert.model_dump() for alert in parsed_alerts],
        "next_page_token": data.get("nextPageToken"),
        "total_count": len(parsed_alerts),
    }


@mcp.tool()
def get_topology_graph() -> Dict[str, Any]:
    """Returns the full directed DAG nodes and caller -> callee edges."""
    return topology_get_graph()


@mcp.tool()
def get_topology(resource: Optional[str] = None) -> Dict[str, Any]:
    """Returns components, edges, and the resource's upstream and downstream dependencies from TOPOLOGY.md."""
    return topology_get_topology(resource)


@mcp.tool()
def get_blast_radius(origin: str) -> Dict[str, Any]:
    """Computes downstream reachable nodes and hop distances from an origin component."""
    impacted = topology_get_blast_radius(origin)
    hops: Dict[str, int] = {}
    for node in impacted:
        dist = calculate_hop_distance(origin, node)
        hops[node] = dist if dist is not None else -1

    return {
        "origin": origin,
        "downstream_nodes": impacted,
        "impacted_nodes": impacted,
        "hops": hops,
        "total_impacted": len(impacted),
    }


def _extract_metric_point_value(pt_value: Any) -> Any:
    """Extracts scalar or distribution value from a monitoring Metric Point TypedValue."""
    from google.cloud import monitoring_v3

    # 1. proto-plus TypedValue
    if isinstance(pt_value, monitoring_v3.TypedValue):
        if "distribution_value" in pt_value:
            return {
                "mean": pt_value.distribution_value.mean,
                "count": pt_value.distribution_value.count,
            }
        elif "int64_value" in pt_value:
            return pt_value.int64_value
        elif "double_value" in pt_value:
            return pt_value.double_value
        elif "bool_value" in pt_value:
            return pt_value.bool_value
        elif "string_value" in pt_value:
            return pt_value.string_value
        return 0.0

    # 2. Protobuf pb2 fallback
    if hasattr(pt_value, "HasField"):
        try:
            if pt_value.HasField("distribution_value"):
                return {
                    "mean": pt_value.distribution_value.mean,
                    "count": pt_value.distribution_value.count,
                }
            if pt_value.HasField("int64_value"):
                return pt_value.int64_value
            if pt_value.HasField("double_value"):
                return pt_value.double_value
        except (ValueError, TypeError):
            pass

    # 3. Fallback for mock objects / generic structures
    dist = getattr(pt_value, "distribution_value", None)
    if dist is not None:
        byte_size = getattr(dist, "ByteSize", None)
        if callable(byte_size) and byte_size() > 0:
            return {
                "mean": getattr(dist, "mean", 0.0),
                "count": getattr(dist, "count", 0),
            }
        mean = getattr(dist, "mean", None)
        count = getattr(dist, "count", None)
        if isinstance(mean, (int, float)) or (isinstance(count, int) and not isinstance(count, bool)):
            return {
                "mean": mean if isinstance(mean, (int, float)) else 0.0,
                "count": count if isinstance(count, int) else 0,
            }

    int_val = getattr(pt_value, "int64_value", None)
    if int_val is not None and isinstance(int_val, int) and not isinstance(int_val, bool):
        return int_val

    dbl_val = getattr(pt_value, "double_value", None)
    if dbl_val is not None and isinstance(dbl_val, (int, float)) and not isinstance(dbl_val, bool):
        return dbl_val

    return 0.0


@mcp.tool()
def get_apigee_metrics(
    proxy_name: str,
    metric_type: str = "proxy/response_count",
    window_minutes: int = 15,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Queries Cloud Monitoring for Apigee metric descriptors (proxy/response_count or target/latencies)."""
    from google.cloud import monitoring_v3

    cfg = get_tools_config()
    proj = project_id or cfg.project_id

    full_metric = metric_type
    if not full_metric.startswith("apigee.googleapis.com/"):
        full_metric = f"apigee.googleapis.com/{metric_type}"

    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": int(now.timestamp())},
        start_time={"seconds": int((now - timedelta(minutes=window_minutes)).timestamp())},
    )

    filter_str = f'metric.type = "{full_metric}" AND resource.labels.proxy_name = "{proxy_name}"'

    series_list: List[Dict[str, Any]] = []
    try:
        client = monitoring_v3.MetricServiceClient()
        req = monitoring_v3.ListTimeSeriesRequest(
            name=f"projects/{proj}",
            filter=filter_str,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )
        for ts in client.list_time_series(request=req):
            points = []
            for pt in ts.points:
                val = _extract_metric_point_value(pt.value)
                time_str = (
                    pt.interval.end_time.ToJsonString()
                    if hasattr(pt.interval.end_time, "ToJsonString")
                    else (
                        pt.interval.end_time.rfc3339()
                        if hasattr(pt.interval.end_time, "rfc3339")
                        else str(pt.interval.end_time)
                    )
                )
                points.append({
                    "time": time_str,
                    "value": val,
                })
            series_list.append({
                "metric_labels": dict(ts.metric.labels),
                "resource_labels": dict(ts.resource.labels),
                "points": points,
            })
    except Exception as e:
        return {
            "proxy_name": proxy_name,
            "metric_type": full_metric,
            "window_minutes": window_minutes,
            "error": str(e),
            "series": [],
        }

    return {
        "proxy_name": proxy_name,
        "metric_type": full_metric,
        "window_minutes": window_minutes,
        "series": series_list,
        "total_series": len(series_list),
    }


@mcp.tool()
def get_apigee_logs(
    proxy_name: str,
    filter_str: Optional[str] = None,
    max_entries: int = 50,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Queries Cloud Logging for access log entries in projects/{project}/logs/{NAME_PREFIX}-apigee-access."""
    import google.cloud.logging

    cfg = get_tools_config()
    proj = project_id or cfg.project_id

    log_name = f"projects/{proj}/logs/{cfg.apigee_access_log_id}"
    filter_clauses = [f'logName="{log_name}"']
    if proxy_name:
        filter_clauses.append(f'(jsonPayload.proxy_name="{proxy_name}" OR textPayload:"{proxy_name}")')
    if filter_str:
        filter_clauses.append(f"({filter_str})")

    combined_filter = " AND ".join(filter_clauses)
    entries_out: List[Dict[str, Any]] = []

    try:
        client = google.cloud.logging.Client(project=proj)
        for entry in client.list_entries(filter_=combined_filter, max_results=max_entries):
            entries_out.append({
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
                "severity": entry.severity,
                "payload": entry.payload,
                "insert_id": entry.insert_id,
            })
    except Exception as e:
        return {
            "proxy_name": proxy_name,
            "log_name": log_name,
            "filter": combined_filter,
            "error": str(e),
            "entries": [],
        }

    return {
        "proxy_name": proxy_name,
        "log_name": log_name,
        "filter": combined_filter,
        "entries": entries_out,
        "count": len(entries_out),
    }


@mcp.tool()
def get_proxy_bundle(
    api_name: str,
    revision: str = "1",
    org: Optional[str] = None,
    bundle_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Fetches ZIP bundle via Apigee Management API, unzips in-memory, parses target endpoints and SpikeArrest policy XML.
    
    Strictly in-memory parsing with zero local disk writes.
    """
    cfg = get_tools_config()
    effective_org = org or cfg.apigee_org

    try:
        if bundle_bytes is None and not effective_org:
            raise ValueError(f"APIGEE_ORG not set in {cfg.name_prefix}-tools (and no org argument given)")
        if bundle_bytes is None:
            url = f"https://apigee.googleapis.com/v1/organizations/{effective_org}/apis/{api_name}/revisions/{revision}?format=bundle"
            headers = _get_auth_headers()
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            bundle_bytes = resp.content

        return parse_proxy_bundle_bytes(bundle_bytes, api_name=api_name, revision=str(revision))
    except Exception as e:
        return {
            "api_name": api_name,
            "revision": str(revision),
            "error": str(e),
            "target_endpoints": [],
            "spike_arrest_policies": [],
            "policies": [],
            "base_paths": [],
            "proxy_endpoints": [],
        }


@mcp.tool()
def get_change_timeline(
    window_hours: int = 2,
    project_id: Optional[str] = None,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """Queries Cloud Run revision list and Apigee revision history over the past window_hours."""
    cfg = get_tools_config()
    proj = project_id or cfg.project_id
    reg = region or cfg.region
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    changes: List[Dict[str, Any]] = []
    headers = _get_auth_headers()

    # 1. Cloud Run service revisions
    try:
        services_url = f"https://run.googleapis.com/v2/projects/{proj}/locations/{reg}/services"
        resp = requests.get(services_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            changes.append({
                "source": "cloud_run",
                "error": f"Cloud Run Admin API services list returned HTTP {resp.status_code}",
            })
        if resp.status_code == 200:
            services = resp.json().get("services", [])
            for svc in services:
                svc_full_name = svc.get("name", "")
                svc_short = svc_full_name.split("/")[-1]

                # Query revisions to identify newly created revisions and serving status
                rev_url = f"https://run.googleapis.com/v2/{svc_full_name}/revisions"
                rev_resp = requests.get(rev_url, headers=headers, timeout=15)
                new_revisions_in_window = []
                all_service_revs = []
                if rev_resp.status_code != 200:
                    changes.append({
                        "source": "cloud_run",
                        "service_name": svc_short,
                        "error": f"Cloud Run Admin API revisions list returned HTTP {rev_resp.status_code}",
                    })
                if rev_resp.status_code == 200:
                    all_service_revs = rev_resp.json().get("revisions", [])
                    for rev in all_service_revs:
                        ctime_str = rev.get("createTime", "")
                        if ctime_str:
                            try:
                                ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
                                if ctime >= cutoff:
                                    new_revisions_in_window.append(rev)
                            except Exception:
                                pass

                # Determine active serving revision
                traffic_list = svc.get("traffic", []) or []
                serving_rev_names = set()
                for tr in traffic_list:
                    if tr.get("percent", 0) > 0 and tr.get("revision"):
                        serving_rev_names.add(tr["revision"].split("/")[-1])
                if not serving_rev_names:
                    latest_ready = svc.get("latestReadyRevision", "")
                    if latest_ready:
                        serving_rev_names.add(latest_ready.split("/")[-1])

                # Identify previous revision by sorting all revisions descending by createTime
                def _rev_time_key(r: Dict[str, Any]) -> str:
                    return str(r.get("createTime") or "")
                sorted_revs = sorted(all_service_revs, key=_rev_time_key, reverse=True)

                if new_revisions_in_window:
                    for rev in new_revisions_in_window:
                        rev_short = rev.get("name", "").split("/")[-1]
                        ctime_str = rev.get("createTime", "")
                        is_serving = rev_short in serving_rev_names

                        # Find previous revision
                        prev_rev_name = None
                        prev_img = None
                        prev_env = []
                        for idx, r in enumerate(sorted_revs):
                            if r.get("name", "").split("/")[-1] == rev_short and idx + 1 < len(sorted_revs):
                                prev_r = sorted_revs[idx + 1]
                                prev_rev_name = prev_r.get("name", "").split("/")[-1]
                                prev_img = (prev_r.get("containers") or [{}])[0].get("image")
                                prev_env = (prev_r.get("containers") or [{}])[0].get("env", [])
                                break

                        cur_img = (rev.get("containers") or [{}])[0].get("image")
                        cur_env = (rev.get("containers") or [{}])[0].get("env", [])

                        differs = (cur_img != prev_img) or (cur_env != prev_env) or (prev_rev_name is None)
                        is_traffic_shift = not differs

                        changes.append({
                            "source": "cloud_run",
                            "service_name": svc_short,
                            "revision_name": rev_short,
                            "previous_revision": prev_rev_name,
                            "timestamp": ctime_str,
                            "is_serving": is_serving,
                            "differs_from_previous": differs,
                            "is_traffic_shift": is_traffic_shift,
                            "action": "traffic_shift" if is_traffic_shift else "revision_deploy",
                            "details": {
                                "image": cur_img,
                                "prev_image": prev_img,
                                "env_diff": [e for e in cur_env if e not in prev_env],
                                "scaling": rev.get("scaling"),
                                "is_serving": is_serving,
                                "differs_from_previous": differs,
                                "is_traffic_shift": is_traffic_shift,
                                "previous_revision": prev_rev_name,
                            },
                        })
                has_serving_new_rev = any(
                    rev.get("name", "").split("/")[-1] in serving_rev_names
                    for rev in new_revisions_in_window
                )
                if not has_serving_new_rev and svc.get("updateTime"):
                    # Service was updated (e.g. traffic repointed) but no new revision was created in window
                    svc_update_str = svc.get("updateTime", "")
                    try:
                        svc_dt = datetime.fromisoformat(svc_update_str.replace("Z", "+00:00"))
                        if svc_dt >= cutoff:
                            template_rev = (svc.get("template", {}) or {}).get("revision") or svc.get("latestReadyRevision", "").split("/")[-1]
                            changes.append({
                                "source": "cloud_run_service",
                                "service_name": svc_short,
                                "revision_name": template_rev,
                                "timestamp": svc_update_str,
                                "action": "traffic_shift",
                                "is_traffic_shift": True,
                                "differs_from_previous": False,
                                "details": {
                                    "action": "traffic_shift",
                                    "is_traffic_shift": True,
                                    "differs_from_previous": False,
                                    "updateTime": svc_update_str,
                                    "note": "Traffic shifted back to existing known revision without new revision creation",
                                },
                            })
                    except Exception:
                        pass
    except Exception as e:
        changes.append({"source": "cloud_run", "error": str(e)})

    # 2. Apigee deployment history of this stack's proxies (<NAME_PREFIX>-*) in APIGEE_ORG /
    # APIGEE_ENV: every deployed revision with its deploy start time ("apigee" block), and the
    # deployments started in the window as changes. A failed read is an "apigee" error entry.
    proxy_prefix = f"{cfg.name_prefix}-"
    apigee_summary: Dict[str, Any] = {
        "org": cfg.apigee_org, "environment": cfg.apigee_env, "proxy_prefix": proxy_prefix, "deployments": [],
    }
    try:
        if not cfg.apigee_org or not cfg.apigee_env:
            missing = [n for n, v in (("APIGEE_ORG", cfg.apigee_org), ("APIGEE_ENV", cfg.apigee_env)) if not v]
            raise ValueError(f"{' and '.join(missing)} not set in {cfg.name_prefix}-tools")
        deps_url = (
            f"https://apigee.googleapis.com/v1/organizations/{cfg.apigee_org}"
            f"/environments/{cfg.apigee_env}/deployments"
        )
        resp = requests.get(deps_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Apigee deployments list returned HTTP {resp.status_code}")
        for dep in resp.json().get("deployments", []):
            proxy = str(dep.get("apiProxy") or "")
            if not proxy.startswith(proxy_prefix):
                continue
            dep_time_ms = int(dep.get("deployStartTime", 0) or 0)
            ts_iso = (
                datetime.fromtimestamp(dep_time_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if dep_time_ms else None
            )
            apigee_summary["deployments"].append({
                "api_name": proxy, "revision": dep.get("revision"), "timestamp": ts_iso,
            })
            if dep_time_ms >= cutoff_ms:
                changes.append({
                    "source": "apigee_deployment",
                    "api_name": proxy,
                    "revision": dep.get("revision"),
                    "environment": dep.get("environment") or cfg.apigee_env,
                    "timestamp": ts_iso,
                    "details": dep,
                })
    except Exception as e:
        changes.append({"source": "apigee", "error": str(e)})

    # Sort changes descending by timestamp where timestamp exists
    def sort_key(c: Dict[str, Any]) -> str:
        return str(c.get("timestamp") or "")

    changes.sort(key=sort_key, reverse=True)

    return {
        "window_hours": window_hours,
        "cutoff_timestamp": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changes": changes,
        "total_changes": len([c for c in changes if "error" not in c]),
        "apigee": apigee_summary,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for Cloud Run probes."""
    return JSONResponse({"status": "healthy", "service": "ata-tools"})  # prefix-ok: component identifier


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing Bearer token and optional Google OIDC validation."""

    def __init__(
        self,
        app: ASGIApp,
        enforce_auth: bool = False,
        verify_oidc: bool = False,
        expected_audience: str | list[str] | None = None,
        allowed_service_accounts: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.enforce_auth = enforce_auth
        self.verify_oidc = verify_oidc
        self.expected_audience = expected_audience
        self.allowed_service_accounts = allowed_service_accounts
        self._auth_request: Any = None
        if self.verify_oidc:
            try:
                from google.auth.transport.requests import Request as GoogleAuthRequest
                self._auth_request = GoogleAuthRequest()
            except ImportError:
                pass

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            not self.enforce_auth
            or request.url.path.rstrip("/") == "/health"
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Missing or invalid Authorization header"},
                status_code=401,
            )

        token = auth_header[7:].strip()
        if not token:
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Empty Bearer token provided"},
                status_code=401,
            )

        if self.verify_oidc and (self._auth_request is None or not self.expected_audience):
            # Fail closed: never accept a token that cannot be verified against an audience.
            return JSONResponse(
                {"error": "Unauthorized", "detail": "OIDC verification is not configured (audience or transport missing)"},
                status_code=401,
            )

        if self.verify_oidc:
            try:
                from google.oauth2 import id_token
                claims: dict[str, Any] = id_token.verify_oauth2_token(
                    token,
                    self._auth_request,
                    audience=self.expected_audience,
                )
                if self.allowed_service_accounts:
                    caller_email = claims.get("email")
                    allowed = {str(sa).strip().lower() for sa in self.allowed_service_accounts}
                    if str(caller_email or "").strip().lower() not in allowed:
                        return JSONResponse(
                            {"error": "Forbidden", "detail": f"Caller email '{caller_email}' not authorized"},
                            status_code=403,
                        )
            except Exception as e:
                return JSONResponse(
                    {"error": "Unauthorized", "detail": f"Token verification failed: {e}"},
                    status_code=401,
                )

        return await call_next(request)


def create_app(
    enforce_auth: Optional[bool] = None,
    verify_oidc: Optional[bool] = None,
    expected_audience: Optional[str | list[str]] = None,
    allowed_service_accounts: Optional[list[str]] = None,
) -> Any:
    """Create and configure the FastMCP Starlette ASGI application.

    Auth defaults come from config: the accepted audiences are EXPECTED_AUDIENCE plus the
    deterministic <prefix>-tools URL (PROJECT_NUMBER), and the accepted callers are
    ALLOWED_SERVICE_ACCOUNTS, else <prefix>-agent-sa@<PROJECT_ID>.
    """
    cfg = get_tools_config()
    if enforce_auth is None:
        enforce_auth = cfg.enforce_auth
    if verify_oidc is None:
        verify_oidc = cfg.verify_oidc
    if expected_audience is None:
        expected_audience = cfg.effective_expected_audiences
    if allowed_service_accounts is None:
        allowed_service_accounts = cfg.effective_allowed_service_accounts
    # Fail closed on Cloud Run (K_SERVICE is set there): ENFORCE_AUTH without VERIFY_OIDC accepts
    # any non-empty Bearer string, and neither leaves the MCP tools open to anyone with run.invoker.
    if os.getenv("K_SERVICE") and not cfg.is_local and not (enforce_auth and verify_oidc):
        raise RuntimeError(
            "the tools service on Cloud Run requires ENFORCE_AUTH=true and VERIFY_OIDC=true "
            "(tools/scripts/deploy.sh sets both)"
        )
    if enforce_auth and verify_oidc and not expected_audience:
        raise RuntimeError(
            f"{cfg.name_prefix}-tools cannot start: VERIFY_OIDC is on but no ID-token audience is configured "
            "(set EXPECTED_AUDIENCE or PROJECT_NUMBER)"
        )

    middleware = [
        Middleware(
            AuthMiddleware,
            enforce_auth=enforce_auth,
            verify_oidc=verify_oidc,
            expected_audience=expected_audience,
            allowed_service_accounts=allowed_service_accounts,
        )
    ]

    return mcp.http_app(
        stateless_http=True,
        json_response=True,
        middleware=middleware,
    )
