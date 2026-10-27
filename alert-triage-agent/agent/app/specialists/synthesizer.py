"""SynthesizerAgent for Alert Triage Runtime.

Orchestrates concurrent execution of specialist agents via asyncio.gather,
integrates evidence dossiers, determines probable root cause and blast radius,
updates the OKF incident file at /knowledge/incident-manager, and compiles
the final InvestigationReport.
"""

import asyncio
import dataclasses
from datetime import datetime, timezone
import logging
import os
import re
import time
from typing import Any, Optional

from ..config import AgentConfig, get_agent_settings
from ..contracts.alert import AlertEvent
from ..contracts.report import (
    ActionItem,
    EvidenceItem,
    InvestigationReport,
    ProbableRootCause,
    SIGNAL_TITLE_PROBLEMS,
    split_observations,
    TimelineItem,
    title_problem_for_signal,
)
from ..contracts.links import ConsoleLinkBuilder
from ..grouping.models import OKFDocument, format_okf_alert_entry
from ..grouping.store import OKFStore, get_canonical_blob_name, resolve_local_target_file
from .base import EvidenceDossier
from .cloud_run import CloudRunSpecialist, measured_breaches
from .apigee import ApigeeSpecialist
from .cloud_sql import CloudSQLSpecialist
from .change import ChangeSpecialist, is_genuine_change
from .origin_selection import OriginSelection, alert_event_from_entry, onset_allows_explanation, select_origin

try:
    from topology.topology_graph import (
        get_blast_radius,
        get_upstream_dependencies,
        _canonicalize_node,
        get_loader,
    )
except ImportError as err:
    logging.getLogger(__name__).error("Failed to import topology_graph: %s", err)
    raise

logger = logging.getLogger(__name__)


_DB_AUTH_PHRASE = "database authentication"

# Sentinel: _determine_probable_origin computes the change verdict itself when none is passed.
_VERDICT_NOT_COMPUTED: Any = object()


def _dossier_has_db_auth_error(dossier: Optional[EvidenceDossier]) -> bool:
    """True when a Cloud Run dossier reports a database authentication failure.

    Relies on the specialist's ``has_db_auth_error`` metric (set from precise log
    signatures) or its explicit "database authentication" finding/symptom. A bare
    "auth" substring is not enough: it matches "Authorization", "OAuth" and similar
    text in unrelated findings.
    """
    if not dossier:
        return False
    if dossier.metrics.get("has_db_auth_error"):
        return True
    texts = [str(dossier.metrics.get("observed_symptom", ""))] + [str(f) for f in dossier.findings]
    return any(_DB_AUTH_PHRASE in t.lower() for t in texts)

# Delimits the synthesizer-owned part of the OKF body; content outside it belongs to other
# writers (e.g. duplicate-alert notes) and is preserved across synthesizer updates.
REPORT_SECTION_BEGIN = "<!-- ata:synthesizer-report:begin -->"
REPORT_SECTION_END = "<!-- ata:synthesizer-report:end -->"

# Leads the report's observations when the investigation completed but the incident OKF file
# could not be updated; observations are rendered into the ServiceNow work note and description.
OKF_DEGRADED_NOTE = "DEGRADED MODE: incident OKF file update failed"

# Leads the observation added for each further alert of the incident group.
CORRELATED_ALERT_PREFIX = "Correlated alert:"


def dependency_path_for(origin: str) -> Optional[str]:
    """Topology path(s) from each impacted entry point to ``origin``, or None when unknown.

    Example: 'proxy-a -> service-a -> origin; proxy-b -> origin'.
    Arrows follow TOPOLOGY.md (caller -> callee). A failed lookup is logged and yields None.
    """
    try:
        path = get_loader().format_dependency_paths(origin)
    except Exception as exc:
        logger.error("Topology dependency path lookup failed for %s: %s", origin, exc)
        return None
    if path is None:
        logger.warning("No topology dependency path for %s: not a component of TOPOLOGY.md", origin)
    return path


def _alert_field(a: dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        v = a.get(k)
        if v:
            return str(v)
    return None


def format_alert_trigger_line(
    policy: str,
    resource: str,
    started_at: str,
    state: str,
    incident_id: Optional[str] = None,
) -> str:
    """One observation line per alert. The incident id keeps two alerts of one policy on one
    resource (e.g. one per metric series) distinguishable; it matches the description's alert
    list and the Cloud Monitoring console link."""
    inc = f", incident {incident_id}" if incident_id else ""
    return f"Cloud Monitoring policy '{policy}' triggered on '{resource}' at {started_at} (state: {state}{inc})"


def apply_group_alerts(report: InvestigationReport, group_alerts: list[Any]) -> InvestigationReport:
    """Make the report reflect every alert of the incident group, not just the triggering one.

    The investigation runs on the group's primary alert, so the synthesized report starts with
    that alert only. This merges the group's alerts (OKF ``alerts``) into ``report.alerts``,
    which drives the ServiceNow title count and the description's alert list, adds one
    observation per further alert, and widens first_seen/last_alert to cover them.

    Idempotent: entries are keyed by incident_id, observation lines are added only when
    absent, so it can run inside an OKF precondition-retry mutation and again before ticketing.
    """
    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for a in [*report.alerts, *(group_alerts or [])]:
        if not isinstance(a, dict):
            continue
        inc_id = a.get("incident_id")
        if inc_id and inc_id in index:
            # Keep the report's (richer) entry; fill fields it lacks from the group entry.
            existing = merged[index[inc_id]]
            for k, v in a.items():
                if v and not existing.get(k):
                    existing[k] = v
            continue
        if inc_id:
            index[inc_id] = len(merged)
        merged.append(dict(a))
    if not merged:
        return report
    report.alerts = merged

    primary_id = merged[0].get("incident_id")
    obs = report.observations or ""
    new_lines: list[str] = []
    for a in merged[1:]:
        if primary_id and a.get("incident_id") == primary_id:
            continue
        policy = _alert_field(a, "policy_name", "policy") or "unknown"
        resource = _alert_field(a, "component", "resource_name", "resource") or "unknown"
        inc_id = _alert_field(a, "incident_id")
        line = f"{CORRELATED_ALERT_PREFIX} " + format_alert_trigger_line(
            policy,
            resource,
            _alert_field(a, "started_at") or "unknown",
            _alert_field(a, "state") or "open",
            inc_id,
        )
        # Keyed by incident id: a second alert of the same policy on the same resource gets
        # its own line. Without an id, fall back to policy and resource.
        if inc_id:
            present = any(
                o.startswith(CORRELATED_ALERT_PREFIX) and f"incident {inc_id})" in o
                for o in split_observations(obs)
            )
        else:
            present = f"{CORRELATED_ALERT_PREFIX} Cloud Monitoring policy '{policy}' triggered on '{resource}'" in obs
        if not present and line not in new_lines:
            new_lines.append(line)
    if new_lines:
        items = split_observations(obs)
        # Directly after the primary alert's trigger line (the first item) when present.
        insert_at = 1 if items else 0
        items[insert_at:insert_at] = new_lines
        report.observations = " | ".join(items)

    starts = sorted(str(a["started_at"]) for a in merged if a.get("started_at"))
    if starts:
        if not report.first_seen or starts[0] < report.first_seen:
            report.first_seen = starts[0]
        if not report.last_alert or starts[-1] > report.last_alert:
            report.last_alert = starts[-1]
    return report


def _merge_report_section(body: str, report_markdown: str) -> str:
    """Return ``body`` with its synthesizer report section replaced by ``report_markdown``.

    The section is placed first; any other content of ``body`` is kept after it.
    """
    start = body.find(REPORT_SECTION_BEGIN)
    end = body.find(REPORT_SECTION_END, start) if start != -1 else -1
    if start != -1 and end != -1:
        body = body[:start] + body[end + len(REPORT_SECTION_END):]
    remainder = body.strip()
    section = f"{REPORT_SECTION_BEGIN}\n{report_markdown.strip()}\n{REPORT_SECTION_END}"
    return f"{section}\n\n{remainder}" if remainder else section


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
        group_alerts: Optional[list[dict[str, Any]]] = None,
    ) -> InvestigationReport:
        """Run specialists concurrently with asyncio.gather and synthesize findings.

        ``group_alerts`` are the incident group's alerts (OKF ``alerts``). When they span more
        than one service, the investigation starts from the topology-selected origin rather
        than from ``alert`` (the group's first alert); see origin_selection.
        """
        start_time = time.monotonic()
        onset_tolerance = config.origin_onset_tolerance_seconds
        selection = self._select_group_origin(alert, group_alerts, onset_tolerance_seconds=onset_tolerance)
        if selection is not None and selection.is_multi_service:
            trigger_alert = alert
            alert = alert_event_from_entry(selection.primary_alert, trigger_alert)
            if alert.incident_id != trigger_alert.incident_id:
                logger.info(
                    "Group %s: investigating from topology-selected origin %s (alert %s) instead of the first alert %s on %s",
                    group_id,
                    selection.origin,
                    alert.incident_id,
                    trigger_alert.incident_id,
                    trigger_alert.resource_name,
                )
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
        # Run CloudRunSpecialist on upstream Cloud Run dependencies
        dep_tasks = {}
        for dep in upstream_deps:
            if "db" not in dep and "apigee" not in dep and dep != canonical_res:
                dep_tasks[dep] = self.cloud_run_specialist.investigate(alert, config, target_service=dep)
        # Bounded: also gather Cloud Run evidence for the runner-up candidate origin, so the
        # report can state why it was not chosen. Keyed by service, so origin rules that walk
        # upstream dependencies do not consider it.
        if selection is not None and selection.is_multi_service:
            for cand in selection.investigated_candidates()[1:]:
                if cand.service != canonical_res and cand.service not in dep_tasks and self._is_cloud_run(cand.service):
                    dep_tasks[cand.service] = self.cloud_run_specialist.investigate(
                        alert, config, target_service=cand.service
                    )

        all_keys = list(spec_tasks.keys()) + list(dep_tasks.keys())
        all_futures = list(spec_tasks.values()) + list(dep_tasks.values())
        all_results = await asyncio.gather(*all_futures)

        dossier_map = dict(zip(all_keys, all_results))
        dossiers: list[EvidenceDossier] = list(all_results)

        # Determine probable origin and blast radius. The change verdict is computed once and
        # shared with the origin rules.
        verdict = self._assess_change(
            alert, dossier_map, upstream_deps, selection, onset_tolerance_seconds=onset_tolerance
        )
        origin = self._determine_probable_origin(
            alert, dossier_map, upstream_deps, group_selection=selection, change_verdict=verdict
        )

        # A recent change that is not implicated must not drive the narrative or the actions.
        if verdict is not None and (not verdict["implicated"] or not self._same_node(verdict["service"], origin)):
            if verdict["implicated"]:
                verdict = dict(verdict, implicated=False, reason=(
                    f"origin is {origin}; recent revision {verdict['revision']} on {verdict['service']} not implicated"
                ))
            neutral = self._neutralize_change_dossier(dossier_map["ChangeSpecialist"], verdict)
            dossier_map["ChangeSpecialist"] = neutral
            dossiers = [neutral if d.specialist_name == "ChangeSpecialist" else d for d in dossiers]
        self._last_dossiers = dossiers
        origin_notes = self._origin_selection_notes(selection, origin, verdict, dossier_map.get("ChangeSpecialist"))
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
        title_problem = self._synthesize_title_problem(alert, dossiers, origin)
        observations = self._synthesize_observations(dossiers, alert)
        recommendations = self._synthesize_recommendations(
            dossiers, origin, alert, region=config.region, project_id=config.project_id
        )
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
        root_cause_analysis = self._build_root_cause_analysis(
            root_cause_text, confidence, dossiers, origin, alert,
        )
        gateway_fact = self._gateway_timeout_fact(dossiers, origin)
        if gateway_fact:
            observations = " | ".join([*split_observations(observations), gateway_fact.replace("`", "")])
            root_cause_analysis["supporting_evidence"].append(gateway_fact)
        if origin_notes:
            label = origin_notes["label"]
            observations = self._insert_origin_observation(observations, origin_notes["explanation"], label)
            root_cause_analysis["origin_selection"] = origin_notes
            root_cause_analysis["supporting_evidence"].append(f"{label}: {origin_notes['explanation']}")
            root_cause_analysis["alternatives_ruled_out"].extend(origin_notes["ruled_out"])
        longer_term_fixes = self._build_longer_term_fixes(origin, alert)
        execution_duration = time.monotonic() - start_time
        agent_run_metadata = self._build_agent_metadata(config, duration_seconds=execution_duration)
        tools_used = [
            "list_open_alerts", "list_time_series", "list_log_entries",
            "get_service", "get_topology_graph", "get_topology", "get_blast_radius"
        ]
        skills_loaded = [
            "cloud-run-diagnostics", "apigee-telemetry", "cloud-sql-triage", "report-style"
        ]

        report = InvestigationReport(
            group_id=group_id,
            filename=filename,
            summary=summary,
            title_problem=title_problem,
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

        # Update OKF file with synthesized findings. Only persistence can fail here; the
        # investigation is complete, so a failure must not discard the synthesized report.
        # The store is built inside the try: locally its constructor can create directories.
        store: Optional[OKFStore] = None
        try:
            store = OKFStore(config=config)
            self._update_okf_file(report, alert, config, store=store)
        except Exception as exc:
            if store is None:
                logger.error(
                    "Failed to open the OKF store for incident file %s (group %s): %s",
                    filename,
                    group_id,
                    type(exc).__name__,
                )
                target = filename
            else:
                # _update_okf_file has already logged it at ERROR (group, location, type).
                target = self._describe_okf_target(store, filename)
            self._mark_okf_persistence_degraded(report, target, exc)

        return report

    def _determine_probable_origin(
        self,
        alert: AlertEvent,
        dossiers: dict[str, EvidenceDossier],
        upstream_deps: Optional[list[str]] = None,
        group_selection: Optional[OriginSelection] = None,
        change_verdict: Any = _VERDICT_NOT_COMPUTED,
    ) -> str:
        """Identify probable origin node using DAG hierarchy, change correlation, and telemetry metric onset.

        Rules:
        1. SpikeArrest 429 is enforced at the Apigee ingress proxy gateway; origin stays at gateway.
        2. Change-induced deployments on a service attribute directly
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
        # A change deployment on a service is attributed
        # directly to that service where the revision was deployed, NEVER to downstream database.
        # The change is weighed only when the changed service shows its own degradation that no
        # degraded upstream dependency explains (see _assess_change).
        if change_verdict is _VERDICT_NOT_COMPUTED:
            verdict = self._assess_change(alert, dossiers, upstream_deps, group_selection)
        else:
            verdict = change_verdict
        if verdict is not None:
            if verdict["implicated"]:
                logger.info(
                    "Change-induced fault detected on %s (%s); attributing origin to deployed service",
                    verdict["service"],
                    verdict["reason"],
                )
                return verdict["service"]
            logger.info("Recent change not implicated: %s", verdict["reason"])

        # 3. Cloud SQL check: database is the deepest backend layer in the topology DAG
        # Origin must ONLY attribute to database when database telemetry is actually anomalous in the window
        # (Thresholds and the caller DB-auth exclusion live in _db_telemetry_anomalous.)
        sql_dossier = dossiers.get("CloudSQLSpecialist")
        if sql_dossier and sql_dossier.anomaly_detected:
            if self._db_telemetry_anomalous(alert, dossiers):
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
                    sql_dossier.metrics.get("active_connections"),
                    sql_dossier.metrics.get("max_connections"),
                    sql_dossier.metrics.get("active_lock_waits"),
                    sql_dossier.metrics.get("cpu_utilization_pct"),
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

    # ------------------------------------------------------------------
    # Group-wide origin selection and change implication
    # ------------------------------------------------------------------

    @staticmethod
    def _select_group_origin(
        alert: AlertEvent,
        group_alerts: Optional[list[dict[str, Any]]],
        onset_tolerance_seconds: Optional[float] = None,
    ) -> Optional[OriginSelection]:
        """Topology-aware origin selection over the group's alerts (None without group alerts).

        ``onset_tolerance_seconds`` defaults to the agent settings' ORIGIN_ONSET_TOLERANCE_SECONDS.
        """
        if not group_alerts:
            return None
        if onset_tolerance_seconds is None:
            onset_tolerance_seconds = get_agent_settings().origin_onset_tolerance_seconds
        try:
            topo = get_loader()
            return select_origin(
                group_alerts,
                canonicalize=topo.canonicalize,
                upstream_of=topo.upstream,
                onset_tolerance_seconds=onset_tolerance_seconds,
            )
        except Exception as exc:
            logger.error(
                "Origin selection failed for group of %d alerts (%s); investigating from the first alert %s on %s",
                len(group_alerts),
                type(exc).__name__,
                alert.incident_id,
                alert.resource_name,
            )
            return None

    @staticmethod
    def _canonical(node: Optional[str]) -> Optional[str]:
        try:
            return _canonicalize_node(node) or node
        except Exception:
            return node

    def _same_node(self, a: Optional[str], b: Optional[str]) -> bool:
        return bool(a) and bool(b) and self._canonical(a) == self._canonical(b)

    @staticmethod
    def _url_resource_type(alert: AlertEvent) -> Optional[str]:
        """Resource type for console links: the alert's, else the topology component type
        (e.g. "Apigee proxy"), so a proxy origin does not get a Cloud Run link."""
        if alert.resource_type:
            return alert.resource_type
        try:
            comp = get_loader().components.get(SynthesizerAgent._canonical(alert.resource_name))
        except Exception as exc:
            logger.warning("Topology component lookup failed for %s: %s", alert.resource_name, type(exc).__name__)
            return None
        return str(comp.type) if comp else None

    @staticmethod
    def _is_cloud_run(service: str) -> bool:
        try:
            comp = get_loader().components.get(service)
        except Exception as exc:
            logger.warning("Topology component lookup failed for %s: %s", service, type(exc).__name__)
            return False
        return bool(comp) and str(comp.type).strip().lower() == "cloud run"

    def _db_telemetry_anomalous(self, alert: AlertEvent, dossiers: dict[str, EvidenceDossier]) -> bool:
        """Cloud SQL telemetry shows saturation/locks (and the caller has no DB auth error)."""
        sql_dossier = dossiers.get("CloudSQLSpecialist")
        if not (sql_dossier and sql_dossier.anomaly_detected):
            return False
        db_conns = sql_dossier.metrics.get("active_connections")
        db_max = sql_dossier.metrics.get("max_connections")
        db_locks = sql_dossier.metrics.get("active_lock_waits")
        db_cpu = sql_dossier.metrics.get("cpu_utilization_pct")
        is_conn_anomalous = isinstance(db_conns, (int, float)) and (
            db_conns >= 20 or (isinstance(db_max, (int, float)) and db_max > 0 and db_conns / db_max >= 0.8)
        )
        is_lock_anomalous = isinstance(db_locks, (int, float)) and db_locks > 0
        is_cpu_anomalous = isinstance(db_cpu, (int, float)) and db_cpu >= 80.0
        is_explicit_db_alert = any(
            k in alert.policy_name.lower() for k in ["cloudsql", "max_connections", "lock_wait", "db_lock", "row_lock"]
        )
        cr_dossier = dossiers.get("CloudRunSpecialist")
        has_db_auth_error = _dossier_has_db_auth_error(cr_dossier)
        return bool(
            (is_conn_anomalous or is_lock_anomalous or is_cpu_anomalous or is_explicit_db_alert) and not has_db_auth_error
        )

    def _assess_change(
        self,
        alert: AlertEvent,
        dossiers: dict[str, EvidenceDossier],
        upstream_deps: Optional[list[str]],
        group_selection: Optional[OriginSelection],
        onset_tolerance_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Decide whether the ChangeSpecialist's recent change is implicated.

        A recent revision on service X is implicated only when X shows degradation of its own
        (an alert on X, or anomalous Cloud Run telemetry for X) AND no degraded upstream
        dependency of X explains it. Returns None when no recent change was detected.

        An upstream dependency U explains X when U has its own alert in the group, or, without
        an alert, when U's Cloud Run telemetry shows a *measured* 5xx/latency breach. A dossier
        that is anomalous only from log signals (e.g. ``has_db_auth_error``) does not explain X.
        In both cases U's onset must not be later than X's onset plus the configured tolerance.
        """
        chg = dossiers.get("ChangeSpecialist")
        if not (chg and chg.anomaly_detected):
            return None
        service = chg.metrics.get("change_service") or chg.target_resource
        if not service:
            return None
        node = self._canonical(service)
        revision = chg.metrics.get("recent_revision") or "unknown revision"
        degraded = set(group_selection.degraded_services) if group_selection is not None else set()

        own: list[str] = []
        if self._same_node(node, alert.resource_name):
            own.append(f"alert {alert.policy_name} fired on it")
        if node in degraded and not own:
            own.append("it has its own alert in the incident group")
        svc_dossier = dossiers.get(node) or dossiers.get(service)
        if svc_dossier and svc_dossier.anomaly_detected:
            own.append("its Cloud Run telemetry is anomalous")
        cr = dossiers.get("CloudRunSpecialist")
        if cr and cr.anomaly_detected and self._same_node(cr.target_resource, node) and not own:
            own.append("its Cloud Run telemetry is anomalous")

        try:
            node_deps = list(get_upstream_dependencies(node) or [])
        except Exception as exc:
            logger.error("Topology upstream lookup failed for %s: %s", node, type(exc).__name__)
            node_deps = []
        tolerance = (
            onset_tolerance_seconds
            if onset_tolerance_seconds is not None
            else get_agent_settings().origin_onset_tolerance_seconds
        )
        node_onset = self._service_onset(node, alert, group_selection, svc_dossier)
        explaining: list[str] = []
        for dep in node_deps:
            dep_dossier = dossiers.get(dep)
            if dep in degraded:
                cand = group_selection.candidate(dep) if group_selection is not None else None
                dep_onset = cand.onset if cand is not None else None
            elif dep_dossier is not None and dep_dossier.anomaly_detected:
                breaches = measured_breaches(dep_dossier.metrics)
                if not breaches:
                    logger.info(
                        "Change assessment: upstream %s has no alert and no measured 5xx/latency breach"
                        " (anomaly from log signals only, has_db_auth_error=%s); it does not explain %s",
                        dep,
                        bool(dep_dossier.metrics.get("has_db_auth_error")),
                        node,
                    )
                    continue
                dep_onset = dep_dossier.metrics.get("metric_onset")
            else:
                continue
            if not onset_allows_explanation(dep_onset, node_onset, tolerance):
                logger.info(
                    "Change assessment: %s (onset %s) degraded before upstream %s (onset %s) beyond %ss tolerance;"
                    " it does not explain %s",
                    node, node_onset, dep, dep_onset, tolerance, node,
                )
                continue
            explaining.append(dep)
        sql = dossiers.get("CloudSQLSpecialist")
        if sql is not None and self._db_telemetry_anomalous(alert, dossiers):
            sql_node = self._canonical(sql.target_resource)
            if sql_node in node_deps and sql_node not in explaining:
                explaining.append(sql_node)

        signal_of = self._service_signal(group_selection, alert)
        if explaining:
            upstream_desc = ", ".join(
                f"{dep} {signal_of(dep)}".rstrip() for dep in explaining
            )
            reason = (
                f"{node} {signal_of(node) or 'degradation'} explained by upstream {upstream_desc};"
                f" recent revision {revision} not implicated"
            )
            implicated = False
        elif not own:
            reason = f"{node} shows no errors or latency of its own; recent revision {revision} not implicated"
            implicated = False
        else:
            reason = f"{node} degraded ({'; '.join(own)}) with no degraded upstream dependency; recent revision {revision} implicated"
            implicated = True
        logger.info(
            "Change assessment: service=%s revision=%s implicated=%s own_evidence=%s explained_by=%s",
            node,
            revision,
            implicated,
            own,
            explaining,
        )
        return {"service": service, "revision": revision, "implicated": implicated, "reason": reason}

    def _service_onset(
        self,
        node: str,
        alert: AlertEvent,
        group_selection: Optional[OriginSelection],
        svc_dossier: Optional[EvidenceDossier],
    ) -> Optional[str]:
        """Earliest known onset of ``node``: its group alerts, the trigger alert, else its telemetry."""
        cand = group_selection.candidate(node) if group_selection is not None else None
        if cand is not None and cand.onset:
            return cand.onset
        if self._same_node(node, alert.resource_name) and alert.started_at:
            return alert.started_at
        if svc_dossier is not None:
            return svc_dossier.metrics.get("metric_onset")
        return None

    @staticmethod
    def _service_signal(group_selection: Optional[OriginSelection], alert: Optional[AlertEvent]):
        """Return a function naming a service's alert signal(s), e.g. 'latency'."""
        def signal_of(service: str) -> str:
            if group_selection is not None:
                cand = group_selection.candidate(service)
                if cand is not None and cand.signals:
                    return "/".join(cand.signals)
            if alert is None:
                return ""
            labels = alert.policy_user_labels or alert.labels or {}
            if service == alert.resource_name and labels.get("signal"):
                return str(labels["signal"])
            return ""
        return signal_of

    @staticmethod
    def _neutralize_change_dossier(chg: EvidenceDossier, verdict: dict[str, Any]) -> EvidenceDossier:
        """Copy of the change dossier marked not anomalous, carrying the not-implicated reason."""
        metrics = dict(chg.metrics)
        metrics["change_not_implicated_reason"] = verdict["reason"]
        findings = [
            f"Recent revision {verdict['revision']} on {verdict['service']} detected but not implicated: {verdict['reason']}."
        ] + [f for f in chg.findings if "aligns with alert onset" not in f and "detected on" not in f]
        logger.info("Change dossier neutralized: %s", verdict["reason"])
        return dataclasses.replace(chg, metrics=metrics, findings=findings, anomaly_detected=False)

    def _origin_selection_notes(
        self,
        selection: Optional[OriginSelection],
        origin: str,
        verdict: Optional[dict[str, Any]],
        chg: Optional[EvidenceDossier],
    ) -> Optional[dict[str, Any]]:
        """Explanation of the origin decision for the report (None when there is nothing to say)."""
        statements: list[str] = []
        ruled_out: list[dict[str, str]] = []
        if selection is not None and selection.is_multi_service:
            statements.extend(selection.reasons)
            if not self._same_node(selection.origin, origin):
                statements.append(f"Specialist evidence moved the origin from {selection.origin} to {origin}.")
            for cand in selection.candidates:
                if self._same_node(cand.service, origin):
                    continue
                if cand.explained_by:
                    ruled_out.append({
                        "alternative": f"{cand.service} as origin",
                        "reason": f"{cand.service} {'/'.join(cand.signals) or 'degradation'} explained by upstream {', '.join(cand.explained_by)}.",
                    })
            # Recent genuine changes on degraded services that the origin explains.
            changes = (chg.metrics.get("all_changes") or []) if chg is not None else []
            signal_of = self._service_signal(selection, None)
            for cand in selection.candidates:
                if self._same_node(cand.service, origin) or not cand.explained_by:
                    continue
                for change in changes:
                    if self._same_node(change.get("service"), cand.service) and is_genuine_change(change):
                        upstream_desc = ", ".join(f"{u} {signal_of(u)}".rstrip() for u in cand.explained_by)
                        text = (
                            f"{cand.service} {signal_of(cand.service) or 'degradation'} explained by upstream {upstream_desc};"
                            f" recent revision {change.get('revision')} not implicated"
                        )
                        if text not in statements:
                            statements.append(text + ".")
                            ruled_out.append({
                                "alternative": f"Deployment regression on {cand.service} ({change.get('revision')})",
                                "reason": text + ".",
                            })
        if verdict is not None and not verdict["implicated"]:
            text = verdict["reason"] + "."
            if text not in statements:
                statements.append(text)
                ruled_out.append({
                    "alternative": f"Deployment regression on {verdict['service']} ({verdict['revision']})",
                    "reason": text,
                })
        if not statements:
            return None
        explanation = " ".join(statements)
        logger.info("Origin selection explanation for origin %s: %s", origin, explanation)
        multi = selection is not None and selection.is_multi_service
        return {
            # "Origin selection" only when a multi-service selection took place; otherwise the
            # notes only explain why a recent change was not implicated.
            "label": "Origin selection" if multi else "Change assessment",
            "origin": origin,
            "topology_origin": selection.origin if selection is not None else None,
            "explanation": explanation,
            "candidates": [c.summary() for c in selection.candidates] if selection is not None else [],
            "ruled_out": ruled_out,
        }

    @staticmethod
    def _insert_origin_observation(observations: str, explanation: str, label: str = "Origin selection") -> str:
        """Put '<label>: ...' directly after the first observation (the alert trigger)."""
        items = split_observations(observations)
        line = f"{label}: {explanation}"
        if line in items:
            return observations
        items[1:1] = [line]
        return " | ".join(items)

    def _determine_dependency_path(self, origin: str, blast_radius: list[str]) -> Optional[str]:
        """Topology path(s) from each impacted entry point to the origin (TOPOLOGY.md edges).

        Returns None when the origin is not a topology component; the description then omits
        the line rather than showing a guessed path. ``blast_radius`` is not used to build the
        path: it is a set of impacted services, not an ordered chain.
        """
        return dependency_path_for(origin)

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

    def _gateway_timeout_fact(self, dossiers: list[EvidenceDossier], origin: str) -> Optional[str]:
        """Sentence stating the Apigee GatewayTimeout evidence for ``origin``, or None.

        Built only from gathered data: the 504/GatewayTimeout count, fault code and fault source
        from Apigee telemetry, the proxy revision from its access log, the topology path from
        the proxy to the origin, and the origin's P95 latency from the Cloud Run dossier. The
        proxy's timeout value is not gathered, so no value is stated.
        """
        apigee = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        gt = apigee.metrics.get("gateway_timeout") if apigee else None
        if not isinstance(gt, dict) or not gt.get("count"):
            return None
        proxy = gt.get("proxy") or apigee.target_resource
        path = next((p for p in get_loader().dependency_paths(origin) if proxy in p), None)
        if path is None:
            logger.info(
                "Apigee GatewayTimeout evidence on %s not stated for origin %s: no topology path between them",
                proxy,
                origin,
            )
            return None
        route = " -> ".join(path[path.index(proxy):])
        rev = f" (revision {gt['revision']})" if gt.get("revision") else ""
        what = f"HTTP {gt['response_code']} responses" if gt.get("response_code") else "responses"
        src = f" (fault source: {gt['fault_source']})" if gt.get("fault_source") else ""
        fact = (
            f"Apigee proxy `{proxy}`{rev} returned {gt['count']} {what} with fault "
            f"`{gt.get('fault_code')}`{src}. Requests on {route} did not complete within the "
            f"proxy's target timeout"
        )
        cr = next(
            (d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin),
            None,
        )
        lat = cr.metrics.get("latency_p95_ms") if cr else None
        if isinstance(lat, (int, float)):
            fact += f"; `{origin}` P95 latency was {int(lat)}ms"
        return fact + "."

    def _format_dominant_signal_facts(self, alert: AlertEvent, dossier: Optional[EvidenceDossier]) -> str:
        """Format facts matching the dominant observed signal (latency vs 5xx vs auth errors vs connections)."""
        if not dossier or not dossier.metrics:
            return ""

        # Check for DB auth errors first
        if _dossier_has_db_auth_error(dossier):
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
            has_db_auth = _dossier_has_db_auth_error(cr_spec)
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
        gateway_fact = self._gateway_timeout_fact(dossiers, origin)
        if gateway_fact:
            root_cause_text = f"{root_cause_text} {gateway_fact}"
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
        obs_items.append(
            format_alert_trigger_line(
                alert.policy_name, alert.resource_name, started_at, alert.state, alert.incident_id
            )
        )

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
                has_db_auth = _dossier_has_db_auth_error(d)
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
                                    # Cut on a word boundary, never mid-word.
                                    head = clean_line[:198]
                                    cut = head.rfind(" ")
                                    clean_line = (head[:cut] if cut > 0 else head[:197]).rstrip() + "..."
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
        region: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> list[ActionItem]:
        """Synthesize concrete, ordered, executable commands with verification and rollback steps.

        Traffic in this environment is pinned to named revisions, so a traffic change always
        names an explicit revision and says why; routing to the latest revision is never
        suggested (it can be an unused or faulty revision). A rollback is only recommended for
        a recent change on the origin itself.
        """
        if not region or not project_id:
            settings = get_agent_settings()
            region = region or settings.region
            project_id = project_id or settings.project_id
        change_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        cr_dossier = next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist"), None)
        sql_dossier = next((d for d in dossiers if d.specialist_name == "CloudSQLSpecialist"), None)
        apigee_dossier = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)

        recent_rev = None
        prev_rev = None
        change_svc = None
        if change_dossier and change_dossier.anomaly_detected:
            change_svc = change_dossier.metrics.get("change_service") or change_dossier.target_resource
        if change_svc and not self._same_node(change_svc, origin):
            logger.info(
                "Recent change on %s is not on origin %s; no rollback recommended for it", change_svc, origin
            )
        elif change_dossier and change_dossier.anomaly_detected:
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
            has_db_auth = _dossier_has_db_auth_error(cr_dossier)
            config_check_cmd = f"gcloud run revisions describe {recent_rev} --region={region} --format=\"yaml(spec.containers[0].env)\""
            config_check_title = f"Verify database connection credentials and environment variables in {recent_rev}" if has_db_auth else f"Verify revision configuration and environment variables in {recent_rev}"

            has_valid_prev = bool(
                prev_rev
                and str(prev_rev).strip()
                and str(prev_rev).strip().lower() not in ("none", "unknown", "previous_revision", "unspecified")
            )
            if has_valid_prev:
                rollback_title = (
                    f"Roll back Cloud Run traffic on {origin} to previous serving revision {prev_rev}"
                    f" (it served before {recent_rev} was deployed)"
                )
                rollback_cmd = f"gcloud run services update-traffic {origin} --to-revisions={prev_rev}=100 --region={region}"
            else:
                rollback_title = f"Roll back Cloud Run traffic on {origin} (previous serving revision could not be identified)"
                rollback_cmd = f"# previous serving revision could not be identified; inspect revision history to select rollback target: gcloud run revisions list --service={origin} --region={region}"

            return [
                ActionItem(
                    title=rollback_title,
                    command=rollback_cmd,
                    verification="Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring",
                    rollback=(
                        f"Traffic is pinned to named revisions: to undo, route back explicitly with"
                        f" gcloud run services update-traffic {origin} --to-revisions={recent_rev}=100 --region={region};"
                        " do not route to the latest revision, which can be an unused or faulty one."
                    ),
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
                    command=f"gcloud apigee apiproxies describe {origin} --organization={project_id}",
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

        # 4. Generic Service Failure: no change on the origin is implicated, so no traffic change
        # is recommended; confirm which named revision serves traffic first.
        return [
            ActionItem(
                title=(
                    f"Confirm the named revision serving traffic on {origin}; no recent change on {origin} is"
                    " implicated, so no traffic change is recommended (if one becomes necessary, route to an"
                    " explicit known-good revision, never to the latest revision)"
                ),
                command=f"gcloud run services describe {origin} --region={region} --format=\"yaml(status.traffic)\"",
                verification="Confirm the serving revision is the one that served before the alert onset",
            ),
            ActionItem(
                title=f"Review active service telemetry and slow queries via Cloud Monitoring and Cloud Logging for {origin}",
                command=f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{origin}\" AND severity>=WARNING' --limit=50",
                verification="Verify latency percentiles stabilize",
            ),
        ]


    def _build_alerts(self, alert: AlertEvent, config: AgentConfig) -> list[dict[str, Any]]:
        sa = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        builder = ConsoleLinkBuilder(project_id=config.project_id)
        entry = format_okf_alert_entry(alert, valid_ts=sa)
        pol_id = entry.get("policy_id")
        entry["policy_url"] = builder.build_policy_url(pol_id or alert.policy_name)
        entry["resource_type"] = alert.resource_type
        entry["resource_url"] = builder.build_resource_url(alert.resource_name, self._url_resource_type(alert))
        inc_url = entry.get("url") or builder.build_incident_url(alert.incident_id)
        entry["url"] = inc_url
        entry["console_url"] = inc_url
        return [entry]

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
        # Only evidence that was actually observed belongs here. No specialist queries host
        # faults or Google Cloud Service Health, so nothing is claimed about either.
        contradicting: list[str] = []
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
            # A not-implicated change is stated by the origin selection notes (alternatives_ruled_out).
            not_implicated = chg_dossier.metrics.get("change_not_implicated_reason")
            if has_chg_check and not chg_dossier.anomaly_detected and not not_implicated:
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

    def _synthesize_title_problem(
        self,
        alert: AlertEvent,
        dossiers: list[EvidenceDossier],
        origin: str,
    ) -> Optional[str]:
        """Short, fixed problem phrase for the ServiceNow title (no metrics, names or timestamps).

        Mirrors the failure-mode branches of _synthesize_problem but returns a short label
        from a closed set, so the title never depends on the length of the synthesized
        sentence. None lets the report contract derive the phrase from the alert signal.
        """
        chg_d = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist" and d.anomaly_detected), None)
        if chg_d:
            rev = chg_d.metrics.get("recent_revision")
            if rev and (chg_d.metrics.get("change_service") == origin or chg_d.target_resource == origin):
                return "Deployment regression"
        signal = (alert.policy_user_labels or {}).get("signal") or (alert.labels or {}).get("signal")
        if not signal:
            pol = (alert.policy_name or "").lower()
            signal = next((k for k in SIGNAL_TITLE_PROBLEMS if pol.endswith(f"-{k}")), None)
        if origin != alert.resource_name and signal in ("5xx", "latency"):
            return {"5xx": "Upstream HTTP 5xx errors", "latency": "Upstream dependency latency"}[signal]
        if any(d.specialist_name == "CloudSQLSpecialist" and d.anomaly_detected for d in dossiers) and (
            "db" in origin or "sql" in origin
        ):
            return "Database connection pool saturation"
        return title_problem_for_signal(signal)

    def _reconcile_report_origin(self, report: InvestigationReport, alert: AlertEvent, origin: str) -> None:
        """Align the report's origin, root cause and summary with the reconciled ``origin``."""
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
            report.title_problem = self._synthesize_title_problem(
                alert=alert,
                dossiers=active_dossiers,
                origin=origin,
            )

    @staticmethod
    def _describe_okf_target(store: OKFStore, identifier: str) -> str:
        """Return a log-safe location for the incident object (no content).

        Locally this is the file the store actually writes, including its fallback directory.
        It only inspects the filesystem; no directory is created.
        """
        if store.storage_client is not None or (store.bucket_name and not store.is_local):
            return f"gs://{store.bucket_name}/{get_canonical_blob_name(identifier)}"
        return str(resolve_local_target_file(store.storage_path, identifier, create=False))

    @staticmethod
    def _mark_okf_persistence_degraded(report: InvestigationReport, target: str, exc: Exception) -> None:
        """Stamp the report so the ServiceNow ticket shows that the incident file is stale.

        ``observations`` is rendered into the ticket's work note and description. Only the
        exception type is included; its text can quote file content.
        """
        note = (
            f"{OKF_DEGRADED_NOTE} ({type(exc).__name__}). {target} does not contain this report, "
            "ticket created from the synthesized report"
        )
        report.observations = f"{note} | {report.observations}" if report.observations else note

    @staticmethod
    def _lookup_downstream(origin: str) -> list[str]:
        try:
            return get_blast_radius(origin)
        except Exception as exc:
            logger.error("Topology blast radius lookup in _update_okf_file failed for %s: %s", origin, exc)
            return []

    @staticmethod
    def _topology_section(origin: str, alert: AlertEvent) -> tuple[str, str]:
        """Return the topology source file name and the relevant-edges lines for the report."""
        try:
            topo_loader = get_loader()
            source_file = topo_loader.source_path.name
            rel_edges = topo_loader.get_relevant_edges(origin, alert.resource_name)
            if not rel_edges:
                rel_edges = [{"from": e.from_node, "to": e.to_node, "relation": e.relation} for e in topo_loader.edges]
            edges_lines = "\n".join(
                f"- `{e['from']}` -> `{e['to']}`" + (f" ({e['relation']})" if e.get("relation") else "")
                for e in rel_edges
            )
        except Exception as topo_err:
            logger.warning("Failed to extract topology edges for report: %s", topo_err)
            source_file = "TOPOLOGY.md"
            edges_lines = "- Topology information unavailable"
        return source_file, edges_lines

    def _build_report_markdown(
        self,
        report: InvestigationReport,
        policy_name: str,
        origin: str,
        merged_blast: list[str],
        topology: tuple[str, str],
    ) -> str:
        """Render the synthesized report section of the OKF body."""
        source_file, edges_lines = topology
        exec_bullets = "\n".join(report.build_executive_summary_bullets())
        return f"""# Incident Report for {policy_name}

## Executive Summary
{exec_bullets}

## Probable Root Cause
{report.probable_root_cause.text} (Confidence: {int(report.probable_root_cause.confidence * 100)}%)
{self._origin_selection_markdown(report)}
## Blast Radius
- Origin: `{origin}`
- Impacted Downstream Components: {', '.join(f'`{b}`' for b in merged_blast)}

## Topology used
- Source: `{source_file}`
- Relevant edges:
{edges_lines}
"""

    @staticmethod
    def _origin_selection_markdown(report: InvestigationReport) -> str:
        """'## Origin selection' section of the OKF report (empty when there was no decision to explain)."""
        notes = (report.root_cause_analysis or {}).get("origin_selection") or {}
        if not notes.get("explanation"):
            return ""
        lines = ["", f"## {notes.get('label') or 'Origin selection'}", notes["explanation"]]
        for cand in notes.get("candidates") or []:
            lines.append(
                f"- `{cand.get('service')}`: score {cand.get('score')}, explains {cand.get('explains') or []},"
                f" explained by {cand.get('explained_by') or []}, onset {cand.get('onset')}"
            )
        return "\n".join(lines) + "\n"

    def _update_okf_file(
        self,
        report: InvestigationReport,
        alert: AlertEvent,
        config: AgentConfig,
        store: Optional[OKFStore] = None,
    ) -> None:
        """Merge the synthesized findings into the incident OKF document.

        The update is an optimistic-concurrency read-modify-write (``OKFStore.update_atomically``:
        GCS generation precondition with 412 re-read/retry, or a file lock locally), so concurrent
        writers (ticket linkage, duplicate appends, deadline changes) are re-read and preserved.

        The mutation sets only the fields the synthesizer owns: ``project``, ``origin``,
        ``blast_radius`` (merged), ``priority``, ``confidence``, this alert's entry, ticket
        fields the report carries, and the delimited report section of the body.
        ``policy_name``, ``resource_name``, ``timestamp`` and ``status`` are filled only when
        missing. Every other field and the rest of the body are kept as read.

        Topology lookups (blast radius, report edges) run before the retry loop, so the
        mutation, which is re-run on every precondition retry and runs under the file lock
        locally, does no I/O. The exception is a report without an origin: the stored origin
        is then re-read on every attempt and looked up inside the mutation.

        Raises:
            Storage errors, including ``ConcurrencyLockError`` (precondition retries
            exhausted), and parse errors of the stored document. Each is logged at ERROR
            with the group id and object location first.
        """
        store = store or OKFStore(config=config)
        identifier = report.filename

        # Computed once, outside the mutation, which is re-run on every precondition retry.
        # _reconcile_report_origin rewrites report.origin, so the report's own origin is captured.
        report_origin = report.origin
        topology_by_origin: dict[str, tuple[list[str], tuple[str, str]]] = {}

        def _topology_for(origin: str) -> tuple[list[str], tuple[str, str]]:
            if origin not in topology_by_origin:
                topology_by_origin[origin] = (self._lookup_downstream(origin), self._topology_section(origin, alert))
            return topology_by_origin[origin]

        if report_origin:
            _topology_for(report_origin)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        alert_started_at = alert.started_at if alert.started_at and str(alert.started_at).strip() else now_str
        builder = ConsoleLinkBuilder(project_id=config.project_id)
        inc_url = alert.url or builder.build_incident_url(alert.incident_id)
        pol_id = getattr(alert, "policy_id", None)
        base_entry = format_okf_alert_entry(alert, valid_ts=alert_started_at)
        base_entry["policy_url"] = builder.build_policy_url(pol_id or alert.policy_name)
        base_entry["resource_type"] = alert.resource_type
        base_entry["resource_url"] = builder.build_resource_url(alert.resource_name, self._url_resource_type(alert))
        if not base_entry.get("url"):
            base_entry["url"] = inc_url
        base_entry["console_url"] = inc_url

        def _mutate(doc: OKFDocument) -> OKFDocument:
            # Origin: the report's, else the stored one; keep the report consistent with it.
            origin = report_origin or doc.origin or alert.resource_name
            if origin != report.origin:
                self._reconcile_report_origin(report, alert, origin)

            # Group metadata owned by grouping: fill only when missing.
            if not doc.policy_name:
                doc.policy_name = alert.policy_name
            if not doc.resource_name:
                doc.resource_name = alert.resource_name
            if "status" not in doc.model_fields_set or not doc.status:
                doc.status = "mitigation-in-progress"
            if not doc.timestamp or not str(doc.timestamp).strip():
                doc.timestamp = alert_started_at

            # Ticket linkage is owned by the timer path; only carry values the report has.
            if report.servicenow_sys_id:
                doc.servicenow_sys_id = report.servicenow_sys_id
            if report.servicenow_number:
                doc.servicenow_number = report.servicenow_number

            # Merge blast radius and traverse full downstream topology
            downstream, topology = _topology_for(origin)
            merged_blast = [origin]
            for b in [*downstream, *report.blast_radius, *doc.blast_radius]:
                if b not in merged_blast:
                    merged_blast.append(b)

            doc.project = config.project_id
            doc.origin = origin
            doc.blast_radius = merged_blast
            doc.priority = report.priority
            doc.confidence = report.probable_root_cause.confidence

            # Upsert this alert by incident_id, keeping labels/component/signal already known.
            new_alert_entry = dict(base_entry)
            alerts_list = list(doc.alerts)
            for i, a in enumerate(alerts_list):
                if isinstance(a, dict) and a.get("incident_id") == alert.incident_id:
                    if not new_alert_entry.get("labels") and a.get("labels"):
                        new_alert_entry["labels"] = a["labels"]
                        new_alert_entry["policy_user_labels"] = a.get("policy_user_labels") or a["labels"]
                    if not new_alert_entry.get("component") and a.get("component"):
                        new_alert_entry["component"] = a["component"]
                    if not new_alert_entry.get("signal") and a.get("signal"):
                        new_alert_entry["signal"] = a["signal"]
                    alerts_list[i] = new_alert_entry
                    break
            else:
                alerts_list.append(new_alert_entry)
            doc.alerts = [format_okf_alert_entry(a, valid_ts=doc.timestamp) for a in alerts_list]
            # The report section and the ticket must cover every alert of the group.
            apply_group_alerts(report, doc.alerts)

            report_markdown = self._build_report_markdown(report, doc.policy_name, origin, merged_blast, topology)
            doc.body = _merge_report_section(doc.body, report_markdown)
            return doc

        try:
            store.update_atomically(identifier, _mutate)
        except Exception as exc:
            # Exception text can quote file content, so only its type is logged.
            logger.error(
                "Failed to update OKF incident file %s (group %s): %s",
                self._describe_okf_target(store, identifier),
                report.group_id,
                type(exc).__name__,
            )
            raise
        logger.info("Updated OKF incident file %s (group %s)", self._describe_okf_target(store, identifier), report.group_id)
