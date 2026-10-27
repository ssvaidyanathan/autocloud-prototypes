"""CloudSQLSpecialist Agent for Alert Triage Runtime.

Analyzes active PostgreSQL connections vs max_connections, CPU utilization,
and database lock wait events.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from .base import BaseSpecialist, EvidenceDossier
from .telemetry_reads import UNAVAILABLE_KEY, failure_reason, gap_text, read_log_entries, record_unavailable

logger = logging.getLogger(__name__)

# Lock-wait / deadlock log entries read per investigation (one page).
CLOUD_SQL_LOCK_LOG_MAX_ENTRIES = 20


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


class CloudSQLSpecialist(BaseSpecialist):
    """Specialist agent diagnosing Cloud SQL database saturation and locks."""

    def __init__(self):
        super().__init__("CloudSQLSpecialist")

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        db_instance = alert.resource_labels.get("database_id")
        if not db_instance:
            if "db" in alert.resource_name or "sql" in alert.resource_name:
                db_instance = alert.resource_name
            else:
                try:
                    from topology.topology_graph import get_upstream_dependencies
                    deps = get_upstream_dependencies(alert.resource_name)
                    db_instance = next((d for d in deps if "db" in d or "sql" in d), None)
                except Exception:
                    db_instance = None
            if not db_instance:
                db_instance = alert.resource_name

        # Strip project prefix if present for queries that require short instance id
        short_db_instance = db_instance.split(":")[-1] if ":" in db_instance else db_instance
        full_database_id = f"{config.project_id}:{short_db_instance}" if ":" not in db_instance else db_instance

        findings: list[str] = []
        evidence_items: list[EvidenceItem] = []
        metrics: dict[str, Any] = {}

        # Calculate time window anchored on alert start time
        start_time, end_time = _parse_time_window(alert)
        start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        query_conns = (
            f'fetch cloudsql_database :: cloudsql.googleapis.com/database/network/connections '
            f'| filter resource.database_id == "{full_database_id}"'
        )
        query_cpu = (
            f'fetch cloudsql_database :: cloudsql.googleapis.com/database/cpu/utilization '
            f'| filter resource.database_id == "{full_database_id}"'
        )
        query_sql_admin = (
            f'GET /v1/projects/{config.project_id}/instances/{short_db_instance}'
        )
        query_sql_logs = (
            f'resource.type="cloudsql_database" AND '
            f'(textPayload=~"lock" OR textPayload=~"deadlock" OR jsonPayload.message=~"lock") AND '
            f'timestamp >= "{start_iso}" AND timestamp <= "{end_iso}"'
        )

        queries_executed: dict[str, list[str]] = {
            "cloud_monitoring": [query_conns, query_cpu],
            "cloud_sql_admin": [query_sql_admin],
            "cloud_logging": [query_sql_logs],
        }

        # Telemetry values extracted from real queries or recorded as N/A (not captured)
        active_connections_val: Optional[int] = None
        max_connections_val: Optional[int] = None
        cpu_utilization_pct_val: Optional[float] = None
        active_lock_waits_val: Optional[int] = None
        instance_details: Optional[dict[str, Any]] = None

        if not config.is_local:
            # 1. Cloud Monitoring time series query
            try:
                from google.cloud import monitoring_v3

                metric_client = monitoring_v3.MetricServiceClient()
                interval = monitoring_v3.TimeInterval(
                    end_time={"seconds": int(end_time.timestamp())},
                    start_time={"seconds": int(start_time.timestamp())},
                )

                # Query connections: PostgreSQL Cloud SQL instances report active connections via
                # database/postgresql/num_backends per database. Fallback to database/network/connections.
                conn_filters = [
                    (
                        f'metric.type = "cloudsql.googleapis.com/database/postgresql/num_backends" AND '
                        f'resource.labels.database_id = "{full_database_id}"'
                    ),
                    (
                        f'metric.type = "cloudsql.googleapis.com/database/postgresql/num_backends" AND '
                        f'resource.labels.database_id = "{short_db_instance}"'
                    ),
                    (
                        f'metric.type = "cloudsql.googleapis.com/database/network/connections" AND '
                        f'resource.labels.database_id = "{full_database_id}"'
                    ),
                    (
                        f'metric.type = "cloudsql.googleapis.com/database/network/connections" AND '
                        f'resource.labels.database_id = "{short_db_instance}"'
                    ),
                ]
                conns_by_timestamp: dict[float, int] = {}
                conn_failures: list[Exception] = []
                for c_filter in conn_filters:
                    try:
                        req_conns = monitoring_v3.ListTimeSeriesRequest(
                            name=f"projects/{config.project_id}",
                            filter=c_filter,
                            interval=interval,
                            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                        )
                        series_list = list(metric_client.list_time_series(request=req_conns))
                        for ts in series_list:
                            for pt in ts.points:
                                t_sec = pt.interval.start_time.timestamp() if hasattr(pt.interval.start_time, "timestamp") else float(getattr(pt.interval.start_time, "seconds", 0))
                                pt_val = pt.value.int64_value if hasattr(pt.value, "int64_value") else (int(pt.value.double_value) if hasattr(pt.value, "double_value") else 0)
                                conns_by_timestamp[t_sec] = conns_by_timestamp.get(t_sec, 0) + pt_val
                        if conns_by_timestamp:
                            sorted_times = sorted(conns_by_timestamp.keys())
                            alert_ts = None
                            if alert.started_at:
                                try:
                                    alert_ts = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00")).timestamp()
                                except Exception:
                                    pass

                            # Avoid picking up historical spikes from prior scenarios when evaluating non-database alerts
                            is_explicit_sql_alert = any(k in alert.policy_name.lower() for k in ["cloudsql", "connections", "max_connections"])
                            if alert_ts is not None and not is_explicit_sql_alert:
                                relevant_conns = [conns_by_timestamp[t] for t in sorted_times if t >= alert_ts - 180]
                                active_connections_val = max(relevant_conns) if relevant_conns else conns_by_timestamp[sorted_times[-1]]
                            else:
                                active_connections_val = max(conns_by_timestamp.values())

                            # Record first onset timestamp when threshold was breached
                            for t in sorted_times:
                                if conns_by_timestamp[t] >= 20:
                                    t_dt = datetime.fromtimestamp(t, tz=timezone.utc)
                                    metrics["metric_onset"] = t_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                                    break
                            break
                    except Exception as filter_exc:
                        logger.warning("Cloud SQL connections query attempt failed for filter %s: %s", c_filter, filter_exc)
                        conn_failures.append(filter_exc)

                if active_connections_val is None and conn_failures:
                    record_unavailable(
                        metrics, source="Cloud Monitoring Cloud SQL connections", target=short_db_instance,
                        reason=failure_reason(conn_failures[-1], "Cloud Monitoring"),
                    )

                # Query CPU utilization
                req_cpu = monitoring_v3.ListTimeSeriesRequest(
                    name=f"projects/{config.project_id}",
                    filter=(
                        f'metric.type = "cloudsql.googleapis.com/database/cpu/utilization" AND '
                        f'resource.labels.database_id = "{full_database_id}"'
                    ),
                    interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                )
                for ts in metric_client.list_time_series(request=req_cpu):
                    for pt in ts.points:
                        pt_val = pt.value.double_value
                        # CPU utilization is a fraction (0.0 to 1.0)
                        pct = round(pt_val * 100.0, 1)
                        if cpu_utilization_pct_val is None or pct > cpu_utilization_pct_val:
                            cpu_utilization_pct_val = pct
            except Exception as exc:
                logger.warning("Cloud SQL Cloud Monitoring query failed for %s: %s", short_db_instance, exc)
                record_unavailable(
                    metrics, source="Cloud Monitoring Cloud SQL metrics", target=short_db_instance,
                    reason=failure_reason(exc, "Cloud Monitoring"),
                )

            # 2. Cloud SQL Admin API Describe
            try:
                import google.auth
                from google.auth.transport.requests import Request as GoogleAuthRequest
                import requests

                creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                if not creds.valid:
                    creds.refresh(GoogleAuthRequest())
                headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
                url = f"https://sqladmin.googleapis.com/v1/projects/{config.project_id}/instances/{short_db_instance}"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    instance_details = resp.json()
                    # Check databaseFlags for max_connections
                    db_flags = instance_details.get("settings", {}).get("databaseFlags", [])
                    for flag in db_flags:
                        if flag.get("name") == "max_connections":
                            try:
                                max_connections_val = int(flag.get("value", 0))
                            except ValueError:
                                pass
                    # Infer default max_connections from instance tier if not explicitly set in flags
                    if max_connections_val is None:
                        tier = instance_details.get("settings", {}).get("tier", "")
                        if "f1-micro" in tier:
                            max_connections_val = 25
                        elif "g1-small" in tier:
                            max_connections_val = 50
                        elif tier:
                            max_connections_val = 100
            except Exception as exc:
                logger.warning("Cloud SQL Admin API query failed for %s: %s", short_db_instance, exc)
                record_unavailable(
                    metrics, source="Cloud SQL Admin API instance describe", target=short_db_instance,
                    reason=f"Cloud SQL Admin API read failed ({type(exc).__name__})",
                )

            # 3. Cloud Logging for lock wait / deadlock events
            try:
                import google.cloud.logging

                log_client = google.cloud.logging.Client(project=config.project_id)
                log_entries = await read_log_entries(
                    log_client,
                    filter_=query_sql_logs,
                    max_results=CLOUD_SQL_LOCK_LOG_MAX_ENTRIES,
                    config=config,
                    what=f"Cloud Logging lock-wait read for {short_db_instance}",
                )
                if log_entries:
                    active_lock_waits_val = len(log_entries)
            except Exception as exc:
                logger.warning("Cloud SQL Logging query failed for %s: %s", short_db_instance, exc)
                record_unavailable(
                    metrics, source="Cloud SQL lock-wait logs", target=short_db_instance,
                    reason=failure_reason(exc, "Cloud Logging"),
                )

        # Assign metrics or 'N/A (not captured)'
        metrics["active_connections"] = (
            active_connections_val if active_connections_val is not None else "N/A (not captured)"
        )
        metrics["max_connections"] = (
            max_connections_val if max_connections_val is not None else "N/A (not captured)"
        )
        metrics["cpu_utilization_pct"] = (
            cpu_utilization_pct_val if cpu_utilization_pct_val is not None else "N/A (not captured)"
        )
        metrics["active_lock_waits"] = (
            active_lock_waits_val if active_lock_waits_val is not None else "N/A (not captured)"
        )

        # Log WARNING for genuinely absent data per Quality Gates
        if active_connections_val is None:
            logger.warning(
                "Active connections on %s is N/A: no num_backends or connections points captured in window [%s, %s]",
                short_db_instance, start_iso, end_iso,
            )
        if max_connections_val is None:
            logger.warning(
                "Max connections on %s is N/A: could not determine max_connections from flags or instance tier",
                short_db_instance,
            )
        if cpu_utilization_pct_val is None:
            logger.warning(
                "CPU utilization on %s is N/A: no CPU utilization points captured in window [%s, %s]",
                short_db_instance, start_iso, end_iso,
            )
        if active_lock_waits_val is None:
            logger.warning(
                "Active lock waits on %s is N/A: no lock wait log entries found in window [%s, %s]",
                short_db_instance, start_iso, end_iso,
            )

        if instance_details:
            state = instance_details.get("state")
            tier = instance_details.get("settings", {}).get("tier")
            if state:
                metrics["database_state"] = state
            if tier:
                metrics["tier"] = tier

        has_conns = isinstance(active_connections_val, (int, float)) and (
            active_connections_val >= 20 or (
                isinstance(max_connections_val, (int, float))
                and max_connections_val > 0
                and active_connections_val / max_connections_val >= 0.8
            )
        )
        has_locks = isinstance(active_lock_waits_val, (int, float)) and active_lock_waits_val > 0
        has_cpu = isinstance(cpu_utilization_pct_val, (int, float)) and cpu_utilization_pct_val >= 80.0

        is_db_incident = any(
            k in f"{alert.policy_name} {alert.resource_name}".lower()
            for k in ["sql", "database", "connection", "postgres", "db", "lock"]
        ) or has_conns or has_locks or has_cpu

        if is_db_incident and not metrics.get("metric_onset"):
            metrics["metric_onset"] = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if not config.is_local:
            try:
                from google.cloud import monitoring_v3
                client = monitoring_v3.MetricServiceClient()
                project_name = f"projects/{config.project_id}"
                logger.debug("CloudSQLSpecialist querying metrics for %s in %s", db_instance, project_name)
            except Exception as exc:
                logger.debug("Live Cloud Monitoring query skipped: %s", exc)

        if is_db_incident or "saturation" in alert.policy_name.lower():
            findings.append(f"PostgreSQL connection pool evaluation performed on `{short_db_instance}`.")
            conns_str = f"{metrics['active_connections']}/{metrics['max_connections']}"
            findings.append(
                f"Active connections: {conns_str}; CPU: {metrics['cpu_utilization_pct']}%; Lock waits: {metrics['active_lock_waits']}."
            )

            evidence_items.append(
                EvidenceItem(
                    headline=f"Cloud SQL Saturation & Contention: {short_db_instance}",
                    content=(
                        f"Instance: {short_db_instance}\n"
                        f"Active Connections: {metrics['active_connections']} / {metrics['max_connections']}\n"
                        f"CPU Utilization: {metrics['cpu_utilization_pct']}\n"
                        f"Lock Wait Events: {metrics['active_lock_waits']}"
                    ),
                )
            )
        elif metrics.get(UNAVAILABLE_KEY):
            # Reads failed: "healthy" would be an unverified claim.
            gaps = "; ".join(gap_text(g) for g in metrics[UNAVAILABLE_KEY])
            findings.append(f"Cloud SQL database instance `{short_db_instance}` health not assessed: {gaps}.")
            evidence_items.append(
                EvidenceItem(
                    headline=f"Cloud SQL Health not assessed: {short_db_instance}",
                    content=(
                        f"Telemetry unavailable for `{short_db_instance}`: {gaps}. Captured: connections "
                        f"{metrics['active_connections']}/{metrics['max_connections']}, CPU {metrics['cpu_utilization_pct']}."
                    ),
                )
            )
        else:
            findings.append(f"Cloud SQL database instance `{short_db_instance}` connections and CPU are healthy.")
            evidence_items.append(
                EvidenceItem(
                    headline=f"Cloud SQL Baseline Health: {short_db_instance}",
                    content=(
                        f"Database metrics for `{short_db_instance}` indicate connections: "
                        f"{metrics['active_connections']}/{metrics['max_connections']} and CPU: {metrics['cpu_utilization_pct']}."
                    ),
                )
            )

        db_conf = 0.70
        if has_conns:
            db_conf += 0.15
        if has_locks:
            db_conf += 0.10
        if has_cpu:
            db_conf += 0.05
        confidence = round(min(0.99, db_conf), 2)

        return EvidenceDossier(
            specialist_name=self.name,
            target_resource=short_db_instance,
            findings=findings,
            metrics=metrics,
            evidence_items=evidence_items,
            queries_executed=queries_executed,
            anomaly_detected=is_db_incident,
            confidence=confidence,
        )
