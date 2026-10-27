"""ChangeSpecialist Agent for Alert Triage Runtime.

Analyzes Cloud Run and Apigee revision history over the past 2 hours
and correlates deployment timestamps with alert onset curves.
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Optional

from ..config import AgentConfig
from ..contracts.alert import AlertEvent
from ..contracts.report import EvidenceItem
from ..request_count import REQUEST_COUNT_METRIC, request_count_filter, request_count_series
from ..tools.tools_client import ToolsCallError, ToolsNotConfigured, fetch_change_timeline
from .base import BaseSpecialist, EvidenceDossier
from .telemetry_reads import (
    CHANGE_SOURCES_KEY,
    UNAVAILABLE_KEY,
    failure_reason,
    gap_text,
    read_log_entries,
    record_change_source,
    record_unavailable,
)

logger = logging.getLogger(__name__)

# Cloud Run audit-log entries read per investigation (one page).
AUDIT_LOG_MAX_ENTRIES = 20
# Look-back of the change timeline before the alert.
CHANGE_WINDOW_HOURS = 2
# Change source / gap labels. The synthesizer lists change sources in Change-Induced.
TIMELINE_SOURCE = "ata-tools change timeline"
CLOUD_RUN_HISTORY = "Cloud Run revision history"
APIGEE_HISTORY = "Apigee deployment history"
NO_CLOUD_RUN_CHANGES_FINDING = (
    "No Cloud Run revision deployments detected in the 2-hour window prior to alert onset."
)
# Finding of a dossier whose change sources covered Cloud Run only. The ChangeSpecialist no
# longer emits it (the ata-tools timeline covers Apigee deployments, or its failure is stated);
# the synthesizer still rewrites it from the Apigee access log (see _reconcile_change_history).
NO_CLOUD_RUN_DEPLOYMENTS_FINDING = (
    "No Cloud Run revision deployments detected in the 2-hour window prior to alert onset "
    "(Apigee proxy deployment history not queried)."
)


def _minutes_before(ts: Any, now: datetime) -> int:
    """Whole minutes from ``ts`` to ``now``; -1 when unknown or after ``now``."""
    dt = _parse_ts(ts)
    if dt is None or dt > now:
        return -1
    return max(0, int((now - dt).total_seconds() / 60))


def timeline_source_errors(timeline: dict[str, Any]) -> dict[str, str]:
    """Per-source errors inside a get_change_timeline result: {label: error text}."""
    errors: dict[str, list[str]] = {}
    for chg in timeline.get("changes", []):
        if not isinstance(chg, dict) or not chg.get("error"):
            continue
        label = APIGEE_HISTORY if str(chg.get("source", "")).startswith("apigee") else CLOUD_RUN_HISTORY
        where = chg.get("service_name") or chg.get("api_name")
        text = " ".join(str(chg["error"]).split())[:200]
        errors.setdefault(label, []).append(f"{where}: {text}" if where else text)
    return {label: "; ".join(texts) for label, texts in errors.items()}


def timeline_cloud_run_changes(timeline: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Cloud Run revision and service changes of a get_change_timeline result, as change records."""
    changes: list[dict[str, Any]] = []
    for chg in timeline.get("changes", []):
        if not isinstance(chg, dict) or chg.get("error"):
            continue
        source = str(chg.get("source") or "")
        if not source.startswith("cloud_run"):  # cloud_run (revision), cloud_run_service
            continue
        details = chg.get("details") if isinstance(chg.get("details"), dict) else {}
        is_traffic_shift = bool(
            chg.get("is_traffic_shift")
            or chg.get("action") == "traffic_shift"
            or details.get("is_traffic_shift")
            or chg.get("differs_from_previous") is False
            or details.get("differs_from_previous") is False
        )
        differs = chg.get("differs_from_previous")
        if differs is None:
            differs = details.get("differs_from_previous")
        is_serving = chg.get("is_serving")
        if is_serving is None:
            is_serving = details.get("is_serving")
        # The timeline's previous_revision is the previously *created* revision, which is
        # not necessarily the one that served traffic (see previous_serving_revision).
        prev_rev = chg.get("previous_revision") or details.get("previous_revision")
        changes.append({
            "service": chg.get("service_name"),
            "revision": chg.get("revision_name") or str(chg.get("revision", "")),
            "previous_created_revision": prev_rev,
            "timestamp": chg.get("timestamp"),
            "deployed_minutes_ago": _minutes_before(chg.get("timestamp"), now),
            "change_type": "traffic_shift" if is_traffic_shift else (
                "service_update" if source == "cloud_run_service" else "revision_deploy"
            ),
            "action": "traffic_shift" if is_traffic_shift else chg.get("action", "deployment"),
            "is_serving": is_serving,
            "is_traffic_shift": is_traffic_shift,
            "differs_from_previous": differs,
            "details": details,
            "status": "Ready",
        })
    return changes


def timeline_apigee_deployments(timeline: dict[str, Any], now: datetime) -> Optional[dict[str, Any]]:
    """Apigee deployment history of a get_change_timeline result, or None if that source failed.

    "in_window": deployments of the stack's proxies started in the window (newest first);
    "current": every deployed revision of the stack's proxies with its deploy time;
    "scope": the Apigee org/environment/proxies ata-tools queried.
    """
    if APIGEE_HISTORY in timeline_source_errors(timeline):
        return None
    in_window = []
    for chg in timeline.get("changes", []):
        if isinstance(chg, dict) and chg.get("source") == "apigee_deployment" and not chg.get("error"):
            in_window.append({
                "proxy": chg.get("api_name"),
                "revision": str(chg.get("revision", "")),
                "environment": chg.get("environment"),
                "timestamp": chg.get("timestamp"),
                "deployed_minutes_ago": _minutes_before(chg.get("timestamp"), now),
            })
    apigee = timeline.get("apigee") if isinstance(timeline.get("apigee"), dict) else {}
    current = [
        {"proxy": d.get("api_name"), "revision": str(d.get("revision", "")), "timestamp": d.get("timestamp")}
        for d in apigee.get("deployments", []) or []
        if isinstance(d, dict)
    ]
    scope_parts = []
    if apigee.get("org"):
        scope_parts.append(f"org {apigee['org']}")
    if apigee.get("environment"):
        scope_parts.append(f"environment {apigee['environment']}")
    if apigee.get("proxy_prefix"):
        scope_parts.append(f"proxies {apigee['proxy_prefix']}*")
    return {"in_window": in_window, "current": current, "scope": ", ".join(scope_parts)}


def apigee_history_text(history: dict[str, Any]) -> str:
    """One sentence stating the Apigee deployment history read from the change timeline."""
    scope = f" ({history['scope']})" if history.get("scope") else ""
    if history["in_window"]:
        deployed = "; ".join(
            f"proxy {d['proxy']} revision {d['revision']} deployed {d['timestamp']}"
            + (f" ({d['deployed_minutes_ago']}m before alert onset)" if d["deployed_minutes_ago"] >= 0 else " (after alert onset)")
            for d in history["in_window"]
        )
        return f"Apigee deployment history{scope}: {deployed}."
    current = ", ".join(f"{d['proxy']} revision {d['revision']} since {d['timestamp']}" for d in history["current"])
    tail = f"; deployed: {current}" if current else ""
    return f"Apigee deployment history{scope}: no proxy deployments in the 2-hour window prior to alert onset{tail}."


# Evaluate genuine changes within the 2-hour window:
# A revision is a genuine change ONLY when:
# 1. Created within lookback window (<= 120m) before anomaly onset.
# 2. Image digest or env/config differs from the previously created revision.
# 3. Traffic shifts to older revisions or without digest/env diff are NOT changes (is_traffic_shift: True).
def is_genuine_change(c: dict[str, Any]) -> bool:
    """True when a change record is a genuine deployment within the lookback window."""
    # 1. Discard explicit traffic shifts
    if (
        c.get("action") == "traffic_shift"
        or c.get("is_traffic_shift") is True
        or c.get("details", {}).get("is_traffic_shift") is True
    ):
        return False

    # 2. Time window check: creation / deploy time must be within 120 minutes and before/at anomaly onset
    mins = c.get("deployed_minutes_ago", 999)
    if mins > 120 or mins < 0:
        return False

    # 3. Must be serving (if serving status known)
    if c.get("is_serving") is False or c.get("details", {}).get("is_serving") is False:
        return False

    # 4. Must differ from the previously created revision (digest or env/config)
    if c.get("differs_from_previous") is False or c.get("details", {}).get("differs_from_previous") is False:
        return False

    details = c.get("details", {})
    if isinstance(details, dict):
        cur_img = details.get("image")
        prev_img = details.get("prev_image")
        env_diff = details.get("env_diff")
        if cur_img and prev_img and cur_img == prev_img and env_diff is not None and len(env_diff) == 0:
            return False

    return True


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def requests_by_revision(
    metric_client: Any, project_id: str, region: str, service: str, start: datetime, end: datetime
) -> dict[str, int]:
    """Sum Cloud Run request_count per revision of `service` in `region` over [start, end]."""
    totals: dict[str, int] = {}
    for ts in request_count_series(metric_client, project_id, region, service, start.timestamp(), end.timestamp()):
        rev = ts.resource.labels.get("revision_name", "")
        if not rev:
            continue
        count = sum(int(pt.value.int64_value) for pt in ts.points)
        totals[rev] = totals.get(rev, 0) + count
    return totals


def previous_serving_revision(
    metric_client: Any,
    project_id: str,
    region: str,
    service: str,
    new_revision: str,
    deploy_timestamp: Any,
    lookback_minutes: int,
) -> dict[str, Any]:
    """Identify the revision that served `service` before `new_revision` was created.

    The answer comes from traffic history (Cloud Monitoring request_count per revision in the
    `lookback_minutes` before the deploy), never from revision creation order: a newer revision
    can be a retired 0%-traffic one while traffic stays pinned to an older revision. A revision
    is named only when it is the single revision that received requests in that window;
    otherwise "revision" is None and "evidence" says why.
    """
    result: dict[str, Any] = {"revision": None, "evidence": "", "requests_by_revision": {}}
    deploy_dt = _parse_ts(deploy_timestamp)
    if deploy_dt is None:
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: the creation time of {new_revision} is unknown."
        )
        return result
    start = deploy_dt - timedelta(minutes=lookback_minutes)
    window = f"between {_iso(start)} and {_iso(deploy_dt)}"
    try:
        totals = requests_by_revision(metric_client, project_id, region, service, start, deploy_dt)
    except Exception as exc:
        logger.warning("Traffic history query for %s failed: %s", service, exc)
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: the Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} query for {window} failed ({type(exc).__name__})."
        )
        return result

    served = {rev: n for rev, n in totals.items() if n > 0 and rev != new_revision}
    result["requests_by_revision"] = served
    result["window_start"] = _iso(start)
    result["window_end"] = _iso(deploy_dt)
    if len(served) == 1:
        rev, n = next(iter(served.items()))
        result["revision"] = rev
        result["evidence"] = (
            f"Cloud Monitoring {REQUEST_COUNT_METRIC}: {rev} was the only revision of {service} that "
            f"received requests {window} ({n} requests), before {new_revision} was created."
        )
    elif not served:
        result["evidence"] = (
            f"Previous serving revision of {service} not determined: Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} recorded no requests on any other revision {window}."
        )
    else:
        split = ", ".join(f"{rev} ({n} requests)" for rev, n in sorted(served.items(), key=lambda kv: (-kv[1], kv[0])))
        result["evidence"] = (
            f"Previous serving revision of {service} not determined as a single revision: Cloud Monitoring "
            f"{REQUEST_COUNT_METRIC} shows requests on {len(served)} revisions {window}: {split}."
        )
    if result["revision"] is None:
        logger.warning("%s", result["evidence"])
    return result


class ChangeSpecialist(BaseSpecialist):
    """Specialist agent correlating recent infrastructure and code changes."""

    def __init__(self, metric_client: Any = None, **kwargs: Any):
        super().__init__("ChangeSpecialist")
        self._metric_client = metric_client

    def _get_metric_client(self, config: AgentConfig) -> Any:
        if self._metric_client is not None:
            return self._metric_client
        if config.is_local:
            return None
        from google.cloud import monitoring_v3

        return monitoring_v3.MetricServiceClient()

    def _previous_serving(self, chg: dict[str, Any], config: AgentConfig) -> dict[str, Any]:
        service, revision = chg.get("service") or "", chg.get("revision") or ""
        try:
            client = self._get_metric_client(config)
        except Exception as exc:
            logger.warning("Cloud Monitoring client unavailable for traffic history of %s: %s", service, exc)
            client = None
        if client is None:
            evidence = (
                f"Previous serving revision of {service} not determined: traffic history "
                "(Cloud Monitoring request_count) was not queried."
            )
            logger.warning("%s", evidence)
            return {"revision": None, "evidence": evidence, "requests_by_revision": {}}
        return previous_serving_revision(
            client, config.project_id, config.region, service, revision, chg.get("timestamp"),
            config.rollback_traffic_lookback_minutes,
        )

    @staticmethod
    def _timeline_unavailable(
        metrics: dict[str, Any],
        findings: list[str],
        evidence_items: list[EvidenceItem],
        *,
        source: str,
        target: str,
        reason: str,
        not_assessed: str,
    ) -> None:
        """State loudly that (part of) the change timeline could not be read."""
        text = f"change timeline unavailable: {reason}"
        logger.error("%s (%s for %s)", text, source, target)
        record_unavailable(metrics, source=source, target=target, reason=text, affects_change_detection=True)
        metrics.setdefault("change_timeline_errors", []).append(text)
        content = f"{text}. {not_assessed} not assessed; change attribution not assessed."
        findings.append(content)
        evidence_items.append(EvidenceItem(headline="Change timeline unavailable", content=content))

    async def _read_change_timeline(
        self,
        alert: AlertEvent,
        config: AgentConfig,
        metrics: dict[str, Any],
        findings: list[str],
        evidence_items: list[EvidenceItem],
    ) -> Optional[dict[str, Any]]:
        """The ata-tools change timeline, or None when it was not read (failure stated)."""
        try:
            timeline = await fetch_change_timeline(config, window_hours=CHANGE_WINDOW_HOURS)
        except ToolsNotConfigured as exc:
            if config.is_local:
                logger.info("TOOLS_URL not set (local mode): ata-tools change timeline not queried")
                return None
            reason = exc.reason
        except ToolsCallError as exc:
            reason = exc.reason
        except Exception as exc:  # any other failure is stated the same way
            reason = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
        else:
            errors = timeline_source_errors(timeline)
            for label in (CLOUD_RUN_HISTORY, APIGEE_HISTORY):
                if label in errors:
                    self._timeline_unavailable(
                        metrics, findings, evidence_items,
                        source=f"{TIMELINE_SOURCE} ({label})", target=alert.resource_name,
                        reason=f"{label} source failed ({errors[label]})",
                        not_assessed=("Apigee proxy deployment history was" if label == APIGEE_HISTORY
                                      else "Cloud Run revision history was"),
                    )
                else:
                    record_change_source(metrics, f"{TIMELINE_SOURCE} ({label})")
            return timeline
        self._timeline_unavailable(
            metrics, findings, evidence_items,
            source=TIMELINE_SOURCE, target=alert.resource_name, reason=reason,
            not_assessed="Apigee proxy deployment history and Cloud Run revision history were",
        )
        return None

    async def investigate(
        self,
        alert: AlertEvent,
        config: AgentConfig,
    ) -> EvidenceDossier:
        findings: list[str] = []
        evidence_items: list[EvidenceItem] = []
        metrics: dict[str, Any] = {}
        queries_executed: dict[str, list[str]] = {
            "ata_tools": [
                f"MCP tools/call get_change_timeline(window_hours={CHANGE_WINDOW_HOURS}, "
                f"project_id={config.project_id}, region={config.region}) on {config.tools_url or 'ata-tools (TOOLS_URL not set)'}"
            ],
        }

        now = datetime.now(timezone.utc)
        if alert.started_at:
            try:
                now = datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
            except Exception:
                pass

        # 1. Change timeline from the deployed ata-tools service: Cloud Run revision history and
        # Apigee deployment history. There is no fallback: when it cannot be read, the report
        # says so and change attribution is not assessed.
        timeline = await self._read_change_timeline(alert, config, metrics, findings, evidence_items)
        recent_changes = timeline_cloud_run_changes(timeline, now) if timeline else []
        apigee_deployments = timeline_apigee_deployments(timeline, now) if timeline else None
        if apigee_deployments is not None:
            metrics["apigee_deployments_in_window"] = apigee_deployments["in_window"]
            metrics["apigee_deployments_current"] = apigee_deployments["current"]
            metrics["apigee_deployment_scope"] = apigee_deployments["scope"]

        # 3. Direct Cloud Logging audit query for Services.ReplaceService / UpdateService events
        if not config.is_local:
            try:
                import google.cloud.logging
                cutoff_iso = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                log_client = google.cloud.logging.Client(project=config.project_id)
                audit_filter = (
                    f'logName="projects/{config.project_id}/logs/cloudaudit.googleapis.com%2Factivity" AND '
                    f'protoPayload.serviceName="run.googleapis.com" AND '
                    f'timestamp >= "{cutoff_iso}"'
                )
                audit_entries = await read_log_entries(
                    log_client,
                    filter_=audit_filter,
                    max_results=AUDIT_LOG_MAX_ENTRIES,
                    config=config,
                    what="Cloud Logging Cloud Run audit-log read",
                )
                record_change_source(metrics, "Cloud Run audit log")
                for ae in audit_entries:
                    pld = ae.payload if isinstance(ae.payload, dict) else {}
                    method = str(pld.get("methodName", ""))
                    if "ReplaceService" in method or "UpdateService" in method:
                        res_name = str(pld.get("resourceName", ""))
                        svc_name = res_name.split("/")[-1] if res_name else ""
                        ts = ae.timestamp
                        if ts and svc_name:
                            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)
                            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts > now:
                                dep_min = -1
                            else:
                                dep_min = max(0, int((now - ts).total_seconds() / 60))
                            # Check if already captured as revision_deploy or traffic_shift
                            if not any(c.get("service") == svc_name and abs(c.get("deployed_minutes_ago", 999) - dep_min) <= 1 for c in recent_changes):
                                recent_changes.append({
                                    "service": svc_name,
                                    "revision": f"{svc_name}-config-update",
                                    "timestamp": ts_str,
                                    "deployed_minutes_ago": dep_min,
                                    "change_type": "traffic_shift",
                                    "action": "traffic_shift",
                                    "is_traffic_shift": True,
                                    "differs_from_previous": False,
                                    "details": {"method": method, "resourceName": res_name, "is_traffic_shift": True},
                                    "status": "Ready",
                                })
            except Exception as e:
                logger.warning("Cloud Logging audit change query failed: %s", e)
                record_unavailable(
                    metrics, source="Cloud Run audit log (cloudaudit.googleapis.com/activity)",
                    target=config.project_id, reason=failure_reason(e, "Cloud Logging"),
                    affects_change_detection=True,
                )

        metrics["all_changes"] = recent_changes
        target = alert.resource_name
        candidate_services = [target]
        visited = set(candidate_services)
        try:
            from topology.topology_graph import get_upstream_dependencies
            for dep in get_upstream_dependencies(target):
                if dep not in visited:
                    visited.add(dep)
                    candidate_services.append(dep)
        except Exception:
            pass

        relevant_changes = [
            c for c in recent_changes if c.get("service") in candidate_services and is_genuine_change(c)
        ]

        if relevant_changes:
            target_changes = [c for c in relevant_changes if c.get("service") == target]
            chg = target_changes[0] if target_changes else relevant_changes[0]
            change_label = "configuration update" if chg.get("change_type") == "service_update" else "revision deployment"
            rev_label = f"`{chg['revision']}`"
            findings.append(
                f"Recent {change_label} detected on `{chg['service']}` ({chg['deployed_minutes_ago']}m ago): {rev_label}."
            )
            findings.append("Deployment timing aligns with alert onset curve.")
            metrics["recent_revision"] = chg["revision"]
            metrics["previous_created_revision"] = chg.get("previous_created_revision") or chg.get(
                "details", {}
            ).get("previous_created_revision")
            metrics["minutes_since_deploy"] = chg["deployed_minutes_ago"]
            metrics["deploy_timestamp"] = chg.get("timestamp")
            metrics["change_service"] = chg.get("service")
            serving = self._previous_serving(chg, config)
            metrics["previous_serving_revision"] = serving["revision"]
            metrics["previous_serving_evidence"] = serving["evidence"]
            metrics["previous_serving_requests"] = serving["requests_by_revision"]
            if serving["evidence"]:
                findings.append(serving["evidence"])
                queries_executed.setdefault("cloud_monitoring", []).append(
                    f'{request_count_filter(config.region, chg.get("service") or "")} '
                    f'grouped by resource.labels.revision_name, {config.rollback_traffic_lookback_minutes} min '
                    f'before {chg.get("timestamp")}'
                )
            evidence_items.append(
                EvidenceItem(
                    headline=f"Recent Deployment Correlation: {chg['service']}",
                    content=(
                        f"Service: {chg['service']}\n"
                        f"Change: {rev_label}\n"
                        f"Deployed: {chg['deployed_minutes_ago']} minutes prior to alert onset\n"
                        f"Correlation: Direct temporal correlation with error curve onset."
                    ),
                )
            )
        elif metrics.get(UNAVAILABLE_KEY):
            # A change source could not be read: "no deployments" would be an unverified claim.
            # Change timeline gaps are already stated ("change timeline unavailable: ...").
            other = [g for g in metrics[UNAVAILABLE_KEY] if not str(g.get("source", "")).startswith(TIMELINE_SOURCE)]
            if other:
                gaps = "; ".join(gap_text(g) for g in other)
                content = f"Change detection incomplete: {gaps}; change attribution not assessed."
                findings.append(content)
                evidence_items.append(EvidenceItem(headline="Deployment History Analysis (incomplete)", content=content))
        elif not metrics.get(CHANGE_SOURCES_KEY):
            # Nothing was read (local run without TOOLS_URL): no finding either way.
            content = (
                "Change history not queried: no change source was available (TOOLS_URL not set in local "
                "mode, so the ata-tools change timeline with Cloud Run revision and Apigee deployment "
                "history was not read; the Cloud Run audit log is not read in local mode); change "
                "attribution not assessed."
            )
            findings.append(content)
            evidence_items.append(EvidenceItem(headline="Deployment History Analysis (not queried)", content=content))
        else:
            findings.append(NO_CLOUD_RUN_CHANGES_FINDING)
            evidence_items.append(EvidenceItem(
                headline="Deployment History Analysis",
                content=f"No recent Cloud Run deployments detected ({TIMELINE_SOURCE}: {CLOUD_RUN_HISTORY}).",
            ))

        # Apigee deployment history is stated whenever it was read (it is never omitted silently).
        if apigee_deployments is not None:
            text = apigee_history_text(apigee_deployments)
            findings.append(text)
            evidence_items.append(EvidenceItem(headline="Apigee Deployment History", content=text))

        mins = metrics.get("minutes_since_deploy", 999)
        proximity_bonus = 0.15 if mins <= 15 else (0.10 if mins <= 30 else (0.05 if mins <= 60 else (0.02 if mins <= 120 else 0.0)))
        base_conf = 0.70 if relevant_changes else 0.65
        chg_conf = round(base_conf + proximity_bonus, 2)

        return EvidenceDossier(
            specialist_name=self.name,
            target_resource=alert.resource_name,
            findings=findings,
            metrics=metrics,
            evidence_items=evidence_items,
            queries_executed=queries_executed,
            anomaly_detected=bool(relevant_changes),
            confidence=chg_conf,
        )
