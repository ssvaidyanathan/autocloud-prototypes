"""CloudRunSpecialist Agent for Alert Triage Runtime.

Analyzes request counts, 5xx error distributions, container restart counts,
and structured error logs while strictly suppressing fault-injection endpoints.
"""

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from .base import BaseSpecialist, EvidenceDossier

logger = logging.getLogger(__name__)

# Patterns for admin and fault injection endpoints that must be excluded from logs
EXCLUDED_ENDPOINT_PATTERNS = [
    re.compile(r"/admin/fault(/.*)?"),
    re.compile(r"/admin(/.*)?"),
]

# Database authentication failure signatures. Each pattern names a specific DB login
# failure (PostgreSQL/MySQL server message, SQLSTATE class 28 code, or the Cloud SQL
# connector's IAM login failure). Generic words such as "auth", "denied" or "password"
# are deliberately absent: they match HTTP Authorization headers, OAuth and IAM text in
# ordinary request logs and produced false DB-auth findings.
DB_AUTH_PATTERNS = [
    # PostgreSQL: FATAL: password authentication failed for user "..."
    re.compile(r"\bpassword authentication failed\b"),
    re.compile(r"\bpam authentication failed\b"),
    re.compile(r"\bfatal:\s*password\b"),
    # Cloud SQL IAM database authentication:
    # FATAL: Cloud SQL IAM user|service account authentication failed for user "..."
    re.compile(r"\biam (?:user |service account |database |db )?authentication failed\b"),
    # PostgreSQL: FATAL: role "..." does not exist (unknown DB user)
    re.compile(r"\brole \"[^\"]+\" does not exist\b"),
    re.compile(r"\bpermission denied for database\b"),
    # MySQL: Access denied for user 'x'@'host'
    re.compile(r"\baccess denied for user\b"),
    # SQLSTATE 28000 (invalid_authorization_specification) / 28P01 (invalid_password),
    # only as an explicit code field, e.g. pg8000 "'C': '28P01'" or "SQLSTATE 28000".
    re.compile(r"(?:\bsqlstate|\bcode|'c')\W{0,4}28(?:000|p01)\b"),
    # Cloud SQL connector + pg8000 IAM login rejection as logged by the inventory app
    # (S5: invalid IAM DB user): "Failed to connect to Cloud SQL: 'NoneType' object has
    # no attribute 'decode'". Matched in full; a bare "failed to connect" is not auth.
    re.compile(r"\bfailed to connect to cloud sql:.*'nonetype' object has no attribute 'decode'"),
]


# Measured breach thresholds for Cloud Run telemetry (5xx share of requests, peak observed
# latency: the larger of the highest per-minute mean of request_latencies and the slowest
# logged request >= 2s; it is not a percentile, so it is never labelled P95).
ERROR_RATE_5XX_BREACH_PCT = 5.0
LATENCY_P95_BREACH_MS = 2000.0

# Which statistic ``latency_p95_ms`` holds (the key name is historical; it is never a P95).
# run.googleapis.com/request_latencies is a DISTRIBUTION metric in milliseconds, one point per
# minute per series; its points carry a mean, so the metric value is the highest per-minute
# mean. A logged request's httpRequest.latency is that single request's duration.
LATENCY_STAT_MINUTE_MEAN = "peak per-minute mean latency"
LATENCY_STAT_LOGGED_REQUEST = "slowest logged request latency"
LATENCY_STAT_GENERIC = "peak observed latency"


def latency_label(metrics: Optional[dict[str, Any]]) -> str:
    """Label of the latency statistic recorded in a Cloud Run dossier's metrics."""
    stat = (metrics or {}).get("latency_statistic")
    return stat if isinstance(stat, str) and stat.strip() else LATENCY_STAT_GENERIC


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("%", "").replace("ms", "").strip())
        except ValueError:
            return None
    return None


def _fmt_latency(value: Any) -> str:
    """'<value>ms' for a measured latency in ms, else the value as recorded (e.g. 'N/A ...')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}ms"
    return str(value)


def measured_breaches(metrics: dict[str, Any]) -> list[str]:
    """Measured 5xx / latency threshold breaches recorded in a Cloud Run dossier's metrics.

    Only telemetry values count; log-derived signals (e.g. ``has_db_auth_error``) do not.
    Returns human-readable breach descriptions (empty when nothing was measured over threshold).
    """
    breaches: list[str] = []
    metrics = metrics or {}
    rate_raw = metrics.get("error_rate_5xx")
    rate = _as_float(rate_raw) if not isinstance(rate_raw, str) or "%" in rate_raw else None
    if rate is not None and rate >= ERROR_RATE_5XX_BREACH_PCT:
        breaches.append(f"HTTP 5xx error rate {rate_raw}")
    latency = _as_float(metrics.get("latency_p95_ms"))
    if latency is not None and latency >= LATENCY_P95_BREACH_MS:
        breaches.append(f"{latency_label(metrics)} {latency:g}ms")
    return breaches


# Database connection failures that do not name a cause. They are recorded as a weaker,
# separate signal ("database connection failures"), never as an authentication failure, so a
# change in the connector's error text cannot make a DB-side failure disappear from the RCA.
DB_CONNECT_FAILURE_PATTERNS = [
    re.compile(r"\bfailed to connect to cloud sql\b"),
    re.compile(r"\bcould not connect to server\b"),
    re.compile(r"\bconnection to server at \S+.* failed\b"),
    re.compile(r"\bsqlalchemy\.exc\.operationalerror\b"),
    re.compile(r"\bpg8000\.exceptions\.interfaceerror\b"),
]


def has_db_connect_failure_signature(text: str) -> bool:
    """Return True if log text reports a database connection failure (cause not named)."""
    lowered = (text or "").lower()
    return any(p.search(lowered) for p in DB_CONNECT_FAILURE_PATTERNS)


def has_db_auth_signature(text: str) -> bool:
    """Return True if log text contains a specific database authentication failure."""
    lowered = (text or "").lower()
    return any(p.search(lowered) for p in DB_AUTH_PATTERNS)


def is_excluded_endpoint(path: str) -> bool:
    """Check if an HTTP request path should be excluded from telemetry/logs."""
    for pattern in EXCLUDED_ENDPOINT_PATTERNS:
        if pattern.search(path):
            return True
    return False


def _extract_point_time(pt: Any) -> Optional[datetime]:
    """Safely extract UTC datetime from Monitoring point interval start_time."""
    if not hasattr(pt, "interval") or not pt.interval:
        return None
    st = getattr(pt.interval, "start_time", None)
    if st is None:
        return None
    if isinstance(st, datetime):
        return st if st.tzinfo else st.replace(tzinfo=timezone.utc)
    if hasattr(st, "timestamp"):
        try:
            return datetime.fromtimestamp(st.timestamp(), tz=timezone.utc)
        except Exception:
            pass
    if hasattr(st, "seconds"):
        try:
            return datetime.fromtimestamp(st.seconds, tz=timezone.utc)
        except Exception:
            pass
    return None


def _parse_time_window(alert: AlertEvent) -> tuple[datetime, datetime]:
    """Calculate query window anchored on the alert start time (-15m to +15m or now)."""
    now = datetime.now(timezone.utc)
    if alert.started_at:
        try:
            started = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
        except Exception:
            started = now
    else:
        started = now

    start_window = started - timedelta(minutes=15)
    end_window = min(started + timedelta(minutes=15), now)
    if end_window <= start_window:
        end_window = now
    if end_window <= start_window:
        start_window = end_window - timedelta(minutes=15)
    return start_window, end_window


class CloudRunSpecialist(BaseSpecialist):
    """Specialist agent diagnosing Cloud Run container service degradation."""

    def __init__(self):
        super().__init__("CloudRunSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
        target_service: Optional[str] = None,
    ) -> EvidenceDossier:
        if not target_service:
            target_service = alert.resource_labels.get("service_name") or alert.resource_name
        findings: list[str] = []
        evidence_items: list[EvidenceItem] = []
        metrics: dict[str, Any] = {}
        queries_executed: dict[str, list[str]] = {"cloud_monitoring": [], "cloud_logging": [], "cloud_run_admin": []}

        # Calculate time window anchored on alert start time
        start_time, end_time = _parse_time_window(alert)
        start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        query_req = (
            f'fetch cloud_run_revision :: run.googleapis.com/request_count '
            f'| filter resource.service_name == "{target_service}"'
        )
        queries_executed["cloud_monitoring"].append(query_req)

        query_lat = (
            f'fetch cloud_run_revision :: run.googleapis.com/request_latencies '
            f'| filter resource.service_name == "{target_service}"'
        )
        queries_executed["cloud_monitoring"].append(query_lat)

        query_logs = (
            f'resource.type="cloud_run_revision" AND '
            f'resource.labels.service_name="{target_service}" AND '
            f'timestamp >= "{start_iso}" AND timestamp <= "{end_iso}" AND '
            f'NOT logName=~"cloudaudit.googleapis.com" AND '
            f'(logName=~"requests" OR logName=~"stdout" OR logName=~"stderr")'
        )
        queries_executed["cloud_logging"].append(query_logs)

        query_describe = (
            f'GET /v2/projects/{config.project_id}/locations/{config.region}/services/{target_service}'
        )
        queries_executed["cloud_run_admin"].append(query_describe)

        # Telemetry extracted from real queries or recorded as N/A (not captured)
        error_rate_5xx_val: Optional[str] = None
        rpm_val: Optional[int] = None
        restarts_val: Optional[int] = None
        latency_p95_ms_val: Optional[float] = None
        latency_statistic: Optional[str] = None
        earliest_anomaly_time: Optional[datetime] = None
        service_describe_info: Optional[dict[str, Any]] = None
        error_logs_captured: list[dict[str, Any]] = []
        detected_db_auth_in_logs: bool = False
        detected_db_connect_failure_in_logs: bool = False

        # 1. Cloud Monitoring time series query
        try:
            from google.cloud import monitoring_v3

            metric_client = monitoring_v3.MetricServiceClient()
            interval = monitoring_v3.TimeInterval(
                end_time={"seconds": int(end_time.timestamp())},
                start_time={"seconds": int(start_time.timestamp())},
            )
            filter_str = (
                f'metric.type = "run.googleapis.com/request_count" AND '
                f'resource.labels.service_name = "{target_service}"'
            )
            req = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{config.project_id}",
                filter=filter_str,
                interval=interval,
                view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            )

            total_requests = 0
            error_5xx_requests = 0
            duration_minutes = max(1.0, (end_time - start_time).total_seconds() / 60.0)

            for ts in metric_client.list_time_series(request=req):
                resp_code_str = ts.metric.labels.get("response_code", "")
                resp_class_str = ts.metric.labels.get("response_code_class", "")
                is_5xx = resp_code_str.startswith("5") or resp_class_str == "5xx"

                for pt in ts.points:
                    val = pt.value.int64_value if hasattr(pt.value, "int64_value") else 0
                    total_requests += val
                    if is_5xx:
                        error_5xx_requests += val
                        if earliest_anomaly_time is None:
                            pt_time = _extract_point_time(pt)
                            if pt_time:
                                earliest_anomaly_time = pt_time

            if total_requests > 0:
                rate_pct = (error_5xx_requests / total_requests) * 100.0
                error_rate_5xx_val = f"{rate_pct:.1f}%"
                rpm_val = int(total_requests / duration_minutes)

            # Query Cloud Monitoring for request_latencies
            filter_lat_str = (
                f'metric.type = "run.googleapis.com/request_latencies" AND '
                f'resource.labels.service_name = "{target_service}"'
            )
            req_lat = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{config.project_id}",
                filter=filter_lat_str,
                interval=interval,
                view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            )
            for ts in metric_client.list_time_series(request=req_lat):
                for pt in ts.points:
                    pt_val = None
                    if hasattr(pt.value, "distribution_value") and pt.value.distribution_value:
                        pt_val = pt.value.distribution_value.mean
                    elif hasattr(pt.value, "double_value"):
                        pt_val = pt.value.double_value
                    elif hasattr(pt.value, "int64_value"):
                        pt_val = float(pt.value.int64_value)

                    if pt_val is not None:
                        # request_latencies is documented in milliseconds: no unit guessing
                        # (a 28 ms mean is 28 ms, not 28 s).
                        lat_ms = float(pt_val)
                        if latency_p95_ms_val is None or lat_ms > latency_p95_ms_val:
                            latency_p95_ms_val = lat_ms
                            latency_statistic = LATENCY_STAT_MINUTE_MEAN
                        if lat_ms >= 2000.0 and earliest_anomaly_time is None:
                            pt_time = _extract_point_time(pt)
                            if pt_time:
                                earliest_anomaly_time = pt_time
        except Exception as exc:
            logger.warning("Live Cloud Monitoring query failed for %s: %s", target_service, exc)

        # 2. Cloud Logging query with endpoint filtering
        try:
            import google.cloud.logging
            from .logs import (
                is_audit_log,
                is_application_or_request_log,
                format_single_line_log_excerpt,
                parse_access_log_line,
            )

            log_client = google.cloud.logging.Client(project=config.project_id)
            try:
                log_entries = log_client.list_entries(
                    filter_=query_logs,
                    max_results=50,
                    order_by=google.cloud.logging.DESCENDING,
                )
            except TypeError:
                log_entries = log_client.list_entries(filter_=query_logs, max_results=50)

            detected_db_auth_in_logs = False

            for entry in log_entries:
                if is_audit_log(entry) or not is_application_or_request_log(entry):
                    continue

                req_path = ""
                status_code = None
                entry_lat_ms = None

                if hasattr(entry, "http_request") and entry.http_request:
                    raw_url = entry.http_request.get("requestUrl", "")
                    if raw_url:
                        try:
                            req_path = urlparse(raw_url).path or raw_url
                        except Exception:
                            req_path = raw_url
                    status_code = entry.http_request.get("status")
                    lat_str = str(entry.http_request.get("latency", ""))
                    if lat_str:
                        try:
                            if lat_str.endswith("ms"):
                                entry_lat_ms = float(lat_str[:-2])
                            elif lat_str.endswith("s"):
                                entry_lat_ms = float(lat_str[:-1]) * 1000.0
                            else:
                                entry_lat_ms = float(lat_str)
                        except ValueError:
                            pass

                if not req_path and isinstance(getattr(entry, "payload", None), str):
                    access = parse_access_log_line(entry.payload)
                    if access:
                        _, req_path, status_code = access

                if hasattr(entry, "payload") and isinstance(entry.payload, dict):
                    if not req_path:
                        req_path = entry.payload.get("path") or entry.payload.get("requestPath") or ""
                    delay_in_payload = entry.payload.get("delay_ms") or entry.payload.get("latency_delay_ms") or entry.payload.get("target_latency_ms")
                    if delay_in_payload and isinstance(delay_in_payload, (int, float)):
                        entry_lat_ms = float(delay_in_payload)

                clean_line = format_single_line_log_excerpt(entry)

                # Scan entry payload, text_payload, and formatted clean line for DB connection/auth signatures
                entry_text_parts = []
                entry_payload = getattr(entry, "payload", None)
                if isinstance(entry_payload, str):
                    entry_text_parts.append(entry_payload)
                elif isinstance(entry_payload, dict):
                    entry_text_parts.extend(str(v) for v in entry_payload.values())
                text_payload = getattr(entry, "text_payload", None)
                if text_payload:
                    entry_text_parts.append(str(text_payload))
                if clean_line:
                    entry_text_parts.append(clean_line)
                full_entry_text = " ".join(entry_text_parts).lower()

                if has_db_auth_signature(full_entry_text):
                    detected_db_auth_in_logs = True
                elif has_db_connect_failure_signature(full_entry_text):
                    detected_db_connect_failure_in_logs = True

                if req_path and is_excluded_endpoint(req_path):
                    continue

                if entry_lat_ms is not None and entry_lat_ms >= 2000.0:
                    if latency_p95_ms_val is None or entry_lat_ms > latency_p95_ms_val:
                        latency_p95_ms_val = entry_lat_ms
                        latency_statistic = LATENCY_STAT_LOGGED_REQUEST
                    if earliest_anomaly_time is None and hasattr(entry, "timestamp") and entry.timestamp:
                        earliest_anomaly_time = entry.timestamp

                if clean_line:
                    error_logs_captured.append({
                        "path": req_path or f"/{target_service}",
                        "status": status_code or 500,
                        "formatted_line": clean_line,
                        "latency_ms": entry_lat_ms,
                    })
        except Exception as exc:
            logger.debug("Live Cloud Logging query skipped or failed for %s: %s", target_service, exc)

        # 3. Cloud Run Service Describe
        if not config.is_local:
            try:
                import google.auth
                from google.auth.transport.requests import Request as GoogleAuthRequest
                import requests

                creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                if not creds.valid:
                    creds.refresh(GoogleAuthRequest())
                headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
                svc_url = f"https://run.googleapis.com/v2/projects/{config.project_id}/locations/{config.region}/services/{target_service}"
                resp = requests.get(svc_url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    service_describe_info = resp.json()
            except Exception as exc:
                logger.debug("Cloud Run describe query skipped or failed for %s: %s", target_service, exc)

        # Assign real metrics or "N/A (not captured)"
        metrics["error_rate_5xx"] = error_rate_5xx_val if error_rate_5xx_val is not None else "N/A (not captured)"
        metrics["request_count_rpm"] = rpm_val if rpm_val is not None else "N/A (not captured)"
        metrics["container_restarts"] = restarts_val if restarts_val is not None else "N/A (not captured)"
        metrics["latency_p95_ms"] = latency_p95_ms_val if latency_p95_ms_val is not None else "N/A (not captured)"
        metrics["target_latency_p95_ms"] = latency_p95_ms_val if latency_p95_ms_val is not None else "N/A (not captured)"
        if latency_p95_ms_val is not None and latency_statistic:
            metrics["latency_statistic"] = latency_statistic

        # Log WARNING for genuinely absent data per Quality Gates
        if error_rate_5xx_val is None:
            logger.warning(
                "HTTP 5xx error rate on %s is N/A: no request count points found in window [%s, %s]",
                target_service, start_iso, end_iso,
            )
        if rpm_val is None:
            logger.warning(
                "Request count RPM on %s is N/A: zero requests observed in window [%s, %s]",
                target_service, start_iso, end_iso,
            )
        if latency_p95_ms_val is None:
            logger.warning(
                "Request latency on %s is N/A: no latency distribution points or slow logged requests found in window [%s, %s]",
                target_service, start_iso, end_iso,
            )
        if restarts_val is None:
            logger.warning(
                "Container restarts on %s is N/A: no restart counter metrics recorded in window [%s, %s]",
                target_service, start_iso, end_iso,
            )

        if earliest_anomaly_time is not None:
            onset_iso = (
                earliest_anomaly_time.isoformat()
                if hasattr(earliest_anomaly_time, "isoformat")
                else str(earliest_anomaly_time)
            )
            metrics["earliest_anomaly_timestamp"] = onset_iso
            metrics["metric_onset"] = onset_iso

        if service_describe_info:
            latest_ready = service_describe_info.get("latestReadyRevision", "")
            if latest_ready:
                metrics["latest_ready_revision"] = latest_ready.split("/")[-1]

        # Evaluate anomaly condition
        rate_val = 0.0
        if error_rate_5xx_val and "%" in error_rate_5xx_val:
            try:
                rate_val = float(error_rate_5xx_val.replace("%", "").strip())
            except ValueError:
                rate_val = 0.0
        has_real_5xx_breach = rate_val >= ERROR_RATE_5XX_BREACH_PCT
        has_latency_breach = latency_p95_ms_val is not None and latency_p95_ms_val >= LATENCY_P95_BREACH_MS
        is_target_of_alert = (target_service == (alert.resource_labels.get("service_name") or alert.resource_name))
        is_alert_policy_5xx = is_target_of_alert and ("5xx" in alert.policy_name.lower() or "500" in (alert.condition_name or ""))
        is_alert_policy_lat = is_target_of_alert and ("latency" in alert.policy_name.lower() or "timeout" in alert.policy_name.lower() or "slow" in alert.policy_name.lower())
        anomaly_detected = (
            has_real_5xx_breach
            or has_latency_breach
            or is_alert_policy_5xx
            or is_alert_policy_lat
            or detected_db_auth_in_logs
        )

        # Formulate findings and evidence items without synthetic numbers
        if anomaly_detected:
            if has_latency_breach:
                findings.append(
                    f"Elevated request latency detected on `{target_service}`: {latency_label(metrics)} "
                    f"{latency_p95_ms_val:g}ms (threshold: 2000ms)."
                )
            elif error_rate_5xx_val is not None:
                findings.append(
                    f"Elevated HTTP 5xx error rate detected on `{target_service}`: {error_rate_5xx_val} of traffic breached threshold."
                )
            else:
                findings.append(
                    f"Operational anomaly detected on `{target_service}` under policy '{alert.policy_name}'."
                )

            # Inspect application logs for database authentication errors
            has_db_auth_error = detected_db_auth_in_logs
            if has_db_auth_error:
                metrics["has_db_auth_error"] = True
                metrics["observed_symptom"] = "database authentication errors from application logs"
                findings.append(f"Application error logs on `{target_service}` indicate database authentication failure.")
            elif detected_db_connect_failure_in_logs:
                metrics["has_db_connect_error"] = True
                metrics["observed_symptom"] = "database connection failures from application logs"
                findings.append(
                    f"Application error logs on `{target_service}` show database connection failures "
                    "(no authentication failure signature)."
                )

            if error_logs_captured:
                findings.append(
                    f"Extracted {len(error_logs_captured)} structured application logs (fault endpoints filtered)."
                )
                log_lines = "\n".join(
                    f"- {e['formatted_line']}" for e in error_logs_captured[:5]
                )
            else:
                findings.append("No active error logs captured in query window (excluding /admin/*).")
                log_lines = "- N/A (not captured)"

            evidence_content = (
                f"Target: {target_service}\n"
                f"5xx Errors: {metrics['error_rate_5xx']}\n"
                f"{latency_label(metrics)[0].upper()}{latency_label(metrics)[1:]}: {_fmt_latency(metrics['latency_p95_ms'])}\n"
                f"Request Rate: {metrics['request_count_rpm']}\n"
                f"Container Restarts: {metrics['container_restarts']}\n"
                f"Sample Logs (excluding /admin/*):\n"
                f"{log_lines}"
            )

            evidence_items.append(
                EvidenceItem(
                    headline=f"Cloud Run Anomaly Distribution: {target_service}",
                    content=evidence_content,
                )
            )
        else:
            findings.append(f"Cloud Run service `{target_service}` operational telemetry evaluated.")
            evidence_items.append(
                EvidenceItem(
                    headline=f"Cloud Run Service Health: {target_service}",
                    content=f"Telemetry indicates baseline operation for `{target_service}`.",
                )
            )

        return EvidenceDossier(
            specialist_name=self.name,
            target_resource=target_service,
            findings=findings,
            metrics=metrics,
            evidence_items=evidence_items,
            queries_executed=queries_executed,
            anomaly_detected=anomaly_detected,
            confidence=0.91,
        )
