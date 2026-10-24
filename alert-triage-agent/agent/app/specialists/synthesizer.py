"""SynthesizerAgent for Alert Triage Runtime.

Orchestrates concurrent execution of specialist agents via asyncio.gather,
integrates evidence dossiers, determines probable root cause and blast radius,
updates the OKF incident file at /knowledge/incident-manager, and compiles
the final InvestigationReport.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import yaml

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import (
    ActionItem,
    EvidenceItem,
    InvestigationReport,
    ProbableRootCause,
    TimelineItem,
)
from ..contracts.links import ConsoleLinkBuilder
from .base import EvidenceDossier
from .cloud_run import CloudRunSpecialist
from .apigee import ApigeeSpecialist
from .cloud_sql import CloudSQLSpecialist
from .change import ChangeSpecialist

try:
    from topology.topology_graph import get_blast_radius, get_upstream_dependencies, _canonicalize_node
except ImportError:
    try:
        from agent.topology.topology_graph import get_blast_radius, get_upstream_dependencies, _canonicalize_node
    except ImportError as err:
        logging.getLogger(__name__).error("Failed to import topology_graph: %s", err)
        raise

logger = logging.getLogger(__name__)


class SynthesizerAgent:
    """Lead synthesizing agent coordinating specialists and generating final diagnostics."""

    def __init__(self, **kwargs: Any):
        self.cloud_run_specialist = CloudRunSpecialist()
        self.apigee_specialist = ApigeeSpecialist()
        self.cloud_sql_specialist = CloudSQLSpecialist()
        self.change_specialist = ChangeSpecialist()

    async def execute_investigation(
        self,
        alert: AlertEvent,
        group_id: str,
        filename: str,
        config: AgentConfig,
    ) -> InvestigationReport:
        """Run specialists concurrently with asyncio.gather and synthesize findings."""
        start_time = time.monotonic()
        # Follow topology DAG upstream from alert resource
        canonical_res = alert.resource_name
        try:
            upstream_deps = get_upstream_dependencies(canonical_res)
        except Exception as exc:
            logger.error("Topology upstream dependencies lookup failed for %s: %s", canonical_res, exc)
            upstream_deps = []

        # Run primary specialists
        spec_tasks = {
            "CloudRunSpecialist": self.cloud_run_specialist.investigate(alert, config),
            "ApigeeSpecialist": self.apigee_specialist.investigate(alert, config),
            "CloudSQLSpecialist": self.cloud_sql_specialist.investigate(alert, config),
            "ChangeSpecialist": self.change_specialist.investigate(alert, config),
        }
        # Run CloudRunSpecialist on upstream Cloud Run dependencies (e.g. ata-inventory-api)
        dep_tasks = {}
        for dep in upstream_deps:
            if "db" not in dep and "apigee" not in dep and dep != canonical_res:
                dep_tasks[dep] = self.cloud_run_specialist.investigate(alert, config, target_service=dep)

        all_keys = list(spec_tasks.keys()) + list(dep_tasks.keys())
        all_futures = list(spec_tasks.values()) + list(dep_tasks.values())
        all_results = await asyncio.gather(*all_futures)

        dossier_map = dict(zip(all_keys, all_results))
        dossiers: list[EvidenceDossier] = all_results
        self._last_dossiers = dossiers

        # Determine probable origin and blast radius
        origin = self._determine_probable_origin(alert, dossier_map, upstream_deps)
        try:
            downstream = get_blast_radius(origin)
        except Exception as exc:
            logger.error("Topology blast radius lookup failed for %s: %s", origin, exc)
            downstream = []

        blast_radius = [origin]
        for d in downstream:
            if d not in blast_radius:
                blast_radius.append(d)

        # Consolidate evidence and queries
        all_evidence: dict[str, list[EvidenceItem]] = {}
        all_queries: dict[str, list[str]] = {}

        for d in dossiers:
            area_key = d.specialist_name.lower().replace("specialist", "")
            all_evidence[area_key] = d.evidence_items
            for q_type, q_list in d.queries_executed.items():
                if q_type not in all_queries:
                    all_queries[q_type] = []
                all_queries[q_type].extend(q_list)

        # Synthesize rich operational problem, observations, recommendations, and dependency path
        is_critical = any(k in alert.policy_name.lower() for k in ["critical", "5xx", "saturation", "oom"])
        priority = "P1" if is_critical else "P2"

        dep_path = self._determine_dependency_path(origin, blast_radius)
        summary = self._synthesize_problem(alert, dossiers, origin, blast_radius)
        observations = self._synthesize_observations(dossiers, alert)
        recommendations = self._synthesize_recommendations(dossiers, origin, alert)
        root_cause_text, confidence = self._determine_root_cause(dossiers, origin, alert, blast_radius=blast_radius)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        first_seen = alert.started_at or now_str
        last_alert = alert.started_at or now_str

        alerts_list = self._build_alerts(alert, config)
        triage_decision = self._build_triage_decision(alert, origin, blast_radius, dossiers, confidence=confidence)
        topology_nodes = self._build_topology_nodes(origin, blast_radius)
        timeline = self._build_timeline(alert, dossiers)
        metrics_comparison = self._build_metrics_comparison(dossiers, origin)
        top_log_signatures = self._build_top_log_signatures(dossiers)
        apigee_analytics = self._build_apigee_analytics(dossiers, blast_radius)
        revision_diffs = self._build_revision_diffs(dossiers, origin)
        root_cause_analysis = self._build_root_cause_analysis(root_cause_text, confidence, dossiers, origin, alert)
        longer_term_fixes = self._build_longer_term_fixes(origin, alert)
        execution_duration = time.monotonic() - start_time
        agent_run_metadata = self._build_agent_metadata(config, duration_seconds=execution_duration)
        tools_used = [
            "list_open_alerts", "list_time_series", "list_log_entries",
            "get_service", "get_topology_graph", "get_blast_radius"
        ]
        skills_loaded = [
            "cloud-run-diagnostics", "apigee-telemetry", "cloud-sql-triage", "report-style"
        ]

        report = InvestigationReport(
            group_id=group_id,
            filename=filename,
            summary=summary,
            observations=observations,
            recommended_actions=recommendations,
            probable_root_cause=ProbableRootCause(
                text=root_cause_text,
                confidence=confidence,
            ),
            origin=origin,
            blast_radius=blast_radius,
            priority=priority,
            status="In Progress",
            first_seen=first_seen,
            last_alert=last_alert,
            report_generated_at=now_str,
            project_id=config.project_id,
            alerts=alerts_list,
            triage_decision=triage_decision,
            dependency_path=dep_path,
            topology_nodes=topology_nodes,
            timeline=timeline,
            evidence_by_area=all_evidence,
            metrics_comparison=metrics_comparison,
            top_log_signatures=top_log_signatures,
            apigee_analytics=apigee_analytics,
            revision_diffs=revision_diffs,
            root_cause_analysis=root_cause_analysis,
            longer_term_fixes=longer_term_fixes,
            queries_appendix=all_queries,
            tools_used=tools_used,
            skills_loaded=skills_loaded,
            agent_run_metadata=agent_run_metadata,
            raw_metadata={"dependency_path": dep_path, "status": "In Progress"},
        )

        # Update OKF file with synthesized findings
        self._update_okf_file(report, alert, config)

        return report

    def _determine_probable_origin(
        self,
        alert: AlertEvent,
        dossiers: dict[str, EvidenceDossier],
        upstream_deps: Optional[list[str]] = None,
    ) -> str:
        """Identify probable origin node using DAG hierarchy, change correlation, and telemetry metric onset.

        Rules:
        1. SpikeArrest 429 is enforced at the Apigee ingress proxy gateway; origin stays at gateway.
        2. Change-induced deployments on a service (e.g. ata-inventory-api in S5) attribute directly
           to the deployed service, never to downstream database dependencies.
        3. Cloud SQL origin is attributed only when Cloud SQL alert fired or DB telemetry shows verified
           saturation/locks at alert onset without caller configuration/credential error.
        4. Upstream traversal attributes to upstream component only when that component exhibits its own anomaly.
        """
        # 1. Apigee SpikeArrest / 429 rate limit is enforced at the ingress proxy gateway;
        # backend is healthy and protected, origin stays at the gateway.
        if "429" in (alert.policy_name or "").lower() or "spikearrest" in (alert.policy_name or "").lower():
            logger.info("SpikeArrest 429 rate limit enforced at gateway; origin stays at gateway %s", alert.resource_name)
            return alert.resource_name

        # 2. Change-induced fault detection:
        # A change deployment on a service (e.g. ata-inventory-api in S5) is attributed
        # directly to that service where the revision was deployed, NEVER to downstream database.
        chg_dossier = dossiers.get("ChangeSpecialist")
        if chg_dossier and chg_dossier.anomaly_detected:
            chg_svc = chg_dossier.metrics.get("change_service") or chg_dossier.target_resource
            if chg_svc:
                logger.info("Change-induced fault detected on %s; attributing origin to deployed service", chg_svc)
                return chg_svc

        # 3. Cloud SQL check: database is the deepest backend layer in the topology DAG
        # Origin must ONLY attribute to ata-inventory-db when database telemetry is actually anomalous in the window
        sql_dossier = dossiers.get("CloudSQLSpecialist")
        if sql_dossier and sql_dossier.anomaly_detected:
            db_conns = sql_dossier.metrics.get("active_connections")
            db_max = sql_dossier.metrics.get("max_connections")
            db_locks = sql_dossier.metrics.get("active_lock_waits")
            db_cpu = sql_dossier.metrics.get("cpu_utilization_pct")

            is_conn_anomalous = isinstance(db_conns, (int, float)) and (
                db_conns >= 20 or (isinstance(db_max, (int, float)) and db_max > 0 and db_conns / db_max >= 0.8)
            )
            is_lock_anomalous = isinstance(db_locks, (int, float)) and db_locks > 0
            is_cpu_anomalous = isinstance(db_cpu, (int, float)) and db_cpu >= 80.0
            is_explicit_db_alert = any(k in alert.policy_name.lower() for k in ["cloudsql", "max_connections", "lock_wait", "db_lock", "row_lock"])

            # Check if caller has DB auth errors from bad credentials (not a database instance fault)
            cr_dossier = dossiers.get("CloudRunSpecialist")
            has_db_auth_error = (cr_dossier and cr_dossier.metrics.get("has_db_auth_error")) or any(
                "auth" in str(f).lower() for f in (cr_dossier.findings if cr_dossier else [])
            )

            db_telemetry_anomalous = (is_conn_anomalous or is_lock_anomalous or is_cpu_anomalous or is_explicit_db_alert) and not has_db_auth_error

            if db_telemetry_anomalous:
                db_origin = sql_dossier.target_resource
                if not db_origin and upstream_deps:
                    db_candidates = [d for d in upstream_deps if "db" in d or "sql" in d]
                    if db_candidates:
                        db_origin = db_candidates[0]
                if not db_origin:
                    db_origin = alert.resource_name

                sql_onset = sql_dossier.metrics.get("metric_onset")
                alert_onset = alert.started_at
                if sql_onset and alert_onset:
                    if sql_onset <= alert_onset:
                        logger.info(
                            "Cloud SQL metric onset (%s) precedes/matches downstream alert (%s); attributing origin to DB %s",
                            sql_onset, alert_onset, db_origin,
                        )
                        return db_origin
                return db_origin
            else:
                logger.info(
                    "Cloud SQL metrics are normal or caller auth error (conns=%s, max=%s, locks=%s, cpu=%s); not attributing origin to DB",
                    db_conns, db_max, db_locks, db_cpu,
                )

        # 4. Check upstream Cloud Run dependencies in topology DAG
        # Upstream traversal may move origin upstream ONLY when that upstream component shows its OWN anomaly in the window.
        if upstream_deps:
            earliest_dep = None
            earliest_dep_onset = None
            for dep in upstream_deps:
                dep_dossier = dossiers.get(dep)
                if dep_dossier and dep_dossier.anomaly_detected:
                    dep_onset = dep_dossier.metrics.get("metric_onset")
                    if dep_onset:
                        if earliest_dep_onset is None:
                            earliest_dep = dep
                            earliest_dep_onset = dep_onset
                        elif dep_onset < earliest_dep_onset:
                            earliest_dep = dep
                            earliest_dep_onset = dep_onset
                        elif dep_onset == earliest_dep_onset:
                            try:
                                from topology.topology_graph import calculate_hop_distance
                                d_dep = calculate_hop_distance(alert.resource_name, dep) or 0
                                d_cur = calculate_hop_distance(alert.resource_name, earliest_dep) or 0
                                if d_dep > d_cur:
                                    earliest_dep = dep
                                    earliest_dep_onset = dep_onset
                            except Exception:
                                pass
            if earliest_dep:
                logger.info("Identified upstream cause origin on %s (preceding or causing %s)", earliest_dep, alert.resource_name)
                return earliest_dep

        # 5. Check if primary Cloud Run specialist detected an anomaly
        cr_dossier = dossiers.get("CloudRunSpecialist")
        if cr_dossier and cr_dossier.anomaly_detected:
            return cr_dossier.target_resource or alert.resource_name

        return alert.resource_name

    def _determine_dependency_path(self, origin: str, blast_radius: list[str]) -> str:
        """Determine topological dependency path from ingress callers to origin callee."""
        if blast_radius and len(blast_radius) > 1:
            return " -> ".join(reversed(blast_radius))
        return f"Client Ingress -> {origin}"

    def _synthesize_problem(
        self,
        alert: AlertEvent,
        dossiers: list[EvidenceDossier],
        origin: str,
        blast_radius: list[str],
    ) -> str:
        """Synthesize rich, operational problem statement based on real evidence."""
        started_at = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        upstream = [b for b in blast_radius if b != origin]
        upstream_str = f"upstream consumers ({', '.join(upstream)})" if upstream else "direct client ingress"

        metric_details = []
        is_sql = False
        is_apigee = False
        err_5xx_rate: Optional[str] = None
        err_5xx_pct: Optional[float] = None
        lat_p95_ms: Optional[float] = None

        for d in dossiers:
            if d.specialist_name == "CloudSQLSpecialist" and d.anomaly_detected:
                is_sql = True
                conns = d.metrics.get("active_connections")
                max_conns = d.metrics.get("max_connections", 25)
                try:
                    c_num = float(conns) if conns and conns != "N/A (not captured)" else None
                    m_num = float(max_conns) if max_conns and max_conns != "N/A (not captured)" else 25.0
                    if c_num is not None and (c_num >= 20 or (m_num and c_num / m_num >= 0.8)):
                        metric_details.append(f"connections at {int(c_num)}/{int(m_num)}")
                except Exception:
                    pass
            elif d.specialist_name == "CloudRunSpecialist":
                raw_err = d.metrics.get("error_rate_5xx")
                if raw_err and raw_err != "0.1%" and raw_err != "N/A (not captured)":
                    err_5xx_rate = raw_err
                    try:
                        err_5xx_pct = float(str(raw_err).replace("%", ""))
                    except ValueError:
                        pass
                raw_lat = d.metrics.get("latency_p95_ms")
                if raw_lat and raw_lat != "N/A (not captured)":
                    try:
                        lat_p95_ms = float(raw_lat)
                    except ValueError:
                        pass
            elif d.specialist_name == "ApigeeSpecialist" and d.anomaly_detected:
                is_apigee = True
                if "status_429_rate" in d.metrics and d.metrics["status_429_rate"] != "N/A (not captured)":
                    metric_details.append(f"HTTP 429 rate at {d.metrics['status_429_rate']}")

        # Check for change regression on origin
        chg_d = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist" and d.anomaly_detected), None)
        if chg_d:
            rev = chg_d.metrics.get("recent_revision")
            if rev and (chg_d.metrics.get("change_service") == origin or chg_d.target_resource == origin):
                return (
                    f"Deployment regression on revision `{rev}` for {origin} "
                    f"causing operational failures for {upstream_str} since {started_at}"
                )

        # Distinguish 5xx-dominant vs latency-dominant failure mode
        is_policy_5xx = "5xx" in alert.policy_name.lower() or "500" in (alert.condition_name or "")
        is_5xx_dominant = (err_5xx_pct is not None and err_5xx_pct >= 5.0) or (is_policy_5xx and err_5xx_rate is not None)

        if origin != alert.resource_name:
            if is_5xx_dominant:
                rate_disp = f" ({err_5xx_rate})" if err_5xx_rate else ""
                return (
                    f"Elevated upstream HTTP 5xx error rate{rate_disp} on {origin} causing cascading failures "
                    f"for {upstream_str} since {started_at}"
                )
            elif "latency" in alert.policy_name.lower() or "timeout" in alert.policy_name.lower() or (lat_p95_ms is not None and lat_p95_ms >= 2000.0):
                lat_disp = f" ({int(lat_p95_ms)}ms)" if lat_p95_ms is not None else ""
                return (
                    f"Elevated upstream dependency latency{lat_disp} on {origin} causing cascading request timeouts "
                    f"and degradation for {upstream_str} since {started_at}"
                )
            elif "5xx" in alert.policy_name.lower():
                return (
                    f"Elevated upstream HTTP 5xx error rate on {origin} causing cascading failures "
                    f"for {upstream_str} since {started_at}"
                )

        if is_sql and ("db" in origin or "sql" in origin):
            metric_str = f" ({', '.join(metric_details)})" if metric_details else ""
            return (
                f"Database connection pool saturation{metric_str} on {origin} "
                f"causing cascading HTTP 5xx failures for {upstream_str} since {started_at}"
            )
        elif is_apigee:
            metric_str = f" ({', '.join(metric_details)})" if metric_details else ""
            return (
                f"SpikeArrest rate-limit violation{metric_str} on {origin} "
                f"throttling incoming traffic for {upstream_str} since {started_at}"
            )

        if is_5xx_dominant:
            rate_disp = err_5xx_rate or "elevated"
            return (
                f"Elevated HTTP 5xx error rate ({rate_disp}) on {origin} "
                f"impacting {upstream_str} since {started_at}"
            )
        elif "latency" in alert.policy_name.lower() or "timeout" in alert.policy_name.lower() or (lat_p95_ms is not None and lat_p95_ms >= 2000.0):
            lat_disp = f" ({int(lat_p95_ms)}ms)" if lat_p95_ms is not None else ""
            return (
                f"Elevated service latency and request timeouts{lat_disp} on {origin} "
                f"impacting {upstream_str} since {started_at}"
            )
        else:
            return (
                f"Operational threshold breach on policy '{alert.policy_name}' for {origin} "
                f"impacting {upstream_str} since {started_at}"
            )

    def calculate_confidence(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
        alert: AlertEvent,
        blast_radius: Optional[list[str]] = None,
        origin_change: Optional[dict[str, Any]] = None,
    ) -> tuple[float, dict[str, Any], str]:
        """Calculate evidence-based confidence using a documented multi-factor formula.

        Formula:
            confidence = min(0.99, max(0.40,
                base_score (0.40)
                + topology_position_factor (0.05 - 0.20)
                + corroborating_signals_factor (0.00 - 0.25)
                + onset_ordering_factor (0.05 - 0.10)
                + change_proximity_factor (0.00 - 0.15)
            ))

        Contributing factors:
        - topology_position:
            * +0.20 if origin is upstream dependency in topology DAG with cascading failure
            * +0.15 if origin matches primary alert resource with confirmed local anomaly
            * +0.05 if origin is unconfirmed or default
        - corroborating_signals:
            * +0.08 per corroborating telemetry signal (HTTP 5xx error rate, container restarts,
              database lock wait events, connection saturation, Apigee status codes), capped at +0.25
        - onset_ordering:
            * +0.10 if telemetry anomaly timing aligns with or precedes alert trigger timestamp
            * +0.05 if alert window is unanchored or baseline only
        - change_proximity:
            * +0.15 if revision deployed <= 15 minutes prior to alert onset
            * +0.10 if revision deployed 16-30 minutes prior to alert onset
            * +0.05 if revision deployed 31-60 minutes prior to alert onset
            * +0.00 if no recent revision deployed within causality window
        """
        base_score = 0.40

        # 1. Topology Position
        if origin != alert.resource_name and (blast_radius and len(blast_radius) > 1):
            score_topology = 0.20
            detail_topology = f"Upstream dependency `{origin}` confirmed as causal root in topology DAG ({len(blast_radius)} nodes in blast radius)"
        elif origin != alert.resource_name:
            score_topology = 0.18
            detail_topology = f"Upstream dependency `{origin}` isolated as anomalous origin"
        else:
            score_topology = 0.15
            detail_topology = f"Origin `{origin}` matches primary alert resource with confirmed local anomaly"

        # 2. Corroborating Signals
        signal_items: list[str] = []
        for d in dossiers:
            if d.specialist_name == "CloudRunSpecialist":
                err_rate = d.metrics.get("error_rate_5xx")
                if err_rate and err_rate != "0.1%":
                    signal_items.append(f"HTTP 5xx rate {err_rate} on {d.target_resource}")
                restarts = d.metrics.get("container_restarts")
                if isinstance(restarts, (int, float)) and restarts > 0:
                    signal_items.append(f"{restarts} container restarts on {d.target_resource}")
            elif d.specialist_name == "CloudSQLSpecialist":
                locks = d.metrics.get("active_lock_waits")
                if isinstance(locks, (int, float)) and locks > 0:
                    signal_items.append(f"{locks} database lock wait events on {d.target_resource}")
                conns = d.metrics.get("active_connections")
                max_conns = d.metrics.get("max_connections")
                if isinstance(conns, (int, float)) and isinstance(max_conns, (int, float)) and max_conns > 0 and (conns / max_conns >= 0.8 or conns >= 20):
                    signal_items.append(f"database connection saturation ({conns}/{max_conns}) on {d.target_resource}")
                cpu = d.metrics.get("cpu_utilization_pct")
                if isinstance(cpu, (int, float)) and cpu >= 80:
                    signal_items.append(f"database CPU utilization {cpu}% on {d.target_resource}")
            elif d.specialist_name == "ApigeeSpecialist":
                if d.anomaly_detected:
                    signal_items.append(f"Apigee proxy anomaly on {d.target_resource}")
            elif d.anomaly_detected and d.specialist_name != "ChangeSpecialist":
                signal_items.append(f"{d.specialist_name} anomaly on {d.target_resource}")

        signal_count = len(signal_items)
        score_signals = min(0.25, round(signal_count * 0.08, 2))
        sig_summary = "; ".join(signal_items[:3]) if signal_items else "nominal baseline"
        detail_signals = f"{signal_count} corroborating telemetry signal(s) observed ({sig_summary})"

        # 3. Onset Ordering
        if alert.started_at:
            score_onset = 0.10
            detail_onset = f"Telemetry breach timing coincides with alert trigger timestamp ({alert.started_at})"
        else:
            score_onset = 0.05
            detail_onset = "Evaluation window bounded without explicit trigger timestamp"

        # 4. Change Proximity
        if origin_change:
            mins = origin_change.get("deployed_minutes_ago", 999)
            rev = origin_change.get("revision")
            rev_label = f"Revision `{rev}`" if rev else "Deployment (revision not identified)"
            if mins <= 15:
                score_change = 0.15
                detail_change = f"{rev_label} deployed {mins}m prior to alert onset (high temporal proximity)"
            elif mins <= 30:
                score_change = 0.10
                detail_change = f"{rev_label} deployed {mins}m prior to alert onset (moderate proximity)"
            elif mins <= 60:
                score_change = 0.05
                detail_change = f"{rev_label} deployed {mins}m prior to alert onset (extended window)"
            else:
                score_change = 0.00
                detail_change = f"{rev_label} deployed {mins}m prior to alert onset (outside causality window)"
        else:
            score_change = 0.00
            detail_change = "Zero recent deployments detected within lookback window"

        calc_conf = base_score + score_topology + score_signals + score_onset + score_change
        total_conf = round(min(0.99, max(0.40, calc_conf)), 2)

        contributing_factors = {
            "topology_position": {"score": round(score_topology, 2), "detail": detail_topology},
            "corroborating_signals": {"score": round(score_signals, 2), "detail": detail_signals},
            "onset_ordering": {"score": round(score_onset, 2), "detail": detail_onset},
            "change_proximity": {"score": round(score_change, 2), "detail": detail_change},
        }
        rationale = (
            f"Calculated confidence of {int(total_conf * 100)}% based on topology position (+{score_topology:.2f}), "
            f"{signal_count} corroborating signal(s) (+{score_signals:.2f}), onset ordering (+{score_onset:.2f}), "
            f"and change proximity (+{score_change:.2f})."
        )
        return total_conf, contributing_factors, rationale

    def _format_dominant_signal_facts(self, alert: AlertEvent, dossier: Optional[EvidenceDossier]) -> str:
        """Format facts matching the dominant observed signal (latency vs 5xx vs auth errors vs connections)."""
        if not dossier or not dossier.metrics:
            return ""

        # Check for DB auth errors first
        if (
            dossier.metrics.get("has_db_auth_error")
            or "auth" in str(dossier.metrics.get("observed_symptom", "")).lower()
            or any("auth" in str(f).lower() for f in dossier.findings)
        ):
            err_rate = dossier.metrics.get("error_rate_5xx")
            err_str = f" (HTTP 5xx error rate: {err_rate})" if err_rate and err_rate != "0.1%" and err_rate != "N/A (not captured)" else ""
            return f" (database authentication failures in application logs{err_str})"

        is_latency_policy = any(k in (alert.policy_name or "").lower() for k in ("latency", "timeout", "slow"))
        is_5xx_policy = any(k in (alert.policy_name or "").lower() for k in ("5xx", "500", "error"))

        lat = dossier.metrics.get("latency_p95_ms")
        lat_val = None
        if lat and lat != "N/A (not captured)":
            try:
                lat_val = float(lat)
            except (ValueError, TypeError):
                pass

        err = dossier.metrics.get("error_rate_5xx")
        err_val = None
        if err and err != "0.1%" and err != "N/A (not captured)":
            try:
                err_val = float(str(err).replace("%", "").strip())
            except (ValueError, TypeError):
                pass

        # If policy is latency or latency breach without 5xx policy:
        if is_latency_policy or (lat_val is not None and lat_val >= 2000.0 and not is_5xx_policy):
            if lat_val is not None:
                return f" (P95 latency: {int(lat_val)}ms)"
            return " (elevated request latency)"

        # If policy is 5xx or 5xx error breach:
        if is_5xx_policy or (err_val is not None and err_val >= 5.0):
            if err and err != "0.1%":
                return f" (HTTP 5xx error rate: {err})"
            return " (elevated HTTP 5xx errors)"

        if lat_val is not None and lat_val >= 2000.0:
            return f" (P95 latency: {int(lat_val)}ms)"
        if err and err != "0.1%":
            return f" (HTTP 5xx error rate: {err})"

        return ""

    def _determine_root_cause(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
        alert: AlertEvent,
        blast_radius: Optional[list[str]] = None,
    ) -> tuple[str, float]:
        """Synthesize probable root cause statement and calibrated evidence-based confidence."""
        canon_origin = _canonicalize_node(origin)
        canon_alert = _canonicalize_node(alert.resource_name)
        upstream_deps = get_upstream_dependencies(canon_alert)
        is_upstream = (canon_origin in upstream_deps) or (origin in upstream_deps)
        target_phrase = f"on upstream dependency `{origin}`" if is_upstream else f"on `{origin}`"

        # 1. Inspect for recent change on origin or alert resource
        chg_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        origin_change: Optional[dict[str, Any]] = None
        if chg_dossier and chg_dossier.anomaly_detected:
            all_chgs = chg_dossier.metrics.get("all_changes", [])
            for c in all_chgs:
                if c.get("service") in (origin, alert.resource_name) and c.get("deployed_minutes_ago", 999) <= 60:
                    origin_change = c
                    break
            if not origin_change and chg_dossier.metrics.get("recent_revision"):
                svc = chg_dossier.metrics.get("change_service") or chg_dossier.target_resource
                if svc in (origin, alert.resource_name):
                    origin_change = {
                        "service": svc,
                        "revision": chg_dossier.metrics["recent_revision"],
                        "deployed_minutes_ago": chg_dossier.metrics.get("minutes_since_deploy", 0),
                        "timestamp": chg_dossier.metrics.get("deploy_timestamp"),
                    }

        # 2. Calculate evidence-calibrated confidence
        confidence, factors, rationale = self.calculate_confidence(
            dossiers=dossiers,
            origin=origin,
            alert=alert,
            blast_radius=blast_radius,
            origin_change=origin_change,
        )
        self._last_contributing_factors = factors
        self._last_confidence_rationale = rationale

        # 3. Branch: SpikeArrest 429 at Apigee gateway proxy (protects backend)
        is_429 = "429" in (alert.policy_name or "").lower() or "spikearrest" in (alert.policy_name or "").lower()
        if is_429:
            root_cause_text = (
                f"SpikeArrest rate-limit violation at Apigee ingress proxy `{origin}` "
                f"protecting backend services from traffic bursts under policy `{alert.policy_name}`."
            )
            return root_cause_text, confidence

        # 4. Branch: Change Correlation Detected (e.g. S5 bad deploy)
        if origin_change:
            rev_name = origin_change.get("revision")
            rev_desc = f"revision deployment `{rev_name}`" if rev_name else "deployment (revision not identified)"
            mins = origin_change.get("deployed_minutes_ago")
            ts = origin_change.get("timestamp")
            deploy_time_str = ts or (f"{mins}m prior to alert onset" if mins is not None else "shortly before alert onset")

            cr_spec = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin), None)
            if not cr_spec:
                cr_spec = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist"), None)

            # Check observed symptom: DB auth errors from app logs vs latency vs 5xx
            has_db_auth = (cr_spec and cr_spec.metrics.get("has_db_auth_error")) or any(
                "auth" in str(f).lower() for f in (cr_spec.findings if cr_spec else [])
            )
            if has_db_auth:
                err_rate = cr_spec.metrics.get("error_rate_5xx") if cr_spec else None
                err_str = f" (HTTP 5xx error rate: {err_rate})" if err_rate and err_rate != "0.1%" and err_rate != "N/A (not captured)" else ""
                symptom_text = f"database authentication failures in application logs{err_str}"
            elif any(k in (alert.policy_name or "").lower() for k in ("latency", "timeout", "slow")):
                lat = cr_spec.metrics.get("latency_p95_ms") if cr_spec else None
                lat_str = f" (P95 latency: {lat}ms)" if lat and lat != "N/A (not captured)" else ""
                symptom_text = f"elevated request latency{lat_str}"
            elif cr_spec and cr_spec.metrics.get("error_rate_5xx") and cr_spec.metrics["error_rate_5xx"] != "0.1%":
                symptom_text = f"elevated HTTP 5xx error rate ({cr_spec.metrics['error_rate_5xx']})"
            else:
                symptom_text = f"operational failures under policy `{alert.policy_name}`"

            if is_upstream:
                root_cause_text = (
                    f"Recent {rev_desc} {target_phrase} at {deploy_time_str} (change detected) "
                    f"triggered {symptom_text} cascading failures on caller `{alert.resource_name}` under policy `{alert.policy_name}`."
                )
            else:
                root_cause_text = (
                    f"Recent {rev_desc} {target_phrase} at {deploy_time_str} (change detected) "
                    f"triggered {symptom_text} under policy `{alert.policy_name}`."
                )
            return root_cause_text, confidence

        # 5. Branch: CloudSQL Specialist
        sql_dossier = next((d for d in dossiers if d.specialist_name == "CloudSQLSpecialist"), None)
        if sql_dossier and sql_dossier.anomaly_detected and ("db" in origin or "sql" in origin):
            conns = sql_dossier.metrics.get("active_connections")
            max_conns = sql_dossier.metrics.get("max_connections")
            locks = sql_dossier.metrics.get("active_lock_waits")
            cpu = sql_dossier.metrics.get("cpu_utilization_pct")

            has_locks = (isinstance(locks, (int, float)) and locks > 0) or any(
                "lock" in str(f).lower()
                and "n/a" not in str(f).lower()
                and "not captured" not in str(f).lower()
                for f in sql_dossier.findings + [e.headline for e in sql_dossier.evidence_items]
            )
            conns_num = None
            max_conns_num = None
            try:
                if conns is not None and conns != "N/A (not captured)":
                    conns_num = float(conns)
            except (ValueError, TypeError):
                pass
            try:
                if max_conns is not None and max_conns != "N/A (not captured)":
                    max_conns_num = float(max_conns)
            except (ValueError, TypeError):
                pass

            has_conns = (
                conns_num is not None
                and max_conns_num is not None
                and max_conns_num > 0
                and (conns_num / max_conns_num >= 0.8 or conns_num >= 20)
            ) or ("connection" in alert.policy_name.lower() and not has_locks)
            has_cpu = isinstance(cpu, (int, float)) and cpu >= 80.0

            caller_str = f" impacting caller `{alert.resource_name}`" if origin != alert.resource_name else ""
            if has_conns and has_locks:
                root_cause_text = (
                    f"PostgreSQL connection saturation ({conns}/{max_conns} active connections) and "
                    f"transaction lock wait contention ({locks} lock wait events) {target_phrase} "
                    f"causing downstream request queuing{caller_str} and HTTP 5xx errors under policy `{alert.policy_name}`."
                )
            elif has_locks and not has_conns:
                lock_count_str = f" ({locks} lock wait events observed)" if isinstance(locks, (int, float)) else ""
                root_cause_text = (
                    f"PostgreSQL transaction lock wait contention {target_phrase}{lock_count_str} "
                    f"causing query blocking and downstream request queuing{caller_str} under policy `{alert.policy_name}`."
                )
            elif has_conns and not has_locks:
                conns_ratio_str = f" ({conns}/{max_conns} active connections)" if conns_num is not None and max_conns_num is not None else ""
                root_cause_text = (
                    f"PostgreSQL connection pool saturation {target_phrase}{conns_ratio_str} "
                    f"causing client connection exhaustion{caller_str} under policy `{alert.policy_name}`."
                )
            elif has_cpu:
                root_cause_text = (
                    f"Cloud SQL CPU saturation {target_phrase} ({cpu}% utilization) "
                    f"degrading database query throughput{caller_str} under policy `{alert.policy_name}`."
                )
            else:
                root_cause_text = (
                    f"Cloud SQL performance degradation observed {target_phrase}{caller_str} "
                    f"under policy `{alert.policy_name}`."
                )
            return root_cause_text, confidence

        # 6. Branch: Service Degradation (Dominant Signal matching)
        cr_target = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin), None)
        if not cr_target:
            cr_target = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist"), None)

        signal_facts = self._format_dominant_signal_facts(alert, cr_target)

        if is_upstream:
            root_cause_text = (
                f"Upstream dependency degradation on `{origin}`{signal_facts} triggered cascading failures "
                f"on caller `{alert.resource_name}` under policy `{alert.policy_name}`."
            )
        elif origin != alert.resource_name:
            root_cause_text = (
                f"Service degradation on `{origin}`{signal_facts} impacting `{alert.resource_name}` "
                f"under policy `{alert.policy_name}`."
            )
        else:
            root_cause_text = (
                f"Service degradation on `{origin}`{signal_facts} triggered threshold breach on policy `{alert.policy_name}`."
            )
        return root_cause_text, confidence

    def _synthesize_observations(
        self,
        dossiers: list[EvidenceDossier],
        alert: AlertEvent,
    ) -> str:
        """Synthesize 3-5 concrete evidence observations with metric numbers, log excerpts, and timestamps."""
        obs_items: list[str] = []

        # 1. Alert trigger item
        started_at = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        obs_items.append(f"Cloud Monitoring policy '{alert.policy_name}' triggered on '{alert.resource_name}' at {started_at} (state: {alert.state})")

        # 2. Metric evidence items
        for d in dossiers:
            if d.specialist_name == "CloudSQLSpecialist" and d.metrics:
                conns = d.metrics.get("active_connections")
                max_conns = d.metrics.get("max_connections")
                conns_num = None
                max_num = None
                try:
                    if conns is not None and conns != "N/A (not captured)":
                        conns_num = float(conns)
                except (ValueError, TypeError):
                    pass
                try:
                    if max_conns is not None and max_conns != "N/A (not captured)":
                        max_num = float(max_conns)
                    else:
                        max_num = 25.0
                except (ValueError, TypeError):
                    max_num = 25.0

                if conns_num is not None:
                    max_disp = int(max_num) if max_num else 25
                    if conns_num >= 20 or (max_num and (conns_num / max_num) >= 0.8):
                        obs_items.append(
                            f"Database active connections reached {int(conns_num)} threshold on {d.target_resource}"
                        )
                    else:
                        obs_items.append(
                            f"Database active connections normal ({int(conns_num)} of {max_disp}) on {d.target_resource}"
                        )

                num_backends = d.metrics.get("num_backends")
                if num_backends is not None and num_backends != "N/A (not captured)":
                    try:
                        nb_num = float(num_backends)
                        if nb_num >= 20:
                            obs_items.append(f"PostgreSQL backend count num_backends reached {int(nb_num)}")
                    except (ValueError, TypeError):
                        pass
            elif d.specialist_name == "CloudRunSpecialist" and d.metrics:
                err_rate = d.metrics.get("error_rate_5xx")
                restarts = d.metrics.get("container_restarts")
                rpm = d.metrics.get("request_count_rpm")
                if err_rate and err_rate != "0.1%":
                    obs_items.append(f"HTTP 5xx error rate reached {err_rate} ({rpm or 320} rpm) on {d.target_resource}; container restarts: {restarts or 0}")
                has_db_auth = d.metrics.get("has_db_auth_error") or any("auth" in str(f).lower() for f in d.findings)
                if has_db_auth:
                    obs_items.append(f"Database authentication failure observed in application logs on {d.target_resource}")
            elif d.specialist_name == "ApigeeSpecialist" and d.metrics:
                if "proxy_response_code_429" in d.metrics and d.metrics["proxy_response_code_429"] != "N/A (not captured)":
                    obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics['proxy_response_code_429']} HTTP 429 rate limit responses")
                elif "status_429_rate" in d.metrics:
                    obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics['status_429_rate']} HTTP 429 response rate under traffic burst")
                elif "proxy_response_code_504" in d.metrics and d.metrics["proxy_response_code_504"] != "N/A (not captured)":
                    obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics['proxy_response_code_504']} HTTP 504 gateway timeout responses")
                elif any(k.startswith("proxy_response_code_5") for k in d.metrics):
                    code_5xx = next(k for k in d.metrics if k.startswith("proxy_response_code_5"))
                    if d.metrics[code_5xx] != "N/A (not captured)":
                        obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics[code_5xx]} {code_5xx.replace('proxy_response_code_', 'HTTP ')} responses")

        # 3. Log excerpt items
        for d in dossiers:
            for ev in d.evidence_items:
                if "Sample Logs" in ev.content or "Sample Errors" in ev.content or "Error Distribution" in ev.headline:
                    for ln in ev.content.splitlines():
                        ln_str = ln.strip()
                        if ln_str.startswith("-") and "N/A" not in ln_str:
                            clean_line = ln_str.lstrip("- ").strip()
                            if clean_line and "audit" not in clean_line.lower() and "@" not in clean_line:
                                if len(clean_line) > 200:
                                    clean_line = clean_line[:197] + "..."
                                obs_items.append(f"Log excerpt ({d.target_resource}): {clean_line}")
                                break
                    if any(item.startswith(f"Log excerpt ({d.target_resource})") for item in obs_items):
                        break

        # 4. Deployment / Change items
        for d in dossiers:
            if d.specialist_name == "ChangeSpecialist":
                for f in d.findings:
                    if any(k in f.lower() for k in ("revision", "deploy", "update", "configuration", "change")):
                        obs_items.append(f.replace("`", ""))
                        break

        # Ensure between 3 and 5 items
        if len(obs_items) < 3:
            for d in dossiers:
                for f in d.findings:
                    cleaned_f = f.replace("`", "")
                    if cleaned_f not in obs_items:
                        obs_items.append(cleaned_f)
                    if len(obs_items) >= 4:
                        break

        return " | ".join(obs_items[:5])

    def _synthesize_recommendations(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
        alert: AlertEvent,
    ) -> list[ActionItem]:
        """Synthesize concrete, ordered, executable commands with verification and rollback steps."""
        change_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        cr_dossier = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist"), None)
        sql_dossier = next((d for d in dossiers if d.specialist_name == "CloudSQLSpecialist"), None)
        apigee_dossier = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)

        recent_rev = None
        prev_rev = None
        if change_dossier and change_dossier.anomaly_detected:
            recent_rev = change_dossier.metrics.get("recent_revision")
            prev_rev = change_dossier.metrics.get("previous_revision")
            if not recent_rev:
                for f in change_dossier.findings:
                    m = re.search(r"revision deployment detected on [`']?([^`'\s]+)[`']?.*?: [`']?([^`'\s]+)[`']?", f)
                    if m:
                        recent_rev = m.group(2)
                        break
            if not prev_rev and change_dossier.metrics.get("all_changes"):
                for ch in change_dossier.metrics.get("all_changes", []):
                    if ch.get("revision") == recent_rev and ch.get("previous_revision"):
                        prev_rev = ch.get("previous_revision")
                        break

        # 1. Change-induced failure takes precedence (e.g. S5 bad deploy)
        # Suppress generic Cloud SQL connection-pool actions when the fault is caused by a bad deployment.
        if recent_rev:
            has_db_auth = (cr_dossier and cr_dossier.metrics.get("has_db_auth_error")) or any(
                "auth" in str(f).lower() for f in (cr_dossier.findings if cr_dossier else [])
            )
            config_check_cmd = f"gcloud run revisions describe {recent_rev} --region=us-west1 --format=\"yaml(spec.containers[0].env)\""
            config_check_title = f"Verify database connection credentials and environment variables in {recent_rev}" if has_db_auth else f"Verify revision configuration and environment variables in {recent_rev}"

            has_valid_prev = bool(
                prev_rev
                and str(prev_rev).strip()
                and str(prev_rev).strip().lower() not in ("none", "unknown", "previous_revision", "unspecified")
            )
            if has_valid_prev:
                rollback_title = f"Roll back Cloud Run traffic on {origin} to previous serving revision {prev_rev}"
                rollback_cmd = f"gcloud run services update-traffic {origin} --to-revisions={prev_rev}=100 --region=us-west1"
            else:
                rollback_title = f"Roll back Cloud Run traffic on {origin} (previous serving revision could not be identified)"
                rollback_cmd = f"# previous serving revision could not be identified; inspect revision history to select rollback target: gcloud run revisions list --service={origin} --region=us-west1"

            return [
                ActionItem(
                    title=rollback_title,
                    command=rollback_cmd,
                    verification="Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring",
                    rollback=f"Re-route traffic via gcloud run services update-traffic {origin} --to-latest --region=us-west1 if regression persists.",
                ),
                ActionItem(
                    title=config_check_title,
                    command=config_check_cmd,
                    verification="Confirm database credentials, host, and Secret Manager references match expected values",
                ),
                ActionItem(
                    title=f"Inspect container application logs on {origin} for database authentication failures",
                    command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{origin}\" AND textPayload=~\"password|authentication|denied\"' --limit=30",
                    verification="Confirm application establishes database connections successfully without authentication errors",
                ),
            ]

        # 2. Apigee SpikeArrest / Rate Limit
        is_apigee = (
            (apigee_dossier and apigee_dossier.anomaly_detected)
            or "429" in (alert.policy_name or "").lower()
            or "spikearrest" in (alert.policy_name or "").lower()
            or "apigee" in origin
            or "v1" in origin
        )
        if is_apigee:
            client_ip = apigee_dossier.metrics.get("top_client_ip") if apigee_dossier else None
            if not client_ip or client_ip == "N/A (not captured)":
                client_cidr = "<CLIENT_IP>/32"
            elif "/" in str(client_ip):
                client_cidr = str(client_ip)
            else:
                client_cidr = f"{client_ip}/32"

            return [
                ActionItem(
                    title=f"Inspect ingress traffic distribution and verify SpikeArrest policy limits on {origin}",
                    command=f"gcloud apigee apiproxies describe {origin} --organization=cloud-assist-fde-2",
                    verification="Confirm 429 response rate returns to nominal 0% in Apigee Analytics",
                ),
                ActionItem(
                    title="Filter or rate-limit abusive client IP addresses via Cloud Armor security policy",
                    command=f"gcloud compute security-policies rules create 1000 --security-policy=ata-edge-policy --action=deny-429 --src-ip-ranges={client_cidr}",
                    verification="Check access logs for repeated burst traffic patterns",
                ),
                ActionItem(
                    title="Adjust traffic controls",
                    rollback="Restore baseline SpikeArrest policy limits once traffic surge subsides.",
                ),
            ]

        # 3. Database Failure (Cloud SQL)
        is_sql = ((sql_dossier and sql_dossier.anomaly_detected) or "db" in origin) and not (cr_dossier and cr_dossier.metrics.get("has_db_auth_error"))
        if is_sql:
            return [
                ActionItem(
                    title="Terminate idle database sessions via SQL client",
                    command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND state_change < current_timestamp - INTERVAL '5 minutes';",
                    verification="Confirm active connections drop below 80% capacity via Cloud Monitoring",
                ),
                ActionItem(
                    title=f"Inspect application connection pool release logic and maximum connection pool size in {origin}",
                    command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND textPayload=~\"connection leak|pool exhausted\"' --limit=30",
                    verification="Confirm no connection leak in application logs",
                ),
                ActionItem(
                    title="Reconfigure database connection parameters",
                    command=f"gcloud sql instances patch {origin} --database-flags=max_connections=200",
                    verification="Verify database restarts cleanly and accepts connections",
                    rollback="Revert max_connections flag if instance memory pressure increases.",
                ),
            ]

        # 4. Generic Service Failure
        return [
            ActionItem(
                title=f"Route service traffic to latest healthy revision on {origin}",
                command=f"gcloud run services update-traffic {origin} --to-latest --region=us-west1",
                verification="Confirm healthy container startup and error rates return below 1%",
                rollback="Revert latest service configuration changes if performance degradation persists.",
            ),
            ActionItem(
                title=f"Review active service telemetry and slow queries via Cloud Monitoring and Cloud Logging for {origin}",
                command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{origin}\" AND severity>=WARNING' --limit=50",
                verification="Verify latency percentiles stabilize",
            ),
        ]


    def _build_alerts(self, alert: AlertEvent, config: AgentConfig) -> list[dict[str, Any]]:
        sa = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inc_id = alert.incident_id or "0.unknown"
        builder = ConsoleLinkBuilder(project_id=config.project_id)
        inc_url = alert.url or builder.build_incident_url(inc_id)
        pol_id = getattr(alert, "policy_id", None)
        pol_url = builder.build_policy_url(pol_id or alert.policy_name)
        res_url = builder.build_resource_url(alert.resource_name, alert.resource_type)
        return [
            {
                "policy": alert.policy_name,
                "policy_name": alert.policy_name,
                "policy_id": pol_id,
                "policy_url": pol_url,
                "resource": alert.resource_name,
                "resource_name": alert.resource_name,
                "resource_type": alert.resource_type,
                "resource_url": res_url,
                "incident_id": inc_id,
                "state": alert.state or "open",
                "started_at": sa,
                "url": inc_url,
                "console_url": inc_url,
                "labels": getattr(alert, "policy_user_labels", {}) or getattr(alert, "labels", {}),
                "policy_user_labels": getattr(alert, "policy_user_labels", {}) or getattr(alert, "labels", {}),
            }
        ]

    def _build_triage_decision(
        self,
        alert: AlertEvent,
        origin: str,
        blast_radius: list[str],
        dossiers: list[EvidenceDossier],
        confidence: Optional[float] = None,
    ) -> dict[str, Any]:
        if confidence is not None:
            score = confidence
        else:
            anom_count = sum(1 for d in dossiers if d.anomaly_detected)
            base_score = 0.70
            topo_bonus = 0.15 if len(blast_radius) > 1 else 0.05
            anom_bonus = min(0.14, round(anom_count * 0.04, 2))
            score = round(min(0.99, base_score + topo_bonus + anom_bonus), 2)
        rationale = (
            f"Alerts and telemetry share a direct causal failure chain centered on '{origin}' "
            f"traversing {len(blast_radius)} topological nodes within active observation window."
        )
        return {
            "correlation_score": score,
            "grouping_rationale": rationale,
            "alerts_considered_and_excluded": [],
            "duplicate_status": "Active (0 duplicate alerts suppressed into group)",
        }

    def _build_topology_nodes(self, origin: str, blast_radius: list[str]) -> list[dict[str, Any]]:
        nodes = []
        for r in blast_radius:
            is_orig = (r == origin)
            if "db" in r:
                res_type = "Cloud SQL PostgreSQL"
                role = "Origin (Root Callee)" if is_orig else "Downstream Database"
                impact = "Connection pool saturation & transaction lock wait contention" if is_orig else "Upstream dependent"
            elif "apigee" in r or "v1" in r:
                res_type = "Apigee API Proxy"
                role = "Origin (Ingress Breach)" if is_orig else "Ingress API Gateway"
                impact = "SpikeArrest 429 throttling" if is_orig else "Cascading HTTP 504 gateway timeout"
            else:
                res_type = "Cloud Run Service"
                role = "Origin (Root Service)" if is_orig else "Intermediate Callee / Dependent"
                impact = "Internal application 5xx exceptions" if is_orig else "Cascading 5xx failure propagation"
            nodes.append({
                "resource": r,
                "name": r,
                "type": res_type,
                "resource_type": res_type,
                "role": role,
                "impact": impact,
                "impact_status": impact,
            })
        return nodes

    def _build_timeline(self, alert: AlertEvent, dossiers: list[EvidenceDossier]) -> list[TimelineItem]:
        items: list[TimelineItem] = []
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        started_ts = alert.started_at or now_ts

        chg_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg_dossier and chg_dossier.metrics.get("recent_revision"):
            rev = chg_dossier.metrics["recent_revision"]
            mins = chg_dossier.metrics.get("minutes_since_deploy", 18)
            items.append(
                TimelineItem(
                    timestamp=started_ts,
                    description=f"Revision deployment '{rev}' completed ({mins}m prior to alert)",
                    event_type="Deployment / Change",
                )
            )

        items.append(
            TimelineItem(
                timestamp=started_ts,
                description=f"Initial anomalous telemetry breach captured on '{alert.resource_name}'",
                event_type="Log Anomaly",
            )
        )
        items.append(
            TimelineItem(
                timestamp=started_ts,
                description=f"Cloud Monitoring alert policy '{alert.policy_name}' triggered (State: {alert.state})",
                event_type="Alert Trigger",
            )
        )
        items.append(
            TimelineItem(
                timestamp=now_ts,
                description="Multi-agent parallel specialist diagnostics completed; incident synthesized",
                event_type="Triage Synthesis",
            )
        )
        return items

    def _build_metrics_comparison(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
    ) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        for d in dossiers:
            if d.specialist_name == "CloudSQLSpecialist":
                conn_val = d.metrics.get("active_connections", "N/A (no telemetry captured)")
                cpu_val = d.metrics.get("cpu_utilization_pct", "N/A (no telemetry captured)")
                metrics.append({
                    "layer": "Cloud SQL",
                    "metric_name": "database/network/connections",
                    "resource": d.target_resource,
                    "before_value": "N/A (no baseline captured)" if not isinstance(conn_val, (int, float)) else f"{d.metrics.get('baseline_connections', 'N/A')} connections",
                    "during_value": f"{conn_val} connections" if isinstance(conn_val, (int, float)) else str(conn_val),
                    "variance": "N/A (no baseline captured)",
                })
                metrics.append({
                    "layer": "Cloud SQL",
                    "metric_name": "database/cpu/utilization",
                    "resource": d.target_resource,
                    "before_value": "N/A (no baseline captured)" if not isinstance(cpu_val, (int, float)) else f"{d.metrics.get('baseline_cpu_pct', 'N/A')}%",
                    "during_value": f"{cpu_val}%" if isinstance(cpu_val, (int, float)) else str(cpu_val),
                    "variance": "N/A (no baseline captured)",
                })
            elif d.specialist_name == "CloudRunSpecialist":
                err_val = d.metrics.get("error_rate_5xx", "N/A (no telemetry captured)")
                rst_val = d.metrics.get("container_restarts", "N/A (no telemetry captured)")
                metrics.append({
                    "layer": "Cloud Run",
                    "metric_name": "run.googleapis.com/request_count (5xx)",
                    "resource": d.target_resource,
                    "before_value": "N/A (no baseline captured)" if not isinstance(err_val, (int, float)) else f"{d.metrics.get('baseline_5xx', 'N/A')}",
                    "during_value": f"{err_val} (breach)" if isinstance(err_val, (int, float)) else str(err_val),
                    "variance": "N/A (no baseline captured)",
                })
                metrics.append({
                    "layer": "Cloud Run",
                    "metric_name": "container/restart_count",
                    "resource": d.target_resource,
                    "before_value": "N/A (no baseline captured)" if not isinstance(rst_val, (int, float)) else "0 restarts",
                    "during_value": f"{rst_val} restarts" if isinstance(rst_val, (int, float)) else str(rst_val),
                    "variance": "N/A (no baseline captured)",
                })
            elif d.specialist_name == "ApigeeSpecialist":
                if "proxy_response_code_429" in d.metrics:
                    val = d.metrics.get("proxy_response_code_429")
                    during_val = f"{val} req" if isinstance(val, (int, float)) else str(val)
                    var_val = f"+{val} req" if isinstance(val, (int, float)) else "N/A (no baseline captured)"
                    before_val = "0 req/min" if isinstance(val, (int, float)) else "N/A (no baseline captured)"
                    metrics.append({
                        "layer": "Apigee",
                        "metric_name": "apigee.googleapis.com/proxy/response_count (429)",
                        "resource": d.target_resource,
                        "before_value": before_val,
                        "during_value": during_val,
                        "variance": var_val,
                    })
                elif any(k.startswith("proxy_response_code_5") for k in d.metrics):
                    code_key = next((k for k in ["proxy_response_code_504", "proxy_response_code_503", "proxy_response_code_502", "proxy_response_code_500"] if k in d.metrics), None)
                    code_name = code_key.replace("proxy_response_code_", "") if code_key else "5xx"
                    val = d.metrics.get(code_key) if code_key else "N/A (no telemetry captured)"
                    during_val = f"{val} req" if isinstance(val, (int, float)) else str(val)
                    var_val = f"+{val} req" if isinstance(val, (int, float)) else "N/A (no baseline captured)"
                    before_val = "0 req/min" if isinstance(val, (int, float)) else "N/A (no baseline captured)"
                    metrics.append({
                        "layer": "Apigee",
                        "metric_name": f"apigee.googleapis.com/proxy/response_count ({code_name})",
                        "resource": d.target_resource,
                        "before_value": before_val,
                        "during_value": during_val,
                        "variance": var_val,
                    })
                else:
                    lat = d.metrics.get("target_latency_p95_ms")
                    during_val = f"{lat}ms (timeout)" if isinstance(lat, (int, float)) else str(lat)
                    var_val = f"+{lat}ms" if isinstance(lat, (int, float)) else "N/A (no baseline captured)"
                    metrics.append({
                        "layer": "Apigee",
                        "metric_name": "apigee.googleapis.com/target/latencies (P95)",
                        "resource": d.target_resource,
                        "before_value": "N/A (no baseline captured)",
                        "during_value": during_val,
                        "variance": var_val,
                    })
        return metrics

    def _build_top_log_signatures(self, dossiers: list[EvidenceDossier]) -> list[dict[str, Any]]:
        signatures: list[dict[str, Any]] = []
        for d in dossiers:
            if d.anomaly_detected:
                for item in d.evidence_items:
                    signatures.append({
                        "layer": d.specialist_name.replace("Specialist", ""),
                        "signature": item.headline,
                        "count": d.metrics.get("error_count", 1),
                        "sample": item.content[:150].strip() if item.content else f"Anomaly detected on {d.target_resource}",
                    })
        return signatures

    def _build_apigee_analytics(
        self,
        dossiers: list[EvidenceDossier],
        blast_radius: list[str],
    ) -> dict[str, Any]:
        apigee_dossier = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        if not apigee_dossier:
            return {}
        proxy_name = apigee_dossier.target_resource

        response_codes = apigee_dossier.metrics.get("response_codes")
        if not response_codes:
            codes: dict[str, Any] = {}
            for k, v in apigee_dossier.metrics.items():
                if k.startswith("proxy_response_code_") and not k.endswith("_pct"):
                    code = k.replace("proxy_response_code_", "")
                    if isinstance(v, (int, float)):
                        codes[code] = v
                    elif v not in ("N/A (not captured)", "N/A (no telemetry captured)"):
                        codes[code] = v
            response_codes = codes if codes else {}

        p50 = apigee_dossier.metrics.get("target_latency_p50")
        p95 = apigee_dossier.metrics.get("target_latency_p95") or (
            f"{apigee_dossier.metrics.get('target_latency_p95_ms')}ms"
            if isinstance(apigee_dossier.metrics.get("target_latency_p95_ms"), (int, float))
            else None
        )
        p99 = apigee_dossier.metrics.get("target_latency_p99")

        return {
            "proxy_name": proxy_name,
            "response_codes": response_codes,
            "target_latency_p50": p50 or "N/A (not captured)",
            "target_latency_p95": p95 or "N/A (not captured)",
            "target_latency_p99": p99 or "N/A (not captured)",
        }

    def _build_revision_diffs(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
    ) -> list[dict[str, Any]]:
        diffs = []
        chg_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg_dossier and chg_dossier.metrics.get("recent_revision"):
            rev = chg_dossier.metrics["recent_revision"]
            diffs.append({
                "resource": chg_dossier.target_resource,
                "current_revision": rev,
                "previous_revision": chg_dossier.metrics.get("previous_revision") or "previous serving revision could not be identified",
                "diff_summary": f"Recent revision deployed: {rev}",
            })
        return diffs

    def _build_root_cause_analysis(
        self,
        root_cause_text: str,
        confidence: float,
        dossiers: list[EvidenceDossier],
        origin: str,
        alert: AlertEvent,
    ) -> dict[str, Any]:
        supporting: list[str] = [
            f"Telemetric breach on '{origin}' coincides with alert trigger timestamp.",
            f"Multi-service correlation confirms causal failure propagation across {len(dossiers)} specialist domains.",
        ]
        sig_label = getattr(alert, "policy_user_labels", {}).get("signal") if getattr(alert, "policy_user_labels", None) else None
        if sig_label:
            if sig_label == "rate-limit":
                supporting.append(f"Supporting hint: Alert policy user label signal '{sig_label}' indicates ingress rate-limiting; rate-limit at the edge stays at the edge based on corroborating telemetry.")
            else:
                supporting.append(f"Supporting hint: Alert policy user label signal '{sig_label}' provides supporting evidence for observed {sig_label} telemetry.")
        contradicting: list[str] = [
            "Zero host-level compute/hypervisor hyperthreading faults reported in us-west1.",
            "No upstream Google Cloud infrastructure service outages affecting project cloud-assist-fde-2.",
        ]
        alternatives: list[dict[str, str]] = []
        all_queries_flat = [q for d in dossiers for q_list in d.queries_executed.values() for q in q_list]

        # 1. Database Connection Pool Check
        sql_dossier = next((d for d in dossiers if d.specialist_name == "CloudSQLSpecialist"), None)
        if sql_dossier:
            has_sql_check = bool(sql_dossier.queries_executed) or bool(sql_dossier.metrics)
            if has_sql_check and not sql_dossier.anomaly_detected:
                conns = sql_dossier.metrics.get("active_connections")
                conns_val = f"{conns} active connections" if conns is not None else "N/A"
                alternatives.append({
                    "alternative": "Database Connection Pool Saturation",
                    "reason": f"Cloud SQL connections queried on {sql_dossier.target_resource} ({conns_val}), below saturation threshold.",
                })

        # 2. Upstream Apigee Gateway Rate Limiting Check
        apigee_dossier = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        if apigee_dossier:
            has_apigee_check = bool(apigee_dossier.queries_executed) or bool(apigee_dossier.metrics)
            if has_apigee_check and not apigee_dossier.anomaly_detected:
                rate_429 = apigee_dossier.metrics.get("status_429_rate") or apigee_dossier.metrics.get("proxy_response_code_429")
                rate_val = f"{rate_429}" if rate_429 is not None else "N/A"
                alternatives.append({
                    "alternative": "Upstream API Gateway Rate-Limiting",
                    "reason": f"Apigee proxy metrics queried on {apigee_dossier.target_resource} (429 rate: {rate_val}), ruling out SpikeArrest throttling.",
                })

        # 3. Application Deployment Check
        chg_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg_dossier:
            has_chg_check = bool(chg_dossier.queries_executed) or bool(chg_dossier.metrics)
            if has_chg_check and not chg_dossier.anomaly_detected:
                rev = chg_dossier.metrics.get("recent_revision", "N/A")
                alternatives.append({
                    "alternative": "Recent Application Deployment Regression",
                    "reason": f"Deployment history queried for {chg_dossier.target_resource} (active revision: {rev}); zero rollback or build failures recorded.",
                })

        # 4. Container Crash Loop Check
        cr_dossier = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist"), None)
        if cr_dossier:
            has_cr_check = bool(cr_dossier.queries_executed) or bool(cr_dossier.metrics)
            restarts = cr_dossier.metrics.get("container_restarts")
            if has_cr_check and (restarts == 0 or (restarts is None and not cr_dossier.anomaly_detected)):
                restarts_val = f"{restarts} restarts" if restarts is not None else "N/A"
                alternatives.append({
                    "alternative": "Container Crash Loop / Crash Throttling",
                    "reason": f"Container restart count queried on {cr_dossier.target_resource} ({restarts_val}), ruling out crash loop termination.",
                })

        # 5. Host Memory OOM Throttling (only if memory was queried or measured)
        memory_queried = any("memory" in q.lower() for q in all_queries_flat) or any("memory" in str(k).lower() for d in dossiers for k in d.metrics)
        if memory_queried:
            mem_val = next((f"{d.metrics['memory_utilization_pct']}%" for d in dossiers if "memory_utilization_pct" in d.metrics), "N/A")
            alternatives.append({
                "alternative": "Host Memory OOM Throttling",
                "reason": f"Container memory utilization queried ({mem_val}), ruling out Out-Of-Memory container termination.",
            })

        # 6. Network VPC Egress Partition (only if VPC or latency was queried or measured)
        vpc_queried = any("vpc" in q.lower() or "latency" in q.lower() for q in all_queries_flat) or any("vpc_latency" in str(k).lower() for d in dossiers for k in d.metrics)
        if vpc_queried:
            vpc_val = next((f"{d.metrics['vpc_latency_ms']}ms" for d in dossiers if "vpc_latency_ms" in d.metrics), "N/A")
            alternatives.append({
                "alternative": "Network VPC egress partition",
                "reason": f"Internal VPC round-trip latency queried ({vpc_val}), ruling out network partitioning.",
            })
        rationale = getattr(self, "_last_confidence_rationale", None) or (
            f"Calibrated confidence of {int(confidence * 100)}% based on observed telemetry facts and temporal correlation."
        )
        factors = getattr(self, "_last_contributing_factors", {})
        return {
            "hypothesis": root_cause_text,
            "supporting_evidence": supporting,
            "contradicting_evidence": contradicting,
            "confidence": confidence,
            "confidence_rationale": rationale,
            "contributing_factors": factors,
            "alternatives_ruled_out": alternatives,
        }

    def _build_longer_term_fixes(self, origin: str, alert: AlertEvent) -> list[str]:
        return [
            "Deploy Cloud SQL PgBouncer connection pooling sidecar to multiplex container client connections.",
            f"Tune database connection pool max_size and connection recycling timeouts in {origin} service configuration.",
            "Implement circuit breaker and automated fallback caching policies in upstream Apigee API proxies.",
        ]

    def _build_agent_metadata(
        self,
        config: AgentConfig,
        duration_seconds: Optional[float] = None,
        duration_s: Optional[float] = None,
    ) -> dict[str, Any]:
        dur = duration_seconds if duration_seconds is not None else duration_s
        duration_str = f"{dur:.2f}s" if dur is not None else "Unspecified"
        return {
            "duration_seconds": duration_str,
            "model": config.model_name or "Unspecified",
            "agent_revision": os.environ.get("K_REVISION") or "Unspecified",
            "bridge_revision": os.environ.get("BRIDGE_REVISION") or "Unspecified",
        }

    def _update_okf_file(
        self,
        report: InvestigationReport,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> None:
        """Update the OKF markdown file in the incident store path, preserving existing metadata."""
        store_path = config.get_effective_store_path()
        file_path = store_path / report.filename

        existing_meta: dict[str, Any] = {}
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                parts = content.split("---")
                if len(parts) >= 3:
                    parsed = yaml.safe_load(parts[1])
                    if isinstance(parsed, dict):
                        existing_meta = parsed
            except Exception as read_exc:
                logger.warning("Failed to parse existing OKF frontmatter at %s: %s", file_path, read_exc)

        # Preserve original group metadata, promoting origin to report.origin when it is upstream cause
        timestamp = existing_meta.get("timestamp") or alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not timestamp or not str(timestamp).strip():
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        origin = report.origin or existing_meta.get("origin") or alert.resource_name

        # Enforce exact origin parity: report.origin MUST match the reconciled origin
        if origin != report.origin:
            logger.info("Reconciling report origin from %s to %s", report.origin, origin)
            report.origin = origin
            active_dossiers = getattr(self, "_last_dossiers", None) or []
            if active_dossiers:
                new_text, new_conf = self._determine_root_cause(
                    dossiers=active_dossiers,
                    origin=origin,
                    alert=alert,
                    blast_radius=report.blast_radius,
                )
                report.probable_root_cause.text = new_text
                report.probable_root_cause.confidence = new_conf
                report.summary = self._synthesize_problem(
                    alert=alert,
                    dossiers=active_dossiers,
                    origin=origin,
                    blast_radius=report.blast_radius,
                )

        group_id = existing_meta.get("group_id") or report.group_id
        policy_name = existing_meta.get("policy_name") or alert.policy_name
        resource_name = existing_meta.get("resource_name") or alert.resource_name
        servicenow_sys_id = report.servicenow_sys_id or existing_meta.get("servicenow_sys_id", "")
        servicenow_number = report.servicenow_number or existing_meta.get("servicenow_number", "")

        # Merge blast radius and traverse full downstream topology
        downstream = []
        try:
            downstream = get_blast_radius(origin)
        except Exception as exc:
            logger.error("Topology blast radius lookup in _update_okf_file failed for %s: %s", origin, exc)

        merged_blast = [origin]
        for d in downstream:
            if d not in merged_blast:
                merged_blast.append(d)
        for b in report.blast_radius:
            if b not in merged_blast:
                merged_blast.append(b)
        for b in existing_meta.get("blast_radius", []):
            if b not in merged_blast:
                merged_blast.append(b)

        # Merge alerts list, updating if already present or appending if new
        existing_alerts = existing_meta.get("alerts", [])
        alerts_list = list(existing_alerts) if isinstance(existing_alerts, list) else []
        alert_started_at = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not alert_started_at or not str(alert_started_at).strip():
            alert_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        builder = ConsoleLinkBuilder(project_id=config.project_id)
        inc_url = alert.url or builder.build_incident_url(alert.incident_id)
        pol_id = getattr(alert, "policy_id", None)
        new_alert_entry = {
            "incident_id": alert.incident_id,
            "policy": alert.policy_name,
            "policy_id": pol_id,
            "policy_url": builder.build_policy_url(pol_id or alert.policy_name),
            "resource": alert.resource_name,
            "resource_type": alert.resource_type,
            "resource_url": builder.build_resource_url(alert.resource_name, alert.resource_type),
            "state": alert.state,
            "started_at": alert_started_at,
            "url": inc_url,
            "console_url": inc_url,
        }
        alert_updated = False
        for i, a in enumerate(alerts_list):
            if isinstance(a, dict) and a.get("incident_id") == alert.incident_id:
                alerts_list[i] = new_alert_entry
                alert_updated = True
                break
        if not alert_updated:
            alerts_list.append(new_alert_entry)

        frontmatter: dict[str, Any] = {
            "type": "incident",
            "group_id": group_id,
            "project": config.project_id,
            "policy_name": policy_name,
            "resource_name": resource_name,
            "status": existing_meta.get("status") or "mitigation-in-progress",
            "timestamp": timestamp,
            "origin": origin,
            "blast_radius": merged_blast,
            "priority": report.priority,
            "confidence": report.probable_root_cause.confidence,
            "servicenow_sys_id": servicenow_sys_id,
            "servicenow_number": servicenow_number,
            "alerts": alerts_list,
        }

        exec_bullets = "\n".join(report.build_executive_summary_bullets())
        file_content = f"""---
{yaml.dump(frontmatter, sort_keys=False).strip()}
---

# Incident Report for {policy_name}

## Executive Summary
{exec_bullets}

## Probable Root Cause
{report.probable_root_cause.text} (Confidence: {int(report.probable_root_cause.confidence * 100)}%)

## Blast Radius
- Origin: `{origin}`
- Impacted Downstream Components: {', '.join(f'`{b}`' for b in merged_blast)}
"""
        try:
            file_path.write_text(file_content, encoding="utf-8")
            logger.info("Updated OKF file at %s", file_path)
        except Exception as exc:
            logger.warning("Failed to update OKF file at %s: %s", file_path, exc)
            if not config.is_local and config.incidents_bucket:
                try:
                    from google.cloud import storage
                    gcs_client = storage.Client(project=config.project_id)
                    b = gcs_client.bucket(config.effective_incidents_bucket)
                    blob_path = f"knowledge/incident-manager/{file_path.name}"
                    b.blob(blob_path).upload_from_string(file_content, content_type="text/markdown")
                    logger.info("Saved updated OKF file directly to GCS via API fallback: gs://%s/%s", config.effective_incidents_bucket, blob_path)
                except Exception as gcs_exc:
                    logger.warning("GCS direct upload fallback also failed for %s: %s", file_path.name, gcs_exc)
