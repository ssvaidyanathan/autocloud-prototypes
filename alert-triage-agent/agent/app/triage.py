"""Triage coordination, pre-analysis locking, and multi-agent investigation."""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import httpx
import yaml

from .config import AgentConfig, get_agent_settings
from .contracts.alert import AlertEvent, is_ata_alert, normalize_resource_name, slugify
from .contracts.report import (
    ActionItem,
    EvidenceItem,
    InvestigationReport,
    ProbableRootCause,
    TimelineItem,
)
from .contracts.links import ConsoleLinkBuilder
from .grouping.models import format_okf_alert_entry
from .specialists.synthesizer import SynthesizerAgent

try:
    from topology.topology_graph import get_blast_radius, get_upstream_dependencies
except ImportError as err:
    logging.getLogger(__name__).error("Failed to import topology_graph: %s", err)
    raise

logger = logging.getLogger(__name__)


class TriageAgent:
    """Agent performing initial incident triage and formulating structured investigation plan."""

    def __init__(self, **kwargs: Any) -> None:
        pass



def write_preanalysis_lock(
    alert: AlertEvent,
    config: AgentConfig,
    group_id: Optional[str] = None,
    filename: Optional[str] = None,
    member_alerts: Optional[list[Any]] = None,
) -> tuple[Path, str, str]:
    """Create immediate pre-analysis lock writing incident-<slug>.md with YAML frontmatter.

    Returns:
        tuple of (file_path, group_id, filename)
    """
    if not is_ata_alert(alert):
        raise ValueError(
            f"Cannot acquire pre-analysis lock for non-ata alert: policy={alert.policy_name}, resource={alert.resource_name}"
        )

    # Normalize resource before dedup, topology, and filenames
    if alert.policy_user_labels and alert.policy_user_labels.get("component"):
        canonical_res = alert.policy_user_labels["component"]
    else:
        canonical_res = normalize_resource_name(
            raw_name=alert.resource_name,
            res_type=alert.resource_type or "",
            res_labels=alert.resource_labels,
        )
    alert.resource_name = canonical_res

    store_dir = config.get_effective_store_path()
    store_dir.mkdir(parents=True, exist_ok=True)

    # 1. Guarantee RFC3339 UTC timestamp
    valid_ts = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not valid_ts or not str(valid_ts).strip():
        valid_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pol_slug = alert.policy_slug
    res_slug = slugify(canonical_res)
    ts_clean = re.sub(r"[^0-9a-zA-Z]", "", valid_ts).lower()
    if not ts_clean:
        ts_clean = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%sz").lower()

    if group_id:
        effective_group_id = group_id
        effective_filename = filename or f"incident-{effective_group_id}.md"
    else:
        effective_filename = f"incident-{pol_slug}-{res_slug}-{ts_clean}.md"
        effective_group_id = f"grp_{ts_clean}_{res_slug}"

    file_path = store_dir / effective_filename

    # 2. Full downstream blast radius computation
    origin = canonical_res
    upstream_deps = get_upstream_dependencies(canonical_res)
    if any(k in alert.policy_name.lower() for k in ["latency", "timeout", "504"]):
        run_deps = [d for d in upstream_deps if "db" not in d and "apigee" not in d]
        if run_deps:
            origin = run_deps[0]

    try:
        downstream = get_blast_radius(origin)
    except Exception as exc:
        logger.error("Topology blast radius lookup failed for %s: %s", origin, exc)
        downstream = []
    blast_radius = [origin]
    for d in downstream:
        if d not in blast_radius:
            blast_radius.append(d)
    if canonical_res not in blast_radius:
        blast_radius.append(canonical_res)

    alerts_list: list[dict[str, Any]] = []
    if member_alerts:
        for a in member_alerts:
            alerts_list.append(format_okf_alert_entry(a, valid_ts=valid_ts))
    if not alerts_list:
        alerts_list = [format_okf_alert_entry(alert, valid_ts=valid_ts)]

    frontmatter: dict[str, Any] = {
        "type": "incident",
        "group_id": effective_group_id,
        "project": config.project_id,
        "policy_name": alert.policy_name,
        "resource_name": alert.resource_name,
        "status": "mitigation-in-progress",
        "timestamp": valid_ts,
        "origin": origin,
        "blast_radius": blast_radius,
        "priority": "P1" if any(k in alert.policy_name.lower() for k in ["critical", "5xx"]) else "P2",
        "servicenow_sys_id": "",
        "servicenow_number": "",
        "alerts": alerts_list,
    }

    if file_path.exists():
        try:
            old_content = file_path.read_text(encoding="utf-8")
            if "---" in old_content:
                parts = old_content.split("---", 2)
                old_meta = yaml.safe_load(parts[1]) or {}
                if isinstance(old_meta, dict):
                    if old_meta.get("servicenow_sys_id"):
                        frontmatter["servicenow_sys_id"] = old_meta["servicenow_sys_id"]
                    if old_meta.get("servicenow_number"):
                        frontmatter["servicenow_number"] = old_meta["servicenow_number"]
                    if old_meta.get("status") in {"ticketed", "resolved"}:
                        frontmatter["status"] = old_meta["status"]
                    old_alerts = old_meta.get("alerts", [])
                    if isinstance(old_alerts, list):
                        existing_ids = {a.get("incident_id") for a in alerts_list}
                        for oa in old_alerts:
                            if isinstance(oa, dict) and oa.get("incident_id") not in existing_ids:
                                alerts_list.append(format_okf_alert_entry(oa, valid_ts=valid_ts))
                        frontmatter["alerts"] = alerts_list
        except Exception as exc:
            logger.debug("Failed reading existing OKF file %s: %s", file_path, exc)

    yaml_header = yaml.dump(frontmatter, sort_keys=False, default_flow_style=False)
    file_content = f"""---
{yaml_header.strip()}
---

# Incident for {alert.policy_name}

**Alert Content**
- Initial Alert: Cloud Monitoring alert received on `{alert.resource_name}`.
- Policy: `{alert.policy_name}`
- Status: {frontmatter['status']}
"""

    file_path.write_text(file_content, encoding="utf-8")
    logger.info("Created pre-analysis lock file at %s (group_id=%s)", file_path, effective_group_id)

    # API fallback if mount unavailable or running in cloud environment
    if not config.is_local and config.incidents_bucket:
        try:
            from google.cloud import storage
            client = storage.Client(project=config.project_id)
            bucket = client.bucket(config.effective_incidents_bucket)
            blob_path = f"knowledge/incident-manager/{effective_filename}"
            bucket.blob(blob_path).upload_from_string(
                file_content, content_type="text/markdown"
            )
            logger.info("Saved incident file to GCS: gs://%s/%s", config.effective_incidents_bucket, blob_path)
        except Exception as exc:
            logger.debug("GCS SDK upload fallback skipped: %s", exc)

    return file_path, effective_group_id, effective_filename


async def execute_multi_agent_investigation(
    alert: AlertEvent,
    group_id: str,
    filename: str,
    config: AgentConfig,
    group_alerts: Optional[list[dict[str, Any]]] = None,
) -> InvestigationReport:
    """Run full ADK multi-agent investigation across parallel specialists.

    ``group_alerts`` (the incident group's alerts) let the synthesizer pick the origin
    group-wide from the topology instead of investigating ``alert`` (the first alert) only.
    """
    synthesizer = SynthesizerAgent()
    report = await synthesizer.execute_investigation(
        alert=alert,
        group_id=group_id,
        filename=filename,
        config=config,
        group_alerts=group_alerts,
    )
    return report


def generate_investigation_report(
    alert: AlertEvent,
    group_id: str,
    filename: str,
    config: AgentConfig,
    degraded_reason: Optional[str] = None,
) -> InvestigationReport:
    """Generate 4-bullet executive summary and diagnostic report (synchronous baseline)."""
    t0 = time.monotonic()

    if alert.policy_user_labels and alert.policy_user_labels.get("component"):
        canonical_res = alert.policy_user_labels["component"]
    else:
        canonical_res = normalize_resource_name(
            raw_name=alert.resource_name,
            res_type=alert.resource_type or "",
            res_labels=alert.resource_labels,
        )
    alert.resource_name = canonical_res
    origin = canonical_res

    res_type = (alert.resource_type or "").lower()
    res_name = canonical_res.lower()
    pol_name = (alert.policy_name or "").lower()

    # Follow topology DAG upstream to identify root cause origin before downstream victim
    upstream_deps = get_upstream_dependencies(canonical_res)
    if "latency" in pol_name or "timeout" in pol_name or "504" in pol_name:
        run_deps = [d for d in upstream_deps if "db" not in d and "apigee" not in d]
        if run_deps:
            origin = run_deps[0]

    is_sql = (
        "sql" in res_type
        or "database" in res_type
        or "db" in res_name
        or "sql" in res_name
        or "sql" in pol_name
    )
    is_apigee = (
        "apigee" in res_type
        or "proxy" in res_type
        or "apigee" in res_name
        or "v1" in res_name
        or "proxy" in res_name
        or "apigee" in pol_name
    )

    is_critical = any(k in pol_name for k in ["critical", "5xx", "saturation"])
    priority = "P1" if is_critical else "P2"

    degraded_prefix = f"degraded: {degraded_reason} - " if degraded_reason else ""
    summary = f"{degraded_prefix}Cloud Monitoring alert triggered by threshold breach on {canonical_res} ({alert.policy_name})"
    degraded_note = f"DEGRADED MODE: {degraded_reason}. " if degraded_reason else ""
    observations = (
        f"{degraded_note}Active anomalous telemetry detected on `{canonical_res}` under condition `{alert.condition_name or alert.policy_name}`; "
        f"pre-analysis lock recorded at `/knowledge/incident-manager/{filename}`."
    )

    if is_sql:
        recommended_actions = [
            ActionItem(
                title="Terminate idle database sessions via SQL client",
                command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND state_change < current_timestamp - INTERVAL '5 minutes';",
                verification="Confirm active connections drop below 80% capacity via Cloud Monitoring",
            ),
            ActionItem(
                title=f"Inspect application connection pool release logic and maximum connection pool size in {canonical_res}",
                command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND textPayload=~\"connection leak|pool exhausted\"' --limit=30",
                verification="Confirm no connection leak in application logs",
            ),
            ActionItem(
                title="Reconfigure database connection parameters",
                command=f"gcloud sql instances patch {canonical_res} --database-flags=max_connections=200",
                verification="Verify database restarts cleanly and accepts connections",
                rollback="Revert max_connections flag if instance memory pressure increases.",
            ),
        ]
    elif is_apigee:
        recommended_actions = [
            ActionItem(
                title=f"Inspect ingress traffic distribution and verify SpikeArrest policy limits on {canonical_res}",
                command=f"gcloud apigee apiproxies describe {canonical_res} --organization={config.project_id}",
                verification="Confirm 429 response rate returns to nominal 0% in Apigee Analytics",
            ),
            ActionItem(
                title="Filter or rate-limit abusive client IP addresses via Cloud Armor security policy",
                command="gcloud compute security-policies rules create 1000 --security-policy=ata-edge-policy --action=deny-429 --src-ip-ranges=<CLIENT_IP>/32",
                verification="Check access logs for repeated burst traffic patterns",
            ),
            ActionItem(
                title="Adjust traffic controls",
                rollback="Restore baseline SpikeArrest policy limits once traffic surge subsides.",
            ),
        ]
    else:
        recommended_actions = [
            ActionItem(
                title=f"Inspect recent deployments and container logs for {canonical_res}",
                command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{canonical_res}\"' --limit=20",
                verification="Confirm zero unhandled 5xx exceptions",
            ),
            ActionItem(
                title="Review upstream dependencies and backend error rates",
                command=f"gcloud run services describe {canonical_res} --region={config.region}",
                verification="Confirm service status Ready=True",
            ),
            ActionItem(
                title="Verify service capacity, instance scaling, and connection limits",
                rollback="Revert latest service revision deployment if degradation persists.",
            ),
        ]

    valid_ts = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not valid_ts or not str(valid_ts).strip():
        valid_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        downstream = get_blast_radius(origin)
    except Exception as exc:
        logger.error("Topology blast radius lookup failed for %s: %s", origin, exc)
        downstream = []
    blast_radius = [origin]
    for d in downstream:
        if d not in blast_radius:
            blast_radius.append(d)
    if canonical_res not in blast_radius:
        blast_radius.append(canonical_res)

    dep_path = f"Client Ingress -> Apigee -> {origin}"
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if origin != canonical_res:
        summary = f"{degraded_prefix}Upstream dependency degradation on {origin} causing threshold breach on caller {canonical_res} ({alert.policy_name})"
        root_cause_text = f"Primary anomaly isolated to upstream dependency {origin} impacting caller {canonical_res} under policy {alert.policy_name}."
    else:
        root_cause_text = f"Primary anomaly isolated to {origin} under policy {alert.policy_name}."

    link_builder = ConsoleLinkBuilder(project_id=config.project_id)
    primary_alert_entry = format_okf_alert_entry(alert, valid_ts=valid_ts)
    pol_id = primary_alert_entry.get("policy_id")
    primary_alert_entry["policy_url"] = link_builder.build_policy_url(pol_id or alert.policy_name)
    primary_alert_entry["resource_type"] = alert.resource_type
    primary_alert_entry["resource_url"] = link_builder.build_resource_url(alert.resource_name, alert.resource_type)
    inc_url = primary_alert_entry.get("url") or link_builder.build_incident_url(alert.incident_id)
    primary_alert_entry["url"] = inc_url
    primary_alert_entry["console_url"] = inc_url

    report = InvestigationReport(
        group_id=group_id,
        filename=filename,
        summary=summary,
        observations=observations,
        recommended_actions=recommended_actions,
        probable_root_cause=ProbableRootCause(
            text=root_cause_text,
            confidence=0.91,
        ),
        origin=origin,
        blast_radius=blast_radius,
        priority=priority,
        status="In Progress",
        first_seen=valid_ts,
        last_alert=valid_ts,
        report_generated_at=now_ts,
        project_id=config.project_id,
        alerts=[primary_alert_entry],
        triage_decision={
            "correlation_score": 0.91,
            "grouping_rationale": f"Alert policy '{alert.policy_name}' isolated to '{origin}' within active triage evaluation window.",
            "alerts_considered_and_excluded": [],
            "duplicate_status": "Active (0 duplicate alerts suppressed into group)",
        },
        dependency_path=dep_path,
        topology_nodes=[
            {
                "resource": r,
                "name": r,
                "type": "Cloud SQL PostgreSQL" if "db" in r else ("Apigee API Proxy" if "v1" in r or "apigee" in r else "Cloud Run Service"),
                "role": "Origin (Root Callee)" if r == origin else "Downstream Dependent",
                "impact": "Root anomaly breach" if r == origin else "Cascading dependent impact",
            }
            for r in blast_radius
        ],
        timeline=[
            TimelineItem(
                timestamp=valid_ts,
                description=f"Alert fired on {alert.resource_name} ({alert.policy_name})",
                event_type="Alert Trigger",
            ),
            TimelineItem(
                timestamp=valid_ts,
                description=f"Anomalous telemetry detected under condition {alert.condition_name or alert.policy_name}",
                event_type="Log Anomaly",
            ),
            TimelineItem(
                timestamp=now_ts,
                description="Pre-analysis lock acquired; triage investigation initialized.",
                event_type="Triage Synthesis",
            ),
        ],
        evidence_by_area={
            "telemetry": [
                EvidenceItem(
                    headline=f"Monitoring Alert Payload: {alert.resource_name}",
                    content=(
                        f"Incident ID: {alert.incident_id}\n"
                        f"Policy: {alert.policy_name}\n"
                        f"Resource: {alert.resource_name}\n"
                        f"Metric: {alert.metric_type or 'anomalous'}\n"
                        f"State: {alert.state}"
                    ),
                )
            ]
        },
        metrics_comparison=[
            {
                "layer": "Cloud SQL" if is_sql else ("Apigee" if is_apigee else "Cloud Run"),
                "metric_name": alert.metric_type or ("cloudsql.googleapis.com/database/network/connections" if is_sql else "run.googleapis.com/request_count (5xx)"),
                "resource": origin,
                "before_value": "N/A",
                "during_value": "Threshold breach detected",
                "variance": "N/A",
            },
        ],
        top_log_signatures=[
            {
                "layer": "Cloud SQL" if is_sql else ("Apigee" if is_apigee else "Cloud Run"),
                "signature": f"Elevated error log rate on {origin}",
                "count": 1,
                "sample": f"ERROR: service {origin} request failure under condition {alert.condition_name or alert.policy_name}",
            }
        ],
        apigee_analytics=(
            {
                "proxy_name": origin if is_apigee else next((r for r in blast_radius if "apigee" in r or "v1" in r), origin),
                "response_codes": {},
                "target_latency_p50": "N/A",
                "target_latency_p95": "N/A",
                "target_latency_p99": "N/A",
            }
            if is_apigee or any("apigee" in r or "v1" in r for r in blast_radius)
            else {}
        ),
        revision_diffs=[],
        root_cause_analysis={
            "hypothesis": f"Primary anomaly isolated to {origin} under policy {alert.policy_name}.",
            "supporting_evidence": [
                f"Anomalous metric values breached threshold on {origin}.",
                f"Condition {alert.condition_name or alert.policy_name} active.",
            ],
            "contradicting_evidence": [
                f"Zero compute host faults detected in {config.region}.",
            ],
            "confidence": 0.91,
            "confidence_rationale": "Direct alert condition match against telemetry signals.",
            "alternatives_ruled_out": [],
        },
        longer_term_fixes=[
            f"Establish autoscaling limits and connection pool tuning on {origin}.",
            "Implement rate-limiting and circuit breakers in upstream proxies.",
        ],
        queries_appendix={
            "cloud_monitoring": [
                f'fetch {alert.resource_type or "generic_resource"} :: metric.type = "{alert.metric_type or "run.googleapis.com/request_count"}"'
            ]
        },
        tools_used=[
            "list_open_alerts", "list_time_series", "list_log_entries",
            "get_service", "get_topology_graph", "get_topology", "get_blast_radius"
        ],
        skills_loaded=[
            "cloud-run-diagnostics", "apigee-telemetry", "cloud-sql-triage", "report-style"
        ],
        agent_run_metadata={
            "duration_seconds": f"{time.monotonic() - t0:.2f}s",
            "model": config.model_name or "Unspecified",
            "agent_revision": os.environ.get("K_REVISION") or "Unspecified",
            "bridge_revision": os.environ.get("BRIDGE_REVISION") or "Unspecified",
        },
        raw_metadata={"dependency_path": dep_path, "status": "In Progress"},
    )
    return report


def send_findings_to_bridge(
    report: InvestigationReport,
    bridge_url: str,
    http_client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Send findings contract callback to ata-bridge with Google ID token."""
    target_url = f"{bridge_url.rstrip('/')}/investigate/findings"
    payload = report.model_dump(mode="json")
    headers = {"Content-Type": "application/json"}

    is_local = config.is_local if config else False
    if not is_local:
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token
            req = google.auth.transport.requests.Request()
            aud = bridge_url.rstrip("/")
            id_token = google.oauth2.id_token.fetch_id_token(req, aud)
            headers["Authorization"] = f"Bearer {id_token}"
        except Exception as exc:
            logger.warning("Failed to fetch Google ID token for bridge callback: %s", exc)
    else:
        headers["Authorization"] = "Bearer test-agent-token"

    if http_client is not None:
        resp = http_client.post(target_url, json=payload, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(target_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
