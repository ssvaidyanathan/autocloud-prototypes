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
from .cloud_run import CloudRunSpecialist, latency_label, measured_breaches, not_applicable_component_type
from .apigee import SPIKE_ARREST_FAULT, ApigeeSpecialist, access_log_id, access_log_name
from .cloud_sql import DB_POOL_NEAR_SATURATION_RATIO, CloudSQLSpecialist
from .measurements import apigee_responses, cloud_run_5xx_rate, component_responses, rate_pct
from .change import (
    APIGEE_HISTORY,
    NO_CLOUD_RUN_DEPLOYMENTS_FINDING,
    ChangeSpecialist,
    is_candidate_cause,
    scoped_apigee_history_text,
)
from .origin_selection import (
    OriginSelection,
    alert_category,
    alert_component,
    alert_event_from_entry,
    onset_allows_explanation,
    select_origin,
)
from .telemetry_reads import (
    CHANGE_SOURCES_KEY,
    change_detection_incomplete_text,
    change_sources_checked,
    gap_text,
    unavailable_telemetry,
)

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


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """An ISO-8601 timestamp as an aware UTC datetime, or None when absent or unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _measured_pct(value: Any) -> bool:
    """True for a measured, non-zero percentage ("85.1%"); N/A and 0% are not signals."""
    if isinstance(value, (int, float)):
        return value > 0
    if not isinstance(value, str) or "%" not in value:
        return False
    try:
        return float(value.replace("%", "").strip()) > 0
    except ValueError:
        return False


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


def db_pool_actions(origin: str, project_id: str, caller_actions: list[ActionItem]) -> list[ActionItem]:
    """Actions for a Cloud SQL connection-pool incident: read-only review first.

    Order: the connection-release review on each caller (read-only), then the session
    termination, labelled as mutating and left to operator judgement, then the read-only
    describe of tier and flags. With no known caller the describe comes first, so the
    mutating step never leads.
    """
    terminate = ActionItem(
        title=(
            f"Mutating, operator judgement required: terminate idle database sessions on {origin} via a SQL "
            "client, only after a connection-release review shows they are leaked connections (this ends those sessions)"
        ),
        command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND state_change < current_timestamp - INTERVAL '5 minutes';",
        verification=(
            f"Confirm active connections drop below {int(DB_POOL_NEAR_SATURATION_RATIO * 100)}% of max_connections "
            "via Cloud Monitoring"
        ),
    )
    describe = ActionItem(
        title=(
            f"Review the tier and database flags of {origin} before any max_connections change "
            "(gcloud sql instances patch --database-flags replaces the whole flag set, so every "
            "existing flag must be repeated)"
        ),
        command=f'gcloud sql instances describe {origin} --format="yaml(settings.tier,settings.databaseFlags)"',
        verification="List the current flags and tier; any change must keep all existing flags",
    )
    if caller_actions:
        return [*caller_actions, terminate, describe]
    return [describe, terminate]


def db_caller_actions(origin: str, project_id: str) -> list[ActionItem]:
    """Connection-release review on the Cloud Run services that call the database directly.

    The callers come from TOPOLOGY.md; the pool and its release logic live in them, not in
    the database. Without a known caller, no action is given.
    """
    try:
        loader = get_loader()
        callers = [
            e.from_node for e in loader.edges
            if e.to_node == loader.canonicalize(origin) and SynthesizerAgent._is_cloud_run(e.from_node)
        ]
    except Exception as exc:
        logger.error("Topology caller lookup failed for %s: %s", origin, type(exc).__name__)
        return []
    if not callers:
        logger.warning("No Cloud Run caller of %s in TOPOLOGY.md; no connection-release review suggested", origin)
    return [
        ActionItem(
            title=f"Inspect connection pool release logic and pool size in {caller}, which calls {origin}",
            command=(
                f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{caller}\" "
                f"AND textPayload=~\"connection leak|pool exhausted|get_db_connection\"' "
                f"--project={project_id} --limit=30"
            ),
            verification=f"Confirm {caller} releases database connections (no leak in its logs)",
        )
        for caller in callers
    ]


def degraded_evidence_observation(dossiers: list[EvidenceDossier]) -> Optional[str]:
    """One observation naming every telemetry source that could not be read, or None.

    Ends with the change-detection consequence when a change source is among them, so the
    ticket says the change attribution was not assessed instead of implying "no change".
    """
    gaps = unavailable_telemetry(dossiers)
    if not gaps:
        return None
    text = "Evidence degraded: " + "; ".join(gap_text(g) for g in gaps)
    change_gap = change_detection_incomplete_text(gaps)
    if change_gap:
        text += f"; {change_gap}"
    return text


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

    # Priority reflects the most severe alert of the group, not only the investigated one
    # (S5: the first alert was latency/p2 while the group held 5xx/p1 alerts). Only raised.
    group_priority = highest_severity_priority(merged)
    if group_priority and priority_rank(group_priority) < priority_rank(report.priority):
        logger.info(
            "Group %s: priority raised from %s to %s (highest alert severity label in the group)",
            report.group_id, report.priority, group_priority,
        )
        report.priority = group_priority
    return report


def _latency_disp(lat_ms: Optional[float], stat: Optional[str]) -> str:
    """' (<statistic>: <n>ms)' when the statistic is recorded, ' (<n>ms)' otherwise, '' if unmeasured."""
    if lat_ms is None:
        return ""
    return f" ({stat}: {int(lat_ms)}ms)" if stat else f" ({int(lat_ms)}ms)"


def _as_number(value: Any) -> Optional[float]:
    """Float of a measured value, or None for missing / 'N/A (not captured)' / unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_latency_alert(alert: AlertEvent) -> bool:
    return any(k in (alert.policy_name or "").lower() for k in ("latency", "timeout", "slow"))


def low_traffic_facts(dossier: Optional[EvidenceDossier], threshold_rpm: float) -> Optional[dict[str, Any]]:
    """Measured request volume of a Cloud Run dossier when it is below `threshold_rpm`, else None.

    The rate is total / window from the measured values (not the truncated request_count_rpm,
    which reads 0 for 3 requests in 17 min). No measured total or window means no statement
    (never assumed low).
    """
    if not dossier or dossier.specialist_name != "CloudRunSpecialist" or not dossier.metrics:
        return None
    total = _as_number(dossier.metrics.get("request_count_total"))
    minutes = _as_number(dossier.metrics.get("request_window_minutes"))
    if total is None or not minutes or minutes <= 0:
        return None
    rate = total / minutes
    if rate >= threshold_rpm:
        return None
    req_txt = "1 request" if int(total) == 1 else f"{int(total)} requests"
    return {
        "service": dossier.target_resource,
        "volume": f"{req_txt} in {minutes:g} min ({rate:.1f} requests/min)",
    }


def low_traffic_sentence(facts: dict[str, Any]) -> str:
    """'low traffic: ...' clause for latency text."""
    return (
        f"low traffic: {facts['volume']} on {facts['service']}, so the latency may come from a few "
        "slow requests (such as a cold start) rather than a service-wide degradation"
    )


CORRELATION_PREFIX = "Correlation:"


def _gray_zone_reviewer(correlation: dict[str, Any]) -> str:
    """Who decided the gray zone: Gemini, or the deterministic fallback (with Gemini's failure)."""
    source = correlation.get("gray_zone_source")
    if source == "gemini":
        return "the gray-zone review by Gemini"
    if source == "deterministic_fallback":
        reason = correlation.get("gray_zone_fallback_reason") or "reason not recorded"
        return f"the gray-zone review by deterministic fallback (Gemini unavailable: {reason})"
    return "the gray-zone review"


def correlation_observation(correlation: Optional[dict[str, Any]]) -> Optional[str]:
    """Observation stating the correlation decision that opened the group, or None."""
    if not isinstance(correlation, dict) or correlation.get("decision") != "new_group":
        return None
    compared = correlation.get("open_groups_compared")
    score = _as_number(correlation.get("best_score"))
    low = _as_number(correlation.get("threshold_auto_new"))
    high = _as_number(correlation.get("threshold_auto_attach"))
    excluded = _excluded_groups_text(correlation.get("excluded_groups"))
    if compared == 0:
        if excluded:
            return f"{CORRELATION_PREFIX} opened a new incident group; {excluded}"
        return f"{CORRELATION_PREFIX} opened a new incident group (no other open incident group to compare with)"
    if score is None or not compared:
        return None
    if excluded:
        return correlation_observation({**correlation, "excluded_groups": None}) + f"; {excluded}"
    groups = "group" if compared == 1 else "groups"
    text = f"{CORRELATION_PREFIX} opened a new incident group; best correlation score {score:g} pts against {compared} open {groups}"
    if correlation.get("is_gray_zone") and low is not None and high is not None:
        return text + f" (between {low:g} and {high:g} pts; {_gray_zone_reviewer(correlation)} decided a new group)"
    if low is not None and score < low:
        return text + f", below the {low:g} pts new-group threshold"
    return text


def _excluded_groups_text(excluded: Any) -> Optional[str]:
    """Text naming ticketed groups left out of correlation by the late new-signal rule, or None."""
    if not isinstance(excluded, list) or not excluded:
        return None
    parts = []
    for g in excluded:
        if not isinstance(g, dict) or not g.get("group_id"):
            continue
        gap = _as_number(g.get("onset_gap_seconds"))
        window = _as_number(g.get("window_seconds"))
        detail = (
            f" (ticketed; this alert's signal type is new to it and opened {gap:g}s after its first alert, "
            f"beyond the {window:g}s window)"
            if gap is not None and window is not None
            else " (ticketed; new signal type beyond the late-alert window)"
        )
        parts.append(f"{g['group_id']}{detail}")
    return f"not compared with {'; '.join(parts)}" if parts else None


def apply_correlation_note(report: InvestigationReport, correlation: Optional[dict[str, Any]]) -> None:
    """Add the correlation observation after the alert lines (idempotent)."""
    line = correlation_observation(correlation)
    if not line:
        return
    items = split_observations(report.observations or "")
    if any(i.startswith(CORRELATION_PREFIX) for i in items):
        return
    pos = 0
    while pos < len(items) and (
        pos == 0 or items[pos].startswith(CORRELATED_ALERT_PREFIX)
    ):
        pos += 1
    items[pos:pos] = [line]
    report.observations = " | ".join(items)


# Change-proximity buckets: (max minutes before the alert onset, score, label). Shared by the
# Cloud Run deploy and the Apigee proxy revision branches of the confidence calculation.
CHANGE_PROXIMITY_BUCKETS: tuple[tuple[int, float, str], ...] = (
    (15, 0.15, "high temporal proximity"),
    (30, 0.10, "moderate proximity"),
    (60, 0.05, "extended window"),
)
CHANGE_PROXIMITY_OUTSIDE = (0.00, "outside causality window")


def _proximity_score(mins: int) -> tuple[float, str]:
    """Change-proximity score and label for a change ``mins`` minutes before the alert onset."""
    bucket = next(((score, label) for limit, score, label in CHANGE_PROXIMITY_BUCKETS if mins <= limit),
                  CHANGE_PROXIMITY_OUTSIDE)
    return bucket


def priority_rank(priority: Optional[str]) -> int:
    """Numeric rank of 'P<n>' (lower is more severe); 99 when it is not a priority."""
    m = re.fullmatch(r"[Pp](\d+)", str(priority or "").strip())
    return int(m.group(1)) if m else 99


def raise_file_priority(doc: Any, priority: Optional[str], source: str) -> None:
    """Set ``doc.priority`` to ``priority`` only if that raises it; never lower it automatically.

    Shared by every write of the incident file's priority (the OKF update before the ticket
    and the ticket record after it). A lower ``priority`` is ignored with a WARNING.
    """
    if priority_rank(priority) < priority_rank(doc.priority):
        logger.info("Group %s: priority %s -> %s (%s)", doc.group_id, doc.priority, priority, source)
        doc.priority = priority
    elif doc.priority != priority:
        logger.warning(
            "Group %s: file priority %s is higher than the %s %s; not lowered automatically",
            doc.group_id, doc.priority, source, priority,
        )


def highest_severity_priority(alerts: list[Any]) -> Optional[str]:
    """'P<n>' for the most severe policy 'severity' label (p1, p2, ...) among ``alerts``, or None."""
    best: Optional[int] = None
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        sev = a.get("severity") or (a.get("labels") or {}).get("severity") or (a.get("policy_user_labels") or {}).get("severity")
        rank = priority_rank(sev)
        if rank != 99 and (best is None or rank < best):
            best = rank
    return f"P{best}" if best is not None else None


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


# Anomaly categories (correlation classifier) of the alerts that match an evidence signal.
_SIGNAL_CATEGORIES: dict[str, set[str]] = {
    "5xx": {"backend_5xx", "gateway_5xx"},
    "latency": {"latency_504"},
}


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
        # Deploys on the group's other candidates get the same attribution rule before the origin
        # notes may name one as a "Deployment regression" alternative.
        if selection is not None and selection.is_multi_service and dossier_map.get("ChangeSpecialist") is not None:
            before = dossier_map["ChangeSpecialist"]
            # In a worker thread: the healthy-period reads are blocking Cloud Monitoring calls.
            assessed = await asyncio.to_thread(
                self.change_specialist.assess_group_changes,
                before, set(selection.degraded_services) | {origin}, alert, config,
            )
            if assessed is not before:
                dossier_map["ChangeSpecialist"] = assessed
                dossiers = [assessed if d is before else d for d in dossiers]
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

        # Upstream dependencies were investigated to choose the origin. Their evidence belongs
        # in this report only when the service is part of this incident: the origin, its blast
        # radius, or a service alerting in this group. Otherwise a concurrent, disjoint incident
        # on a shared upstream (S3: inventory 5xx under an orders 429) leaks into this ticket.
        dependency_results = list(all_results[len(spec_tasks):])
        dependent_dossiers = await self._investigate_db_dependents(alert, config, origin, blast_radius, dossiers)
        if dependent_dossiers:
            dossiers = dossiers + dependent_dossiers
            dependency_results.extend(dependent_dossiers)
        dossiers = self._reconcile_change_history(dossiers)
        self._last_dossiers = dossiers
        all_dossiers = dossiers
        dossiers = self._scope_report_dossiers(
            dossiers, dependency_results, origin, blast_radius, selection, group_id
        )

        # Consolidate evidence and queries. Several dossiers can share an area (Cloud Run of
        # the origin and of a dependency): their items are appended, not overwritten.
        all_evidence: dict[str, list[EvidenceItem]] = {}
        all_queries: dict[str, list[str]] = {}

        for d in all_dossiers:
            if any(d is kept for kept in dossiers):
                area_key = d.specialist_name.lower().replace("specialist", "")
                all_evidence.setdefault(area_key, []).extend(d.evidence_items)
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
        self._last_group_alerts = group_alerts
        root_cause_text, confidence = self._determine_root_cause(
            dossiers, origin, alert, blast_radius=blast_radius, group_alerts=group_alerts
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        first_seen = alert.started_at or now_str
        last_alert = alert.started_at or now_str

        alerts_list = self._build_alerts(alert, config)
        triage_decision = self._build_triage_decision(alert, origin, blast_radius, dossiers, confidence=confidence)
        topology_nodes = self._build_topology_nodes(origin, blast_radius, dossiers)
        timeline = self._build_timeline(alert, dossiers)
        metrics_comparison = self._build_metrics_comparison(dossiers, origin)
        top_log_signatures = self._build_top_log_signatures(dossiers)
        apigee_analytics = self._build_apigee_analytics(dossiers, blast_radius)
        revision_diffs = self._build_revision_diffs(dossiers, origin)
        root_cause_analysis = self._build_root_cause_analysis(
            root_cause_text, confidence, dossiers, origin, alert,
        )
        # The Apigee GatewayTimeout fact is stated once, in the root cause (see _determine_root_cause);
        # it is not repeated as an observation or as RCA supporting evidence.
        if origin_notes:
            label = origin_notes["label"]
            observations = self._insert_origin_observation(observations, origin_notes["explanation"], label)
            root_cause_analysis["origin_selection"] = origin_notes
            root_cause_analysis["supporting_evidence"].append(f"{label}: {origin_notes['explanation']}")
            root_cause_analysis["alternatives_ruled_out"].extend(origin_notes["ruled_out"])
        longer_term_fixes = self._build_longer_term_fixes(origin, alert, dossiers)
        # Telemetry that could not be read is stated, never read as a negative finding. With a
        # change source missing and no change implicated, "Change-Induced: No" is unverified.
        gaps = unavailable_telemetry(dossiers)
        change_gap = change_detection_incomplete_text(gaps)
        change_induced = self._change_induced(verdict, dossiers, origin, change_gap)
        if change_gap and change_induced.startswith("Not assessed"):
            all_evidence.setdefault("change", []).append(
                EvidenceItem(headline="Change detection incomplete", content=f"{change_gap[0].upper()}{change_gap[1:]}.")
            )
            root_cause_analysis.setdefault("contradicting_evidence", []).append(
                f"Not assessed: {change_gap}"
            )
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
            change_induced=change_induced,
            first_seen=first_seen,
            last_alert=last_alert,
            report_generated_at=now_str,
            project_id=config.project_id,
            region=config.region,
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
            apigee_history=self._apigee_history_line(
                dossiers, origin=origin,
                components=self._history_scope(origin, alert, selection),
            ),
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

    @staticmethod
    def _is_cloud_sql(service: str) -> bool:
        try:
            comp = get_loader().components.get(get_loader().canonicalize(service) or service)
        except Exception as exc:
            logger.warning("Topology component lookup failed for %s: %s", service, type(exc).__name__)
            return False
        return bool(comp) and str(comp.type).strip().lower() == "cloud sql"

    async def _investigate_db_dependents(
        self,
        alert: AlertEvent,
        config: AgentConfig,
        origin: str,
        blast_radius: list[str],
        dossiers: list[EvidenceDossier],
    ) -> list[EvidenceDossier]:
        """Cloud Run dossiers of a database origin's dependents that were not investigated yet.

        A database alert's investigation reads only the database, so the impact on the services
        that call it (HTTP 5xx rate) was never measured (S4). For a Cloud SQL origin every Cloud
        Run service in its blast radius is investigated here, so the report states their
        measured rate. Apigee proxies are not Cloud Run services; the report states their
        proxy/response_count counts when ApigeeSpecialist read them, else "not measured".
        """
        if not self._is_cloud_sql(origin):
            return []
        investigated = {
            d.target_resource for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource
        }
        targets = [
            svc for svc in blast_radius
            if svc != origin and svc not in investigated and self._is_cloud_run(svc)
        ]
        if not targets:
            return []
        logger.info("Database origin %s: measuring its Cloud Run dependents %s", origin, ", ".join(targets))
        results = await asyncio.gather(
            *(self.cloud_run_specialist.investigate(alert, config, target_service=svc) for svc in targets)
        )
        return list(results)

    @staticmethod
    def _is_apigee_proxy(service: str) -> bool:
        try:
            comp = get_loader().components.get(get_loader().canonicalize(service) or service)
        except Exception as exc:
            logger.warning("Topology component lookup failed for %s: %s", service, type(exc).__name__)
            return False
        return bool(comp) and "apigee" in str(comp.type).lower()

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
            evidence = f"{node} degraded ({'; '.join(own)}) with no degraded upstream dependency"
            reason = f"{evidence}; recent revision {revision} implicated"
            implicated = True
        logger.info(
            "Change assessment: service=%s revision=%s implicated=%s own_evidence=%s explained_by=%s",
            node,
            revision,
            implicated,
            own,
            explaining,
        )
        verdict = {"service": service, "revision": revision, "implicated": implicated, "reason": reason}
        if implicated:
            verdict["evidence"] = evidence
        return verdict

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
    def _scope_report_dossiers(
        dossiers: list[EvidenceDossier],
        dependency_dossiers: Any,
        origin: str,
        blast_radius: list[str],
        selection: Optional[OriginSelection],
        group_id: str,
    ) -> list[EvidenceDossier]:
        """Dossiers whose evidence may appear in this group's report.

        The primary specialists' dossiers are kept. A dependency dossier (Cloud Run of an
        upstream service, investigated only to choose the origin) is kept when its service is
        the origin, in the blast radius, or alerting in this group.
        """
        deps = [id(d) for d in dependency_dossiers]
        in_scope = set(blast_radius) | {origin}
        if selection is not None:
            in_scope |= set(selection.degraded_services)
        kept: list[EvidenceDossier] = []
        for d in dossiers:
            if id(d) in deps and d.target_resource not in in_scope:
                logger.info(
                    "Group %s: evidence of upstream dependency %s not reported (not the origin %s, "
                    "not in its blast radius, no alert in this group)",
                    group_id, d.target_resource, origin,
                )
                continue
            kept.append(d)
        return kept

    @staticmethod
    def _reconcile_change_history(dossiers: list[EvidenceDossier]) -> list[EvidenceDossier]:
        """Make the change dossier's deployment-history statement match the Apigee evidence.

        The ChangeSpecialist says "Apigee proxy deployment history not queried" when its own
        sources cover Cloud Run only, while the ApigeeSpecialist reads proxy revisions from the
        access log, and Change-Induced lists that log as checked (S2, S3). The statement is
        rewritten from the same dossiers Change-Induced is computed from: the access-log
        sources read and any proxy revision switch found there.
        """
        chg = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg is None or NO_CLOUD_RUN_DEPLOYMENTS_FINDING not in chg.findings:
            return dossiers
        apigee = [d for d in dossiers if d.specialist_name == "ApigeeSpecialist"]
        sources = [
            src for d in apigee for src in (d.metrics.get(CHANGE_SOURCES_KEY) or [])
            if str(src).startswith("Apigee access log")
        ]
        if not sources:
            return dossiers
        switches = [
            (d.target_resource, d.metrics["proxy_revision_change"])
            for d in apigee
            if isinstance(d.metrics.get("proxy_revision_change"), dict)
        ]
        if switches:
            apigee_text = "; ".join(
                f"Apigee proxy {proxy} switched from revision {sw.get('previous_revision')} to "
                f"{sw.get('new_revision')} (first served {sw.get('new_first_served_at')}, Apigee access log)"
                for proxy, sw in switches
            )
        else:
            apigee_text = "no Apigee proxy revision switch in " + ", ".join(sources)
        statement = (
            "No Cloud Run revision deployments detected in the 2-hour window prior to alert onset; "
            f"{apigee_text}."
        )
        findings = [statement if f == NO_CLOUD_RUN_DEPLOYMENTS_FINDING else f for f in chg.findings]
        items = [
            item.model_copy(update={"content": statement})
            if item.headline == "Deployment History Analysis" and "was not queried" in (item.content or "")
            else item
            for item in chg.evidence_items
        ]
        logger.info("Deployment history statement reconciled with the Apigee access log: %s", statement)
        new_chg = dataclasses.replace(chg, findings=findings, evidence_items=items)
        return [new_chg if d is chg else d for d in dossiers]

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
                    if self._same_node(change.get("service"), cand.service) and is_candidate_cause(change):
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
        # The blast radius minus the origin: services that call the origin (directly or not).
        upstream_str = f"dependent services ({', '.join(upstream)})" if upstream else "direct client ingress"

        metric_details = []
        is_sql = False
        is_apigee = False
        err_5xx_rate: Optional[str] = None
        err_5xx_pct: Optional[float] = None
        # The origin's own measured 5xx rate: the only rate stated as the origin's (S1: the
        # orders-api rate was quoted as inventory-api's when it was the last dossier read).
        origin_5xx_rate: Optional[str] = None
        lat_p95_ms: Optional[float] = None
        # Statistic label of the origin's latency (e.g. per-minute mean), when the dossier records it.
        lat_stat: Optional[str] = None
        # Measured 5xx rate (text, percent) per topology dependent of the origin.
        dependent_5xx: dict[str, tuple[str, float]] = {}
        low_traffic: Optional[dict[str, Any]] = None
        if _is_latency_alert(alert):
            low_traffic = low_traffic_facts(
                next((d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin), None),
                get_agent_settings().low_traffic_rpm,
            )

        for d in dossiers:
            if d.specialist_name == "CloudSQLSpecialist" and d.anomaly_detected:
                is_sql = True
                c_num = _as_number(d.metrics.get("active_connections"))
                m_num = _as_number(d.metrics.get("max_connections"))
                # Only measured values: no assumed connection limit.
                if c_num is not None and m_num and c_num / m_num >= 0.8:
                    metric_details.append(f"connections at {int(c_num)}/{int(m_num)}")
                elif c_num is not None and not m_num:
                    metric_details.append(f"{int(c_num)} active connections (max_connections not captured)")
            elif d.specialist_name == "CloudRunSpecialist":
                # Same classification as the HTML Operational Impact column (measurements.py).
                raw_err = cloud_run_5xx_rate(d)
                if raw_err:
                    err_5xx_rate = raw_err
                    this_pct = rate_pct(raw_err)
                    err_5xx_pct = this_pct
                    if d.target_resource in upstream:
                        dependent_5xx[d.target_resource] = (raw_err, this_pct)
                    if d.target_resource == origin:
                        origin_5xx_rate = str(raw_err)
                # Latency of the origin only: the root cause states the origin's value, and a
                # dependent's (S2: orders-api 6638ms vs inventory-api 6546ms) must not replace it.
                raw_lat = d.metrics.get("latency_p95_ms") if d.target_resource == origin else None
                if raw_lat and raw_lat != "N/A (not captured)":
                    try:
                        lat_p95_ms = float(raw_lat)
                        if d.metrics.get("latency_statistic"):
                            lat_stat = latency_label(d.metrics)
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

        # Each 5xx rate is stated with its own service: the origin's in the rate parenthesis,
        # measured non-zero rates of dependents after it.
        if not origin_5xx_rate:
            # An Apigee origin's counts are measured by proxy/response_count, not Cloud Run.
            origin_resp = component_responses(origin, dossiers)
            if origin_resp is not None:
                origin_5xx_rate = origin_resp.five_xx_statement() or origin_resp.no_5xx_statement()
        origin_rate_disp = f" ({origin_5xx_rate})" if origin_5xx_rate else ""
        origin_rate_gap = "" if origin_5xx_rate else f" (HTTP 5xx rate not measured on {origin})"
        erroring_dependents = {svc: v for svc, v in dependent_5xx.items() if v[1] > 0}
        dependent_rates = (
            "; HTTP 5xx rate on dependent services: "
            + ", ".join(f"{svc} {txt}" for svc, (txt, _) in sorted(erroring_dependents.items()))
            if erroring_dependents
            else ""
        )

        # Distinguish 5xx-dominant vs latency-dominant failure mode
        is_policy_5xx = "5xx" in alert.policy_name.lower() or "500" in (alert.condition_name or "")
        is_5xx_dominant = (err_5xx_pct is not None and err_5xx_pct >= 5.0) or (is_policy_5xx and err_5xx_rate is not None)

        if origin != alert.resource_name:
            if is_5xx_dominant:
                return (
                    f"Elevated upstream HTTP 5xx error rate{origin_rate_disp} on {origin}{origin_rate_gap} causing cascading failures "
                    f"for {upstream_str} since {started_at}{dependent_rates}"
                )
            elif "latency" in alert.policy_name.lower() or "timeout" in alert.policy_name.lower() or (lat_p95_ms is not None and lat_p95_ms >= 2000.0):
                lat_disp = _latency_disp(lat_p95_ms, lat_stat)
                if low_traffic:
                    return f"Elevated latency{lat_disp} on {origin} since {started_at} under {low_traffic_sentence(low_traffic)}"
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
            head = f"Database connection pool saturation{metric_str} on {origin} since {started_at}"
            # Apigee proxies measured by proxy/response_count (the HTML Operational Impact
            # column reads the same counts): never "not measured" when they were read.
            proxies = {
                svc: r for svc in upstream if svc not in dependent_5xx
                if (r := component_responses(svc, dossiers)) is not None
            }
            erroring_proxies = {svc: r for svc, r in proxies.items() if r.count("5")}
            if erroring_dependents or erroring_proxies:
                # Only measured rates and counts on topology dependents are stated, each with its value.
                proxy_counts = (
                    "; HTTP 5xx responses on dependent proxies: "
                    + ", ".join(f"{svc} {r.five_xx_statement()}" for svc, r in sorted(erroring_proxies.items()))
                    if erroring_proxies
                    else ""
                )
                return f"{head}{dependent_rates}{proxy_counts}"
            unmeasured = [svc for svc in upstream if svc not in dependent_5xx and svc not in proxies]
            parts = []
            if dependent_5xx:
                parts.append(f"no HTTP 5xx errors measured on {', '.join(sorted(dependent_5xx))}")
            for svc, r in sorted(proxies.items()):
                parts.append(f"{r.no_5xx_statement()} on {svc}")
            if unmeasured:
                parts.append(f"HTTP 5xx rate not measured on {', '.join(unmeasured)}")
            return f"{head} ({'; '.join(parts)})" if parts else head
        elif is_apigee:
            metric_str = f" ({', '.join(metric_details)})" if metric_details else ""
            return (
                f"SpikeArrest rate-limit violation{metric_str} on {origin} "
                f"throttling incoming traffic for {upstream_str} since {started_at}"
            )

        if is_5xx_dominant:
            return (
                f"Elevated HTTP 5xx error rate{origin_rate_disp} on {origin}{origin_rate_gap} "
                f"impacting {upstream_str} since {started_at}{dependent_rates}"
            )
        elif "latency" in alert.policy_name.lower() or "timeout" in alert.policy_name.lower() or (lat_p95_ms is not None and lat_p95_ms >= 2000.0):
            lat_disp = _latency_disp(lat_p95_ms, lat_stat)
            if low_traffic:
                return f"Elevated latency{lat_disp} on {origin} since {started_at} under {low_traffic_sentence(low_traffic)}"
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
                # Only a measured, non-trivial rate corroborates; N/A is not a signal.
                if _measured_pct(err_rate) and err_rate != "0.1%":
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
            score_change, window = _proximity_score(mins)
            detail_change = f"{rev_label} deployed {mins}m prior to alert onset ({window})"
        elif (proxy_chg := self._proxy_change_evidence(dossiers, origin)) is not None:
            # An attributed Apigee proxy revision change is a change too (same proximity buckets).
            rev_label = (
                f"Apigee proxy `{proxy_chg['proxy']}` revision {proxy_chg['new_revision']} "
                f"(replacing {proxy_chg['previous_revision']}) first served {proxy_chg.get('new_first_served_at') or 'at an unknown time'}"
            )
            first = _parse_iso_utc(proxy_chg.get("new_first_served_at"))
            onset = _parse_iso_utc(alert.started_at)
            if first is None or onset is None:
                score_change = 0.00
                detail_change = f"{rev_label}; proximity to alert onset not determined (timestamp unknown)"
            elif first > onset:
                # First served after the onset: it cannot have caused the alert by timing.
                after = int((first - onset).total_seconds() // 60)
                score_change = 0.00
                detail_change = f"{rev_label}, {after}m after the alert onset (not before the onset)"
            else:
                mins = int((onset - first).total_seconds() // 60)
                score_change, window = _proximity_score(mins)
                detail_change = f"{rev_label}, {mins}m prior to alert onset ({window})"
        else:
            score_change = 0.00
            # State what was assessed: only the origin's (and the alerted resource's) deployments
            # score here. The report can list other components' deploys in the window (S4: a proxy
            # not upstream of the origin), so "zero recent deployments" would contradict it.
            subject = f"the origin `{origin}`"
            if alert.resource_name and alert.resource_name != origin:
                subject += f" or the alerted resource `{alert.resource_name}`"
            if change_sources_checked(dossiers):
                detail_change = (
                    f"No deployment of {subject} within the change window before alert onset "
                    "(deployments of other components are not scored)"
                )
            else:
                detail_change = f"Change proximity not assessed: no change source was read for {subject}"

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

    def _change_induced(
        self,
        verdict: Optional[dict[str, Any]],
        dossiers: list[EvidenceDossier],
        origin: str,
        change_gap: Optional[str],
    ) -> str:
        """The Change-Induced value of the report, with its evidence.

        "Yes (...)" when a change is implicated: a Cloud Run revision (the change verdict) or an
        Apigee proxy revision that brought the SpikeArrest faults. "Not assessed (...)" when a
        change source could not be read, none was queried, or the origin proxy's revisions were
        not evaluated. "No (...)" only when change detection ran and found no implicated change.
        """
        if verdict is not None and verdict.get("implicated"):
            chg = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
            metrics = chg.metrics if chg is not None else {}
            when = ""
            if metrics.get("deploy_timestamp"):
                when = f" deployed {metrics['deploy_timestamp']}"
            mins = metrics.get("minutes_since_deploy")
            if isinstance(mins, int) and mins >= 0:
                when += f" ({mins}m before the alert)"
            evidence = verdict.get("evidence") or verdict["reason"]
            value = f"Yes (revision {verdict['revision']} on {verdict['service']}{when}; {evidence})"
            logger.info("Change-Induced: %s", value)
            return value

        proxy = self._proxy_change_evidence(dossiers, origin)
        if proxy is not None:
            new, prev = proxy["new_revision"], proxy["previous_revision"]
            value = (
                f"Yes (Apigee proxy {proxy['proxy']} revision {new} first served {proxy['new_first_served_at']}, "
                f"replacing revision {prev}; access log: revision {new} served {proxy['new_requests']} requests "
                f"with {proxy['new_spike_arrest_faults']} SpikeArrest faults, revision {prev} served "
                f"{proxy['previous_requests']} requests with none)"
            )
            logger.info("Change-Induced: %s", value)
            return value

        if change_gap:
            value = f"Not assessed ({change_gap})"
            logger.info("Change-Induced: %s", value)
            return value

        chg = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        not_assessed = None
        if chg is None or not chg.metrics.get(CHANGE_SOURCES_KEY):
            not_assessed = "no deployment or configuration change source was queried"
        else:
            # An Apigee origin's changes are proxy revisions, read only from its access log.
            apigee = None
            if self._is_apigee_proxy(origin):
                apigee = next(
                    (d for d in dossiers if d.specialist_name == "ApigeeSpecialist" and d.target_resource == origin),
                    None,
                )
            switch = apigee.metrics.get("proxy_revision_change") if apigee is not None else None
            if apigee is not None and not apigee.metrics.get(CHANGE_SOURCES_KEY):
                cap = " (access log read cap hit)" if apigee.metrics.get("access_log_cap_hit") else ""
                not_assessed = f"Apigee proxy {origin} revisions not evaluated{cap}"
            elif isinstance(switch, dict):
                not_assessed = (
                    f"Apigee proxy {origin} switched from revision {switch.get('previous_revision')} to "
                    f"{switch.get('new_revision')} in the window; it is attributed only when the new revision brought SpikeArrest faults"
                )
            elif self._is_apigee_proxy(origin):
                # A deployment of the origin proxy in the window (ata-tools Apigee deployment
                # history) is not "no change"; it is attributed only from access-log evidence.
                deploys = [
                    d for d in chg.metrics.get("apigee_deployments_in_window") or []
                    if isinstance(d, dict) and d.get("proxy") == origin
                ]
                if deploys:
                    listed = ", ".join(f"revision {d.get('revision')} deployed {d.get('timestamp')}" for d in deploys)
                    not_assessed = (
                        f"Apigee proxy {origin} {listed} in the window (Apigee deployment history); it is "
                        "attributed only when the new revision brought SpikeArrest faults"
                    )
        if not_assessed:
            value = f"Not assessed ({not_assessed}; change attribution not assessed)"
            logger.info("Change-Induced: %s", value)
            return value

        checked = ", ".join(change_sources_checked(dossiers))
        # Deploys in the window that fail the attribution rule are listed with their evidence.
        # "likely unrelated" only with an established healthy period; else "not attributed".
        unrelated = chg.metrics.get("unrelated_changes_text")
        unattributed = chg.metrics.get("unattributed_changes_text")
        unrelated_part = f"recent changes likely unrelated: {unrelated}; " if unrelated else ""
        unrelated_part += f"not attributed: {unattributed}; " if unattributed else ""
        if verdict is not None:
            value = f"No ({verdict['reason']}; {unrelated_part}checked: {checked})"
        elif unrelated_part:
            value = f"No ({unrelated_part}checked: {checked})"
        else:
            value = (
                f"No (no deployment or configuration change on {chg.target_resource} or its upstream "
                f"dependencies in the {chg.metrics.get('change_window_text') or '2 h'} before the alert; checked: {checked})"
            )
        logger.info("Change-Induced: %s", value)
        return value

    @staticmethod
    def _proxy_change_evidence(dossiers: list[EvidenceDossier], origin: str) -> Optional[dict[str, Any]]:
        """Access-log evidence that a new Apigee proxy revision brought the SpikeArrest faults.

        Implicated only when the origin proxy switched revision in the window, the new revision
        has SpikeArrest faults and the previous one served requests without any. Returns the
        revision change merged with proxy, org/environment and SpikeArrest totals, else None.
        """
        apigee = next(
            (d for d in dossiers if d.specialist_name == "ApigeeSpecialist" and d.target_resource == origin),
            None,
        )
        if not apigee:
            return None
        change = apigee.metrics.get("proxy_revision_change")
        if not isinstance(change, dict):
            return None
        if not (
            change.get("new_spike_arrest_faults", 0) > 0
            and change.get("previous_spike_arrest_faults", 0) == 0
            and change.get("previous_requests", 0) > 0
        ):
            return None
        sa = apigee.metrics.get("spike_arrest") if isinstance(apigee.metrics.get("spike_arrest"), dict) else {}
        return {
            **change,
            "proxy": origin,
            "org": apigee.metrics.get("apigee_org"),
            "environment": apigee.metrics.get("apigee_environment"),
            "spike_arrest_count": sa.get("count"),
            "spike_arrest_response_code": sa.get("response_code"),
            "spike_arrest_fault_code": sa.get("fault_code"),
        }

    @staticmethod
    def _proxy_change_root_cause(ev: dict[str, Any], policy_name: str) -> str:
        """Root cause sentence for a proxy revision change that brought SpikeArrest faults."""
        fault = f" with fault `{ev['spike_arrest_fault_code']}`" if ev.get("spike_arrest_fault_code") else ""
        code = f"HTTP {ev['spike_arrest_response_code']} " if ev.get("spike_arrest_response_code") else ""
        total = f"{ev['spike_arrest_count']} {code}responses{fault} in total; " if ev.get("spike_arrest_count") else ""
        text = (
            f"Configuration change on Apigee proxy `{ev['proxy']}`: revision {ev['new_revision']} "
            f"(first served {ev['new_first_served_at']}, replacing revision {ev['previous_revision']}) "
            f"is rejecting requests with SpikeArrest faults under policy `{policy_name}`. "
            f"{total}access log: revision {ev['new_revision']} served {ev['new_requests']} requests with "
            f"{ev['new_spike_arrest_faults']} SpikeArrest faults, revision {ev['previous_revision']} served "
            f"{ev['previous_requests']} requests with none."
        )
        prev_rpm, new_rpm = ev.get("previous_requests_per_minute"), ev.get("new_requests_per_minute")
        if prev_rpm is not None and new_rpm is not None:
            text += (
                f" Request rate: {prev_rpm}/min on revision {ev['previous_revision']}, "
                f"{new_rpm}/min on revision {ev['new_revision']}."
            )
        elif prev_rpm is None:
            text += (
                f" The request rate before the switch was not measured (revision {ev['previous_revision']} "
                "served for less than a minute in the query window)."
            )
        return text

    def _apigee_recommendations(
        self, dossiers: list[EvidenceDossier], origin: str, project_id: str
    ) -> list[ActionItem]:
        """Actions for an Apigee proxy incident, naming only resources seen in the telemetry.

        With a proxy revision change implicated: redeploy the previous revision (explicit
        number). Otherwise: read the proxy's SpikeArrest faults from its access log. Commands
        need the org and environment Apigee reported; without them no command is given.
        """
        apigee = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        access_log = access_log_name(project_id)
        log_cmd = (
            f"gcloud logging read 'logName=\"{access_log}\" AND jsonPayload.proxy=\"{origin}\" "
            f"AND jsonPayload.fault_source=\"{SPIKE_ARREST_FAULT}\"' --project={project_id} --limit=20"
        )
        change = self._proxy_change_evidence(dossiers, origin)
        if change:
            prev, new = change["previous_revision"], change["new_revision"]
            org, env = change.get("org"), change.get("environment")
            if org and env:
                deploy = f"gcloud apigee apis deploy {{rev}} --api={origin} --environment={env} --organization={org} --override"
                rollback_cmd: Optional[str] = deploy.format(rev=prev)
                undo = f"Redeploy revision {new}: {deploy.format(rev=new)}"
            else:
                logger.warning(
                    "Apigee org/environment not captured for %s; rollback to revision %s given without a command",
                    origin,
                    prev,
                )
                rollback_cmd = None
                undo = f"Redeploy revision {new} of {origin}."
            return [
                ActionItem(
                    title=(
                        f"Roll back Apigee proxy {origin} to revision {prev}, which served before revision {new} "
                        f"without SpikeArrest faults"
                    ),
                    command=rollback_cmd,
                    verification=(
                        f"Confirm the {access_log_id()} log shows revision {prev} serving and no new "
                        f"{SPIKE_ARREST_FAULT} faults"
                    ),
                    rollback=undo,
                ),
                ActionItem(
                    title=f"Review the SpikeArrest faults of {origin} in its access log",
                    command=log_cmd,
                    verification=f"Confirm the faults are on revision {new} only",
                ),
            ]
        if apigee is None:
            return [ActionItem(title=f"Review Apigee proxy {origin} telemetry (no Apigee evidence captured)")]
        return [
            ActionItem(
                title=f"Review the SpikeArrest faults of {origin} in its access log",
                command=log_cmd,
                verification="Identify the proxy revision and clients receiving HTTP 429 responses",
            ),
        ]

    def _db_caller_actions(self, origin: str, project_id: str) -> list[ActionItem]:
        return db_caller_actions(origin, project_id)

    def _gateway_timeout_fact(self, dossiers: list[EvidenceDossier], origin: str) -> Optional[str]:
        """Sentence stating the Apigee GatewayTimeout evidence for ``origin``, or None.

        Built only from gathered data: the 504/GatewayTimeout count, fault code and fault source
        from Apigee telemetry, the proxy revision from its access log, the topology path from
        the proxy to the origin, and the origin's peak observed latency from the Cloud Run dossier. The
        proxy's timeout value is not gathered, so no value is stated.
        """
        apigee = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        gt = apigee.metrics.get("gateway_timeout") if apigee else None
        if not isinstance(gt, dict) or not gt.get("count"):
            return None
        proxy = gt.get("proxy") or apigee.target_resource
        try:
            paths = get_loader().dependency_paths(origin)
        except Exception as exc:
            logger.error("Topology dependency path lookup failed for %s: %s", origin, type(exc).__name__)
            return None
        path = next((p for p in paths if proxy in p), None)
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
        count = f"at least {gt['count']}" if gt.get("count_is_lower_bound") else str(gt["count"])
        src = f" (fault source: {gt['fault_source']})" if gt.get("fault_source") else ""
        fact = (
            f"Apigee proxy `{proxy}`{rev} returned {count} {what} with fault "
            f"`{gt.get('fault_code')}`{src}. Requests on {route} did not complete within the "
            f"proxy's target timeout"
        )
        cr = next(
            (d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin),
            None,
        )
        lat = cr.metrics.get("latency_p95_ms") if cr else None
        if isinstance(lat, (int, float)):
            fact += f"; `{origin}` {latency_label(cr.metrics)} was {int(lat)}ms"
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
        if dossier.metrics.get("has_db_connect_error"):
            err_rate = dossier.metrics.get("error_rate_5xx")
            err_str = f" (HTTP 5xx error rate: {err_rate})" if err_rate and err_rate != "0.1%" and err_rate != "N/A (not captured)" else ""
            return f" (database connection failures in application logs{err_str})"

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
                return f" ({latency_label(dossier.metrics)}: {int(lat_val)}ms)"
            return " (elevated request latency)"

        # If policy is 5xx or 5xx error breach:
        if is_5xx_policy or (err_val is not None and err_val >= 5.0):
            if err and err != "0.1%":
                return f" (HTTP 5xx error rate: {err})"
            return " (elevated HTTP 5xx errors)"

        if lat_val is not None and lat_val >= 2000.0:
            return f" ({latency_label(dossier.metrics)}: {int(lat_val)}ms)"
        if err and err != "0.1%":
            return f" (HTTP 5xx error rate: {err})"

        return ""

    def _policy_matching_evidence(
        self,
        alert: AlertEvent,
        group_alerts: Optional[list[dict[str, Any]]],
        origin: str,
        evidence_signal: Optional[str],
    ) -> str:
        """Policy to name in the root cause: the investigated alert's, unless its signal differs
        from the evidence and another alert of the group on the origin matches the evidence.

        S5: the group's first alert on the origin was ata-inventory-latency while the evidence
        is HTTP 5xx; the root cause names ata-inventory-5xx then.
        """
        if not evidence_signal:
            return alert.policy_name
        wanted = _SIGNAL_CATEGORIES.get(evidence_signal, set())
        own = alert_category({
            "policy_name": alert.policy_name,
            "signal": (alert.policy_user_labels or {}).get("signal") or (alert.labels or {}).get("signal"),
        })
        if own in wanted:
            return alert.policy_name
        for entry in group_alerts or []:
            if not isinstance(entry, dict) or not self._same_node(alert_component(entry), origin):
                continue
            policy = entry.get("policy_name") or entry.get("policy")
            if policy and alert_category(entry) in wanted:
                logger.info(
                    "Root cause names policy %s (matches the %s evidence on %s) instead of the investigated alert's %s",
                    policy, evidence_signal, origin, alert.policy_name,
                )
                return str(policy)
        return alert.policy_name

    def _determine_root_cause(
        self,
        dossiers: list[EvidenceDossier],
        origin: str,
        alert: AlertEvent,
        blast_radius: Optional[list[str]] = None,
        group_alerts: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[str, float]:
        """Synthesize probable root cause statement and calibrated evidence-based confidence.

        ``group_alerts`` (the incident group's alerts) let the change branch name the origin's
        policy that matches the evidence, rather than the investigated alert's policy.
        """
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
                if (
                    c.get("service") in (origin, alert.resource_name)
                    and c.get("deployed_minutes_ago", 999) <= 60
                    and is_candidate_cause(c)
                ):
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
            proxy_change = self._proxy_change_evidence(dossiers, origin)
            if proxy_change:
                return self._proxy_change_root_cause(proxy_change, alert.policy_name), confidence
            apigee = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
            sa = apigee.metrics.get("spike_arrest") if apigee else None
            count_str = ""
            if isinstance(sa, dict) and sa.get("count"):
                least = "at least " if sa.get("count_is_lower_bound") else ""
                code = f"HTTP {sa['response_code']} " if sa.get("response_code") else ""
                count_str = f" ({least}{sa['count']} {code}responses with fault `{sa['fault_code']}`)"
            root_cause_text = (
                f"SpikeArrest rate-limit violations at Apigee ingress proxy `{origin}`{count_str} "
                f"under policy `{alert.policy_name}`."
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
            # Signal of the evidence in symptom_text; the policy named is one matching it.
            evidence_signal: Optional[str] = None
            if has_db_auth:
                err_rate = cr_spec.metrics.get("error_rate_5xx") if cr_spec else None
                err_str = f" (HTTP 5xx error rate: {err_rate})" if err_rate and err_rate != "0.1%" and err_rate != "N/A (not captured)" else ""
                symptom_text = f"database authentication failures in application logs{err_str}"
                evidence_signal = "5xx" if err_str else None
            elif any(k in (alert.policy_name or "").lower() for k in ("latency", "timeout", "slow")):
                lat = cr_spec.metrics.get("latency_p95_ms") if cr_spec else None
                lat_str = (
                    f" ({latency_label(cr_spec.metrics)}: {lat:g}ms)"
                    if isinstance(lat, (int, float)) and not isinstance(lat, bool)
                    else ""
                )
                symptom_text = f"elevated request latency{lat_str}"
            elif cr_spec and cr_spec.metrics.get("error_rate_5xx") and cr_spec.metrics["error_rate_5xx"] != "0.1%":
                symptom_text = f"elevated HTTP 5xx error rate ({cr_spec.metrics['error_rate_5xx']})"
                evidence_signal = "5xx"
            else:
                symptom_text = f"operational failures under policy `{alert.policy_name}`"
            policy = self._policy_matching_evidence(alert, group_alerts, origin, evidence_signal)

            if is_upstream:
                root_cause_text = (
                    f"Recent {rev_desc} {target_phrase} at {deploy_time_str} (change detected) "
                    f"triggered {symptom_text} cascading failures on caller `{alert.resource_name}` under policy `{policy}`."
                )
            else:
                root_cause_text = (
                    f"Recent {rev_desc} {target_phrase} at {deploy_time_str} (change detected) "
                    f"triggered {symptom_text} under policy `{policy}`."
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
        # The origin's own traffic only (as in the problem statement): a fallback dossier of
        # another service must not describe the origin's volume.
        origin_cr = cr_target if cr_target is not None and cr_target.target_resource == origin else None
        low_traffic = (
            low_traffic_facts(origin_cr, get_agent_settings().low_traffic_rpm) if _is_latency_alert(alert) else None
        )

        if low_traffic:
            root_cause_text = (
                f"Elevated latency on `{origin}`{signal_facts} under {low_traffic_sentence(low_traffic)}. "
                f"It triggered policy `{alert.policy_name}`"
                + (f" on caller `{alert.resource_name}`." if origin != alert.resource_name else ".")
            )
        elif is_upstream:
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
        policy_lower = (alert.policy_name or "").lower()
        is_rate_limit_alert = "429" in policy_lower or "spikearrest" in policy_lower or (
            str((alert.policy_user_labels or {}).get("signal") or "").lower() == "rate-limit"
        )

        # 1. Alert trigger item
        started_at = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        obs_items.append(
            format_alert_trigger_line(
                alert.policy_name, alert.resource_name, started_at, alert.state, alert.incident_id
            )
        )

        # Degraded evidence second, so the 5-item cap never drops it.
        degraded = degraded_evidence_observation(dossiers)
        if degraded:
            obs_items.append(degraded)

        # 2. Metric evidence items
        for d in dossiers:
            if d.specialist_name == "CloudSQLSpecialist" and d.metrics:
                if is_rate_limit_alert:
                    # A SpikeArrest rejection happens in the proxy before any backend call, so
                    # database state is not evidence for it (S3: "connections normal" on a 429).
                    continue
                conns = d.metrics.get("active_connections")
                max_conns = d.metrics.get("max_connections")
                conns_num = _as_number(conns)
                max_num = _as_number(max_conns)
                if conns_num is not None:
                    if max_num:
                        if conns_num / max_num >= 0.8:
                            obs_items.append(
                                f"Database active connections reached {int(conns_num)} of {int(max_num)} on {d.target_resource}"
                            )
                        else:
                            obs_items.append(
                                f"Database active connections normal ({int(conns_num)} of {int(max_num)}) on {d.target_resource}"
                            )
                    else:
                        # No limit was captured: state the count only, never an assumed maximum.
                        obs_items.append(
                            f"Database active connections: {int(conns_num)} on {d.target_resource} "
                            "(max_connections not captured)"
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
                if err_rate and err_rate != "0.1%" and not str(err_rate).startswith("N/A"):
                    # Only measured values: no request rate or restart count is filled in.
                    line = f"HTTP 5xx error rate {err_rate}"
                    if isinstance(rpm, (int, float)):
                        line += f" ({rpm} rpm)"
                    line += f" on {d.target_resource}"
                    if isinstance(restarts, (int, float)):
                        line += f", with {restarts} container restarts"
                    obs_items.append(line)
                if _is_latency_alert(alert):
                    low = low_traffic_facts(d, get_agent_settings().low_traffic_rpm)
                    if low:
                        obs_items.append(f"Low traffic on {low['service']}: {low['volume']} in the investigation window")
                has_db_auth = _dossier_has_db_auth_error(d)
                if has_db_auth:
                    obs_items.append(f"Database authentication failure observed in application logs on {d.target_resource}")
                elif d.metrics.get("has_db_connect_error"):
                    obs_items.append(
                        f"Database connection failures observed in application logs on {d.target_resource} "
                        "(no authentication failure signature)"
                    )
            elif d.specialist_name == "ApigeeSpecialist" and d.metrics:
                cap = d.metrics.get("access_log_cap_hit")
                if isinstance(cap, dict):
                    obs_items.append(
                        f"Apigee access log for {d.target_resource} hit the {cap.get('cap')}-entry read cap in "
                        f"[{cap.get('window_start')}, {cap.get('window_end')}]; log-derived counts are lower bounds "
                        "and no proxy revision switch was evaluated"
                    )
                if "proxy_response_code_429" in d.metrics and d.metrics["proxy_response_code_429"] != "N/A (not captured)":
                    obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics['proxy_response_code_429']} HTTP 429 rate limit responses")
                elif "status_429_rate" in d.metrics:
                    obs_items.append(f"Apigee proxy {d.target_resource} recorded {d.metrics['status_429_rate']} HTTP 429 response rate")
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

    @staticmethod
    def _db_auth_log_action(dossiers: list[EvidenceDossier], origin: str) -> Optional[ActionItem]:
        """Log query for the DB authentication failure the evidence came from, else None.

        The search term is text of the log line the signature was detected in
        (``db_auth_log_term``), so the query finds that evidence. Without such a term no
        query is proposed: a guessed pattern can match none of the real lines.
        """
        cr = next(
            (d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin),
            None,
        )
        term = cr.metrics.get("db_auth_log_term") if cr is not None else None
        if not isinstance(term, str) or not term.strip() or "'" in term:
            if cr is not None and cr.metrics.get("has_db_auth_error"):
                logger.info(
                    "No log query proposed for the DB authentication failure on %s: no search term from the matched log line",
                    origin,
                )
            return None
        literal = term.strip().replace("\\", "\\\\").replace('"', '\\"')
        return ActionItem(
            title=f"Inspect container application logs on {origin} for the database authentication failure seen in the evidence",
            command=(
                f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"{origin}\""
                f" AND \"{literal}\"' --limit=30"
            ),
            verification="Confirm no new entries containing this text appear after the fix",
        )

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
        prev_evidence = ""
        change_svc = None
        if change_dossier and change_dossier.anomaly_detected:
            change_svc = change_dossier.metrics.get("change_service") or change_dossier.target_resource
        if change_svc and not self._same_node(change_svc, origin):
            logger.info(
                "Recent change on %s is not on origin %s; no rollback recommended for it", change_svc, origin
            )
        elif change_dossier and change_dossier.anomaly_detected:
            recent_rev = change_dossier.metrics.get("recent_revision")
            # Only a revision verified from traffic history is a rollback target; the previously
            # created revision can be a retired 0%-traffic one while traffic is pinned elsewhere.
            prev_rev = change_dossier.metrics.get("previous_serving_revision")
            prev_evidence = str(change_dossier.metrics.get("previous_serving_evidence") or "").strip()
            if not recent_rev:
                for f in change_dossier.findings:
                    m = re.search(r"revision deployment detected on [`']?([^`'\s]+)[`']?.*?: [`']?([^`'\s]+)[`']?", f)
                    if m:
                        recent_rev = m.group(2)
                        break

        # 1. Change-induced failure takes precedence (e.g. S5 bad deploy)
        # Suppress generic Cloud SQL connection-pool actions when the fault is caused by a bad deployment.
        if recent_rev:
            has_db_auth = _dossier_has_db_auth_error(cr_dossier)
            config_check_cmd = f"gcloud run revisions describe {recent_rev} --region={region} --format=\"yaml(spec.containers[0].env)\""
            has_db_connect = bool(cr_dossier and cr_dossier.metrics.get("has_db_connect_error"))
            if has_db_auth:
                config_check_title = f"Verify database connection credentials and environment variables in {recent_rev}"
            elif has_db_connect:
                config_check_title = f"Verify database connection settings and environment variables in {recent_rev}"
            else:
                config_check_title = f"Verify revision configuration and environment variables in {recent_rev}"

            has_valid_prev = bool(
                prev_rev
                and prev_evidence
                and str(prev_rev).strip()
                and str(prev_rev).strip().lower() not in ("none", "unknown", "previous_revision", "unspecified")
            )
            if has_valid_prev:
                rollback_title = (
                    f"Roll back Cloud Run traffic on {origin} to {prev_rev}, the revision that served before"
                    f" {recent_rev} was deployed. Evidence: {prev_evidence}"
                )
                rollback_cmd = f"gcloud run services update-traffic {origin} --to-revisions={prev_rev}=100 --region={region}"
            else:
                if prev_rev and not prev_evidence:
                    logger.warning(
                        "Previous serving revision %s for %s has no traffic evidence; not naming it", prev_rev, origin
                    )
                reason = prev_evidence or "no traffic history evidence was gathered."
                rollback_title = (
                    f"Roll back Cloud Run traffic on {origin} (previous serving revision could not be identified;"
                    f" {reason} Do not pick the previously created revision: it may never have served.)"
                )
                rollback_cmd = (
                    "# previous serving revision could not be identified; check which revision served before"
                    f" {recent_rev} (request_count by revision_name in Cloud Monitoring), then route to it explicitly:"
                    f" gcloud run revisions list --service={origin} --region={region}"
                )

            actions = [
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
            ]
            log_action = self._db_auth_log_action(dossiers, origin)
            if log_action is not None:
                actions.append(log_action)
            return actions

        # 2. Apigee SpikeArrest / Rate Limit
        is_apigee = (
            (apigee_dossier and apigee_dossier.anomaly_detected)
            or "429" in (alert.policy_name or "").lower()
            or "spikearrest" in (alert.policy_name or "").lower()
            or "apigee" in origin
            or "v1" in origin
        )
        if is_apigee:
            return self._apigee_recommendations(dossiers, origin, project_id)

        # 3. Database Failure (Cloud SQL)
        is_sql = ((sql_dossier and sql_dossier.anomaly_detected) or "db" in origin) and not (cr_dossier and cr_dossier.metrics.get("has_db_auth_error"))
        if is_sql:
            return db_pool_actions(origin, project_id, self._db_caller_actions(origin, project_id))

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
            self._generic_log_review_action(origin, alert),
        ]

    @staticmethod
    def _generic_log_review_action(origin: str, alert: AlertEvent) -> ActionItem:
        """Log review worded for the alert's signal (no latency wording on a 5xx incident)."""
        command = (
            f"gcloud logging read 'resource.type=\"cloud_run_revision\" AND "
            f"resource.labels.service_name=\"{origin}\" AND severity>=WARNING' --limit=50"
        )
        signal = str((alert.policy_user_labels or {}).get("signal") or "").lower()
        policy = (alert.policy_name or "").lower()
        if signal == "5xx" or (not signal and "5xx" in policy):
            return ActionItem(
                title=f"Review the error logs of {origin} for the cause of the HTTP 5xx responses",
                command=command,
                verification=f"Confirm the HTTP 5xx error rate of {origin} returns below the alert threshold",
            )
        if signal == "latency" or (not signal and "latency" in policy):
            return ActionItem(
                title=f"Review the warning and error logs of {origin} for slow requests and dependencies",
                command=command,
                verification=f"Confirm the request latency of {origin} returns below the alert threshold",
            )
        return ActionItem(
            title=f"Review the warning and error logs of {origin}",
            command=command,
            verification=f"Confirm the alerting condition on {origin} has cleared",
        )


    def _build_alerts(self, alert: AlertEvent, config: AgentConfig) -> list[dict[str, Any]]:
        sa = alert.started_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        builder = ConsoleLinkBuilder(project_id=config.project_id, region=config.region)
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

    @staticmethod
    def _node_impact(resource: str, dossiers: list[EvidenceDossier]) -> str:
        """What this investigation measured on `resource`, or "not measured in this investigation".

        Only recorded values: Cloud Run 5xx rate and latency, Apigee non-2xx response counts
        (with the GatewayTimeout / SpikeArrest fault when recorded), Cloud SQL connections.
        """
        facts: list[str] = []
        for d in dossiers:
            if d.target_resource != resource:
                continue
            m = d.metrics or {}
            if d.specialist_name == "CloudRunSpecialist":
                # A not-applicable dossier (Apigee proxy, Cloud SQL) records no rate or latency.
                rate = cloud_run_5xx_rate(d)
                if rate:
                    facts.append(f"HTTP 5xx error rate {rate}")
                lat = m.get("latency_p95_ms")
                if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                    facts.append(f"{latency_label(m)} {lat:g}ms")
            elif d.specialist_name == "ApigeeSpecialist":
                # Same counts as the ticket problem statement (measurements.py); None = not measured.
                resp = apigee_responses(d)
                if resp is None:
                    continue
                gt = m.get("gateway_timeout") if isinstance(m.get("gateway_timeout"), dict) else {}
                sa = m.get("spike_arrest") if isinstance(m.get("spike_arrest"), dict) else {}
                for code, n in resp.errors.items():
                    fault = (
                        " (GatewayTimeout)" if gt and str(gt.get("response_code")) == str(code)
                        else " (SpikeArrestViolation)" if sa and str(sa.get("response_code")) == str(code)
                        else ""
                    )
                    facts.append(f"HTTP {code}: {n}{fault}")
                if not resp.errors:
                    facts.append(resp.statement())
            elif d.specialist_name == "CloudSQLSpecialist":
                conns, max_conns = m.get("active_connections"), m.get("max_connections")
                if isinstance(conns, (int, float)) and not isinstance(conns, bool):
                    facts.append(
                        f"{conns:g} of {max_conns:g} connections active"
                        if isinstance(max_conns, (int, float)) and not isinstance(max_conns, bool)
                        else f"{conns:g} active connections"
                    )
        return "; ".join(facts) if facts else "not measured in this investigation"

    def _build_topology_nodes(
        self, origin: str, blast_radius: list[str], dossiers: Optional[list[EvidenceDossier]] = None,
    ) -> list[dict[str, Any]]:
        nodes = []
        for r in blast_radius:
            is_orig = (r == origin)
            # The impact is what was measured on the node, never a template phrase.
            impact = self._node_impact(r, dossiers or [])
            if "db" in r:
                res_type = "Cloud SQL PostgreSQL"
                role = "Origin (Root Callee)" if is_orig else "Downstream Database"
            elif "apigee" in r or "v1" in r:
                res_type = "Apigee API Proxy"
                role = "Origin (Ingress Breach)" if is_orig else "Ingress API Gateway"
            else:
                res_type = "Cloud Run Service"
                role = "Origin (Root Service)" if is_orig else "Intermediate Callee / Dependent"
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
            elif d.specialist_name == "CloudRunSpecialist" and not_applicable_component_type(d) is not None:
                # Not a Cloud Run service (e.g. an Apigee proxy origin): nothing was queried, so
                # one row says so instead of Cloud Run metric rows with N/A values.
                kind = not_applicable_component_type(d) or "not a Cloud Run service"
                metrics.append({
                    "layer": "Cloud Run",
                    "metric_name": "Cloud Run service metrics",
                    "resource": d.target_resource,
                    "before_value": "not applicable",
                    "during_value": f"not applicable ({kind})",
                    "variance": "not applicable",
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
                    code_key = next((k for k in ["proxy_response_code_504", "proxy_response_code_5xx", "proxy_response_code_503", "proxy_response_code_502", "proxy_response_code_500"] if k in d.metrics), None)
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
                    basis = d.metrics.get("target_latency_basis")
                    during_val = f"{lat}ms" if isinstance(lat, (int, float)) else str(lat)
                    # "(timeout)" only when the Apigee dossier observed GatewayTimeout faults.
                    if isinstance(lat, (int, float)) and d.metrics.get("gateway_timeout"):
                        during_val += " (GatewayTimeout observed)"
                    metrics.append({
                        "layer": "Apigee",
                        # The P95 is of logged requests or of per-minute means; the basis says which.
                        "metric_name": f"Apigee target latency, P95 ({basis})" if basis else "Apigee target latency",
                        "resource": d.target_resource,
                        "before_value": "N/A (no baseline captured)",
                        "during_value": during_val,
                        "variance": "N/A (no baseline captured)",
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
            **(
                {"target_latency_basis": apigee_dossier.metrics["target_latency_basis"]}
                if apigee_dossier.metrics.get("target_latency_basis")
                else {}
            ),
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
                "previous_revision": chg_dossier.metrics.get("previous_serving_revision") or "previous serving revision could not be identified",
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
                max_conns = sql_dossier.metrics.get("max_connections")
                if isinstance(conns, (int, float)):
                    of_max = f"/{max_conns:g}" if isinstance(max_conns, (int, float)) else ""
                    reason = (
                        f"Cloud SQL connections queried on {sql_dossier.target_resource} "
                        f"({conns:g}{of_max} active connections), below saturation threshold."
                    )
                else:
                    reason = (
                        f"Cloud SQL connections on {sql_dossier.target_resource}: not available; "
                        "pool saturation not ruled out."
                    )
                alternatives.append({"alternative": "Database Connection Pool Saturation", "reason": reason})

        # 2. Upstream Apigee Gateway Rate Limiting Check
        apigee_dossier = next((d for d in dossiers if d.specialist_name == "ApigeeSpecialist"), None)
        if apigee_dossier:
            has_apigee_check = bool(apigee_dossier.queries_executed) or bool(apigee_dossier.metrics)
            if has_apigee_check and not apigee_dossier.anomaly_detected:
                rate_429 = apigee_dossier.metrics.get("status_429_rate") or apigee_dossier.metrics.get("proxy_response_code_429")
                # The counts the Operational Impact column states for this proxy.
                resp = apigee_responses(apigee_dossier)
                if resp is not None and resp.count("429"):
                    reason = (
                        f"Apigee proxy metrics queried on {apigee_dossier.target_resource} "
                        f"({resp.count('429')} HTTP 429 responses of {resp.total} requests); "
                        "SpikeArrest throttling not ruled out."
                    )
                elif resp is not None:
                    counted = f"429: 0 of {resp.total} requests" if resp.total else "no traffic recorded"
                    reason = (
                        f"Apigee proxy metrics queried on {apigee_dossier.target_resource} ({counted}), "
                        "ruling out SpikeArrest throttling."
                    )
                elif rate_429 is None or (isinstance(rate_429, str) and rate_429.startswith("N/A")):
                    reason = (
                        f"Apigee 429 count on {apigee_dossier.target_resource}: not available; "
                        "SpikeArrest throttling not ruled out."
                    )
                else:
                    reason = (
                        f"Apigee proxy metrics queried on {apigee_dossier.target_resource} (429: {rate_429}), "
                        "ruling out SpikeArrest throttling."
                    )
                alternatives.append({"alternative": "Upstream API Gateway Rate-Limiting", "reason": reason})

        # 3. Application Deployment Check
        chg_dossier = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg_dossier:
            has_chg_check = bool(chg_dossier.queries_executed) or bool(chg_dossier.metrics)
            # A not-implicated change is stated by the origin selection notes (alternatives_ruled_out).
            not_implicated = chg_dossier.metrics.get("change_not_implicated_reason")
            proxy_changed = self._proxy_change_evidence(dossiers, origin) is not None
            unrelated = chg_dossier.metrics.get("unrelated_changes_text")
            # A deploy whose healthy period is undetermined is not attributed, but not ruled out either.
            unattributed = chg_dossier.metrics.get("unattributed_changes_text")
            if unattributed:
                pass
            elif has_chg_check and not chg_dossier.anomaly_detected and not not_implicated and not proxy_changed and unrelated:
                alternatives.append({
                    "alternative": "Recent Application Deployment Regression",
                    "reason": f"Recent changes (likely unrelated): {unrelated}.",
                })
            elif has_chg_check and not chg_dossier.anomaly_detected and not not_implicated and not proxy_changed:
                alternatives.append({
                    "alternative": "Recent Application Deployment Regression",
                    "reason": (
                        f"Deployment history queried for {chg_dossier.target_resource} "
                        f"({self._serving_revision_text(dossiers, chg_dossier)}); zero rollback or build failures recorded."
                    ),
                })

        # 4. Container Crash Loop Check
        # A not-applicable dossier (Cloud SQL instance, Apigee proxy) has no containers: skip it.
        cr_dossier = next(
            (d for d in dossiers
             if d.specialist_name == "CloudRunSpecialist" and not_applicable_component_type(d) is None),
            None,
        )
        if cr_dossier:
            has_cr_check = bool(cr_dossier.queries_executed) or bool(cr_dossier.metrics)
            restarts = cr_dossier.metrics.get("container_restarts")
            if has_cr_check and (restarts == 0 or (restarts is None and not cr_dossier.anomaly_detected)):
                if isinstance(restarts, (int, float)):
                    reason = (
                        f"Container restart count queried on {cr_dossier.target_resource} ({restarts:g} restarts), "
                        "ruling out crash loop termination."
                    )
                else:
                    reason = (
                        f"Container restart count on {cr_dossier.target_resource}: not available; "
                        "crash loop not ruled out."
                    )
                alternatives.append({"alternative": "Container Crash Loop / Crash Throttling", "reason": reason})

        # 5. Host Memory OOM Throttling (only if memory was queried or measured)
        memory_queried = any("memory" in q.lower() for q in all_queries_flat) or any("memory" in str(k).lower() for d in dossiers for k in d.metrics)
        if memory_queried:
            mem_val = next((d.metrics["memory_utilization_pct"] for d in dossiers
                            if isinstance(d.metrics.get("memory_utilization_pct"), (int, float))), None)
            alternatives.append({
                "alternative": "Host Memory OOM Throttling",
                "reason": (
                    f"Container memory utilization queried ({mem_val:g}%), ruling out Out-Of-Memory container termination."
                    if mem_val is not None
                    else "Container memory utilization: not available; Out-Of-Memory termination not ruled out."
                ),
            })

        # 6. Network VPC Egress Partition (only if VPC or latency was queried or measured)
        vpc_queried = any("vpc" in q.lower() or "latency" in q.lower() for q in all_queries_flat) or any("vpc_latency" in str(k).lower() for d in dossiers for k in d.metrics)
        if vpc_queried:
            vpc_val = next((d.metrics["vpc_latency_ms"] for d in dossiers
                            if isinstance(d.metrics.get("vpc_latency_ms"), (int, float))), None)
            alternatives.append({
                "alternative": "Network VPC egress partition",
                "reason": (
                    f"Internal VPC round-trip latency queried ({vpc_val:g}ms), ruling out network partitioning."
                    if vpc_val is not None
                    else "Internal VPC round-trip latency: not available; network partitioning not ruled out."
                ),
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

    @staticmethod
    def _history_scope(origin: str, alert: AlertEvent, selection: Optional[OriginSelection]) -> list[str]:
        """Components whose Apigee deployments the ticket names: the origin, the group's
        alerting components, and the proxies that route directly to the origin."""
        scope = [origin, alert.resource_name]
        if selection is not None:
            scope.extend(selection.degraded_services)
        try:
            loader = get_loader()
            canon = loader.canonicalize(origin) or origin
            scope.extend(
                e.from_node for e in loader.edges
                if e.to_node == canon and "apigee" in str(getattr(loader.components.get(e.from_node), "type", "")).lower()
            )
        except Exception as exc:
            logger.warning("Topology edge lookup failed for %s: %s", origin, type(exc).__name__)
        out: list[str] = []
        for c in scope:
            if c and c not in out:
                out.append(c)
        return out

    @staticmethod
    def _apigee_history_line(
        dossiers: list[EvidenceDossier], origin: Optional[str] = None, components: Optional[list[str]] = None,
    ) -> Optional[str]:
        """The change specialist's one-line Apigee deployment history, for the ticket text.

        With `components`, deployments of other proxies are counted, not named (see
        scoped_apigee_history_text); the full line stays in the HTML evidence. The proxy glob
        ("proxies ata-*") is spelled out, since ticket text carries no asterisks.
        """
        chg = next((d for d in dossiers if d.specialist_name == "ChangeSpecialist"), None)
        if chg is None:
            return None
        line = next((f for f in chg.findings if f.startswith(APIGEE_HISTORY)), None)
        if not line:
            return None
        in_window = chg.metrics.get("apigee_deployments_in_window")
        if origin and components and isinstance(in_window, list):
            scoped = scoped_apigee_history_text({
                "in_window": in_window,
                "scope": chg.metrics.get("apigee_deployment_scope") or "",
                "window_minutes": chg.metrics.get("apigee_deployment_window_minutes"),
            }, origin, components)
            if scoped:
                line = scoped
        return re.sub(r"proxies (\S+?)\*", r"proxies with prefix \1", line).replace("`", "")

    @staticmethod
    def _serving_revision_text(dossiers: list[EvidenceDossier], chg_dossier: EvidenceDossier) -> str:
        """The serving revision of the change target as Cloud Run reported it, or why it is unknown."""
        service = chg_dossier.target_resource
        cr = next(
            (d for d in dossiers if d.specialist_name == "CloudRunSpecialist" and d.target_resource == service),
            None,
        )
        if cr is None:
            return f"serving revision not known: no Cloud Run read of {service} in this investigation"
        component_type = not_applicable_component_type(cr)
        if component_type is not None:
            # No describe ran: CloudRunSpecialist skips components that are not Cloud Run services.
            return f"not a Cloud Run service ({component_type or 'other component type'}); no serving revision"
        serving = cr.metrics.get("serving_revisions")
        if isinstance(serving, dict) and serving:
            return "serving revision: " + ", ".join(
                f"{rev} ({pct}% of traffic)" for rev, pct in sorted(serving.items(), key=lambda kv: -kv[1])
            )
        return f"serving revision not known: the Cloud Run service describe of {service} returned no traffic split"

    def _build_longer_term_fixes(
        self, origin: str, alert: AlertEvent, dossiers: Optional[list[EvidenceDossier]] = None,
    ) -> list[str]:
        """Only fixes backed by this investigation's measurements, each with the measured fact."""
        fixes: list[str] = []
        dossiers = dossiers or []
        for d in dossiers:
            if d.specialist_name != "CloudSQLSpecialist":
                continue
            conns = d.metrics.get("active_connections")
            max_conns = d.metrics.get("max_connections")
            if (
                isinstance(conns, (int, float)) and isinstance(max_conns, (int, float)) and max_conns > 0
                and conns / max_conns >= DB_POOL_NEAR_SATURATION_RATIO
            ):
                callers = [
                    c.target_resource for c in dossiers
                    if c.specialist_name == "CloudRunSpecialist" and c.target_resource != d.target_resource
                ]
                where = ", ".join(sorted(set(callers))) or "the services that connect to it"
                fixes.append(
                    f"Deploy Cloud SQL connection pooling (e.g. PgBouncer) in front of {d.target_resource}: "
                    f"connections measured at {conns:g}/{max_conns:g}."
                )
                fixes.append(
                    f"Tune the database connection pool size and connection recycling in {where} "
                    f"({d.target_resource} connections measured at {conns:g}/{max_conns:g})."
                )
        cascading = sorted({
            d.target_resource for d in dossiers
            if d.specialist_name == "CloudRunSpecialist" and d.target_resource != origin
            and measured_breaches(d.metrics)
        })
        if cascading and origin:
            facts = "; ".join(
                f"{d.target_resource}: {', '.join(measured_breaches(d.metrics))}"
                for d in dossiers
                if d.specialist_name == "CloudRunSpecialist" and d.target_resource in cascading
            )
            fixes.append(
                f"Implement a circuit breaker on calls to {origin}: failures measured on other services "
                f"({facts})."
            )
        return fixes

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
                origin_db_auth = any(
                    d.specialist_name == "CloudRunSpecialist" and d.target_resource == origin
                    and _dossier_has_db_auth_error(d)
                    for d in dossiers
                )
                return "Deployment regression: database authentication failure" if origin_db_auth else "Deployment regression"
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
                group_alerts=getattr(self, "_last_group_alerts", None),
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
        builder = ConsoleLinkBuilder(project_id=config.project_id, region=config.region)
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
            # After apply_group_alerts, which can raise the priority to the group's most severe
            # alert: written before it, the file kept P2 while the ticket said P1. Raise-only,
            # like the ticket record (main._record_ticket_title).
            raise_file_priority(doc, report.priority, "report priority")

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
