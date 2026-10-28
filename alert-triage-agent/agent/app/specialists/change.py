"""ChangeSpecialist Agent for Alert Triage Runtime.

Analyzes Cloud Run and Apigee revision history over the change window (CHANGE_WINDOW_MINUTES, default 2 hours)
and correlates deployment timestamps with alert onset curves.
"""

import asyncio
import dataclasses
from datetime import datetime, timezone, timedelta
import logging
import math
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
# Default change window before the alert onset (CHANGE_WINDOW_MINUTES, AgentConfig.change_window_minutes).
DEFAULT_CHANGE_WINDOW_MINUTES = 120


def change_window_hours(window_minutes: int) -> int:
    """The whole hours of ata-tools change timeline that cover a change window (window_hours)."""
    return max(1, math.ceil(window_minutes / 60))


def change_window_label(window_minutes: int) -> str:
    """The change window in ticket text: "2-hour" for 120, "90-minute" for 90."""
    return f"{window_minutes // 60}-hour" if window_minutes % 60 == 0 else f"{window_minutes}-minute"


def no_cloud_run_changes_finding(window_minutes: int = DEFAULT_CHANGE_WINDOW_MINUTES) -> str:
    """The finding when no Cloud Run deploy is in the change window."""
    return f"No Cloud Run revision deployments detected in the {change_window_label(window_minutes)} window prior to alert onset."


# Change source / gap labels. The synthesizer lists change sources in Change-Induced.
def timeline_source(config: AgentConfig) -> str:
    """Name of the change timeline source in tickets and gap records: <NAME_PREFIX>-tools."""
    return f"{config.name_prefix}-tools change timeline"
CLOUD_RUN_HISTORY = "Cloud Run revision history"
APIGEE_HISTORY = "Apigee deployment history"
NO_CLOUD_RUN_CHANGES_FINDING = no_cloud_run_changes_finding()
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
    if APIGEE_HISTORY not in errors:
        # No error entry is not evidence of "no deployments": an older ata-tools (or a silent
        # failure) returns no "apigee" summary at all. Without the summary and its scope, the
        # Apigee history was not read, and the ticket must not state a negative (crev R4).
        problem = _apigee_summary_problem(timeline.get("apigee"))
        if problem:
            errors[APIGEE_HISTORY] = [problem]
    return {label: "; ".join(texts) for label, texts in errors.items()}


def _apigee_summary_problem(apigee: Any) -> Optional[str]:
    """Why a get_change_timeline "apigee" block cannot back a statement, or None if it can."""
    if apigee is None:
        return "the tools service returned no Apigee deployment summary (no 'apigee' block)"
    if not isinstance(apigee, dict):
        return f"the tools service returned a malformed Apigee deployment summary ({type(apigee).__name__}, not an object)"
    missing = [k for k in ("org", "environment") if not apigee.get(k)]
    if not isinstance(apigee.get("deployments"), list):
        missing.append("deployments")
    if missing:
        return f"the tools service Apigee deployment summary is incomplete (missing {', '.join(missing)})"
    return None


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


def timeline_apigee_deployments(
    timeline: dict[str, Any], now: datetime, window_minutes: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """Apigee deployment history of a get_change_timeline result, or None if that source failed.

    "in_window": deployments of the stack's proxies started in the window (newest first), and at
    most `window_minutes` before `now` when it is given (the timeline is read in whole hours);
    "current": every deployed revision of the stack's proxies with its deploy time;
    "scope": the Apigee org/environment/proxies ata-tools queried.
    """
    if APIGEE_HISTORY in timeline_source_errors(timeline):
        return None
    in_window = []
    for chg in timeline.get("changes", []):
        if isinstance(chg, dict) and chg.get("source") == "apigee_deployment" and not chg.get("error"):
            mins = _minutes_before(chg.get("timestamp"), now)
            if window_minutes is not None and mins > window_minutes:
                continue
            in_window.append({
                "proxy": chg.get("api_name"),
                "revision": str(chg.get("revision", "")),
                "environment": chg.get("environment"),
                "timestamp": chg.get("timestamp"),
                "deployed_minutes_ago": mins,
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
    return {
        "in_window": in_window, "current": current, "scope": ", ".join(scope_parts),
        "window_minutes": window_minutes or DEFAULT_CHANGE_WINDOW_MINUTES,
    }


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
    window = change_window_label(history.get("window_minutes") or DEFAULT_CHANGE_WINDOW_MINUTES)
    return f"Apigee deployment history{scope}: no proxy deployments in the {window} window prior to alert onset{tail}."


# Evaluate genuine changes within the change window (CHANGE_WINDOW_MINUTES, default 120):
# A revision is a genuine change ONLY when:
# 1. Created within the change window before anomaly onset.
# 2. Image digest or env/config differs from the previously created revision.
# 3. Traffic shifts to older revisions or without digest/env diff are NOT changes (is_traffic_shift: True).
def is_genuine_change(c: dict[str, Any], window_minutes: Optional[int] = None) -> bool:
    """True when a change record is a genuine deployment within the change window.

    The window is `window_minutes`, else the record's "change_window_minutes" (set by the
    ChangeSpecialist from CHANGE_WINDOW_MINUTES, so later readers of all_changes apply the
    same window), else DEFAULT_CHANGE_WINDOW_MINUTES.
    """
    # 1. Discard explicit traffic shifts
    if (
        c.get("action") == "traffic_shift"
        or c.get("is_traffic_shift") is True
        or c.get("details", {}).get("is_traffic_shift") is True
    ):
        return False

    # 2. Time window check: creation / deploy time must be within the change window and before/at anomaly onset
    window = window_minutes or c.get("change_window_minutes") or DEFAULT_CHANGE_WINDOW_MINUTES
    mins = c.get("deployed_minutes_ago", 999)
    if mins > window or mins < 0:
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


REQUEST_LATENCIES_METRIC = "run.googleapis.com/request_latencies"
# Per-call deadline of the healthy-period Cloud Monitoring reads. A timeout is a failed read:
# the healthy period is "unavailable" and only the onset-window rule applies, stated. Worst case:
# 2 reads per genuine Cloud Run deploy beyond the onset window, run one after another in a worker
# thread (the event loop stays free), so a Monitoring outage adds up to ~60 s per such deploy.
MONITORING_READ_TIMEOUT_SECONDS = 30.0


def _point_minute(pt: Any) -> datetime:
    """UTC minute a 60 s Monitoring point covers (its interval end minus one minute).

    ValueError when the point has no interval end time.
    """
    interval = getattr(pt, "interval", None)
    end = getattr(interval, "end_time", None) if interval is not None else None
    if end is None:
        raise ValueError("Cloud Monitoring point without an interval end time")
    if isinstance(end, datetime):
        dt = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    elif hasattr(end, "seconds"):
        dt = datetime.fromtimestamp(int(end.seconds), tz=timezone.utc)
    else:
        raise ValueError(f"Cloud Monitoring point interval end time of type {type(end).__name__}")
    return (dt.astimezone(timezone.utc) - timedelta(minutes=1)).replace(second=0, microsecond=0)


def _latency_series(
    metric_client: Any, project_id: str, region: str, service: str, revision: str, start: datetime, end: datetime
) -> Any:
    """request_latencies time series (full view) of `revision` of `service` in `region` over [start, end]."""
    from google.cloud import monitoring_v3

    req = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project_id}",
        filter=(
            f'metric.type = "{REQUEST_LATENCIES_METRIC}" AND resource.type = "cloud_run_revision" '
            f'AND resource.labels.location = "{region}" AND resource.labels.service_name = "{service}" '
            f'AND resource.labels.revision_name = "{revision}"'
        ),
        interval=monitoring_v3.TimeInterval(
            start_time={"seconds": int(start.timestamp())}, end_time={"seconds": int(end.timestamp())}
        ),
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    )
    return metric_client.list_time_series(request=req, timeout=MONITORING_READ_TIMEOUT_SECONDS)


def healthy_period(
    metric_client: Any,
    project_id: str,
    region: str,
    service: str,
    revision: str,
    deploy_timestamp: Any,
    onset: datetime,
    min_minutes: int,
    max_5xx_share: float = 0.05,
    max_latency_ms: float = 2000.0,
    min_active_minutes: int = 3,
) -> dict[str, Any]:
    """Whether `revision` had a healthy period between its deploy and the alert onset.

    Read per minute from Cloud Monitoring for this revision only: request_count by response
    code class and request_latencies. A minute with requests is healthy when its HTTP 5xx share
    is under `max_5xx_share` (a fraction, CHANGE_HEALTHY_MAX_5XX_SHARE) and its mean latency
    under `max_latency_ms` (CHANGE_HEALTHY_MAX_LATENCY_MS); it is unhealthy when either is at
    or over its threshold. These are heuristics, not the alert-policy conditions. A minute with
    requests but no latency point is judged on the 5xx share alone, and the evidence states how
    many minutes that was. Minutes without requests neither count nor break a span.

    Verdicts, counting only HEALTHY minutes with requests (failing minutes, including the
    fault's own lead-in before the onset, never count toward health):
      - "healthy": a span of at least `min_minutes` (first to last healthy minute, both
        counted) with no unhealthy minute inside and requests in at least
        `min_active_minutes` of its minutes (CHANGE_HEALTHY_MIN_ACTIVE_MINUTES);
      - "no_healthy_period": not one healthy minute, and at least one failing minute (S5);
      - "unavailable" with reason "insufficient_traffic": 1 to `min_active_minutes` - 1
        healthy minutes and no qualifying span;
      - "unavailable" with reason "inconclusive": at least `min_active_minutes` healthy
        minutes but no qualifying span.
    The verdict is monotone: adding a healthy minute never makes "no_healthy_period" more likely.

    Returns {"status": "healthy" | "no_healthy_period" | "unavailable", "evidence": str, ...}.
    "unavailable" also means the test could not be applied: a read failed, no request reached
    the revision, or request_latencies returned no point for it (the evidence is incomplete, so
    no verdict is drawn from the 5xx share alone). The caller falls back to the onset-window
    rule and says so.

    Both reads are filtered to the revision and bounded by MONITORING_READ_TIMEOUT_SECONDS; a
    timeout is a failed read ("unavailable").
    """
    deploy_dt = _parse_ts(deploy_timestamp)
    if deploy_dt is None:
        return {"status": "unavailable", "evidence": f"the creation time of {revision} is unknown"}
    window = f"between {_iso(deploy_dt)} (deploy) and {_iso(onset)} (alert onset)"
    try:
        per_minute: dict[datetime, list[int]] = {}
        for ts in request_count_series(
            metric_client, project_id, region, service, deploy_dt.timestamp(), onset.timestamp(),
            revision=revision, timeout=MONITORING_READ_TIMEOUT_SECONDS,
        ):
            # The filter already selects the revision; this guards against a series of another.
            if ts.resource.labels.get("revision_name", "") != revision:
                continue
            labels = getattr(getattr(ts, "metric", None), "labels", None) or {}
            is_5xx = labels.get("response_code_class") == "5xx" or str(labels.get("response_code", "")).startswith("5")
            for pt in ts.points:
                n = int(pt.value.int64_value)
                if n <= 0:
                    continue
                bucket = per_minute.setdefault(_point_minute(pt), [0, 0])
                bucket[0] += n
                if is_5xx:
                    bucket[1] += n
        if not per_minute:
            return {
                "status": "unavailable",
                "evidence": f"Cloud Monitoring {REQUEST_COUNT_METRIC} recorded no requests on {revision} {window}",
            }
        latency: dict[datetime, list[float]] = {}
        for ts in _latency_series(metric_client, project_id, region, service, revision, deploy_dt, onset):
            if ts.resource.labels.get("revision_name", "") != revision:
                continue
            for pt in ts.points:
                dist = pt.value.distribution_value
                count = int(getattr(dist, "count", 0) or 0)
                if count <= 0:
                    continue
                acc = latency.setdefault(_point_minute(pt), [0.0, 0])
                acc[0] += float(dist.mean) * count
                acc[1] += count
        if not latency:
            return {
                "status": "unavailable",
                "evidence": (
                    f"Cloud Monitoring {REQUEST_LATENCIES_METRIC} returned no data for {revision} {window}, "
                    f"although {REQUEST_COUNT_METRIC} recorded {sum(v[0] for v in per_minute.values())} requests on it"
                ),
            }
    except Exception as exc:
        logger.warning("Healthy-period query for %s on %s failed: %s", revision, service, exc)
        return {
            "status": "unavailable",
            "evidence": f"the Cloud Monitoring query for {revision} {window} failed ({type(exc).__name__})",
        }

    # Runs of healthy minutes: [start, end, requests, minutes with requests].
    runs: list[tuple[datetime, datetime, int, int]] = []
    run_start: Optional[datetime] = None
    run_end: Optional[datetime] = None
    run_requests = 0
    run_active = 0
    first_bad: Optional[tuple[datetime, str]] = None
    no_latency = 0

    def close_run() -> None:
        if run_start is not None and run_end is not None:
            runs.append((run_start, run_end, run_requests, run_active))

    for minute in sorted(per_minute):
        total, errors = per_minute[minute]
        share = errors * 100.0 / total
        lat = latency.get(minute)
        mean_ms = lat[0] / lat[1] if lat else None
        if mean_ms is None:
            no_latency += 1
        breach = []
        if errors / total >= max_5xx_share:
            breach.append(f"HTTP 5xx {share:.1f}% of {total} requests")
        if mean_ms is not None and mean_ms >= max_latency_ms:
            breach.append(f"mean latency {mean_ms:.0f} ms")
        if breach:
            if first_bad is None:
                first_bad = (minute, ", ".join(breach))
            close_run()
            run_start = run_end = None
            run_requests = run_active = 0
        else:
            run_start = run_start or minute
            run_end = minute
            run_requests += total
            run_active += 1
    close_run()

    def span_of(run: tuple[datetime, datetime, int, int]) -> int:
        return int((run[1] - run[0]).total_seconds() // 60) + 1

    def shown(run: tuple[datetime, datetime, int, int]) -> tuple[str, str]:
        # The first minute can start before the deploy; the stated span starts no earlier than it.
        return _iso(max(run[0], deploy_dt)), _iso(run[1] + timedelta(minutes=1))

    thresholds = (
        f"HTTP 5xx under {max_5xx_share * 100:g}% of requests and mean latency under {max_latency_ms:g} ms "
        f"(CHANGE_HEALTHY_MAX_5XX_SHARE {max_5xx_share:g}, CHANGE_HEALTHY_MAX_LATENCY_MS {max_latency_ms:g}; "
        "agent heuristics, not the alert-policy thresholds)"
    )
    gaps = ""
    if no_latency:
        gaps = (
            f"; {REQUEST_LATENCIES_METRIC} had no point for {no_latency} of {len(per_minute)} minutes with requests, "
            "judged on the 5xx share alone"
        )
    floor = f"CHANGE_HEALTHY_MIN_ACTIVE_MINUTES {min_active_minutes}"
    result: dict[str, Any] = {"window_start": _iso(deploy_dt), "window_end": _iso(onset)}
    long_runs = [r for r in runs if span_of(r) >= min_minutes]
    healthy_runs = [r for r in long_runs if r[3] >= min_active_minutes]
    if healthy_runs:
        best = max(healthy_runs, key=span_of)
        span, (start, end) = span_of(best), shown(best)
        result.update(
            status="healthy", start=start, end=end, minutes=span, requests=best[2], active_minutes=best[3],
            evidence=(
                f"healthy period: Cloud Monitoring shows {revision} served {best[2]} requests from "
                f"{start} to {end} ({span} one-minute intervals, at least {min_minutes}; {best[3]} of them with "
                f"requests, at least {min_active_minutes}) with {thresholds} "
                f"in every minute with requests, before the alert onset at {_iso(onset)}{gaps}"
            ),
        )
        return result
    # The floor counts HEALTHY minutes with requests only. Failing minutes (the fault's own
    # lead-in before the onset included) never count toward health, so the verdict is monotone:
    # adding a healthy minute can only move it from "no healthy period" towards "healthy".
    healthy_active = sum(r[3] for r in runs)
    unhealthy_active = len(per_minute) - healthy_active
    bad = f"; first minute over the thresholds {_iso(first_bad[0])} ({first_bad[1]})" if first_bad else ""
    if healthy_active == 0:
        # Not one healthy minute from the deploy to the onset, and at least one failing minute
        # (per_minute is not empty): the revision failed from its first request (S5).
        result.update(
            status="no_healthy_period", minutes=0, active_minutes=0, unhealthy_minutes=unhealthy_active,
            evidence=(
                f"no healthy period: Cloud Monitoring shows {revision} {window}: it had no minute of normal "
                f"traffic (all {unhealthy_active} minutes with requests were over a threshold) ({thresholds}){bad}{gaps}"
            ),
        )
        return result
    # Some healthy minutes but no qualifying span: neither a healthy period nor its absence is
    # established, so the healthy period is undetermined and only rule (a) applies.
    best = max(runs, key=lambda r: (span_of(r), r[3]))
    start, end = shown(best)
    detail = (
        f"{healthy_active} of its {len(per_minute)} minutes with requests were healthy; its longest span of "
        f"normal traffic was {span_of(best)} one-minute intervals ({start} to {end}) with requests in {best[3]} of them"
    )
    if healthy_active < min_active_minutes:
        reason = "insufficient_traffic"
        head = (
            f"insufficient traffic to establish a healthy period ({healthy_active} healthy active minutes < "
            f"{min_active_minutes}, {floor})"
        )
    else:
        reason = "inconclusive"
        head = (
            f"healthy period inconclusive ({healthy_active} healthy active minutes, but no span of at least "
            f"{min_minutes} one-minute intervals with requests in at least {min_active_minutes} of them, all healthy; "
            f"CHANGE_HEALTHY_PERIOD_MINUTES {min_minutes}, {floor})"
        )
    result.update(
        status="unavailable", reason=reason, minutes=0, active_minutes=healthy_active,
        unhealthy_minutes=unhealthy_active,
        evidence=f"{head}: Cloud Monitoring shows {revision} {window}: {detail} ({thresholds}){bad}{gaps}",
    )
    return result


def assess_deploy_causation(chg: dict[str, Any], onset: datetime, config: AgentConfig, metric_client: Any) -> dict[str, Any]:
    """Whether a genuine deploy can be the cause of an incident whose onset is `onset`.

    The cause only if (a) the onset is at most CHANGE_ONSET_WINDOW_MINUTES after the deploy,
    or (b) the new revision had no healthy period before the onset (see healthy_period).
    When the healthy-period data is unavailable, only (a) is applied and the evidence says so.
    Returns {"causal": bool, "rule": "a" | "b" | None, "evidence": str, "healthy_period": ...}.
    """
    raw_mins = chg.get("deployed_minutes_ago")
    mins = int(raw_mins) if raw_mins is not None else None
    window = config.change_onset_window_minutes
    revision, service = chg.get("revision") or "", chg.get("service") or ""
    if mins is not None and 0 <= mins <= window:
        return {
            "causal": True, "rule": "a", "healthy_period": None,
            "evidence": (
                f"deployed {mins}m before the alert onset, within the {window}-minute change onset window "
                "(CHANGE_ONSET_WINDOW_MINUTES)"
            ),
        }
    beyond = (
        f"deployed {mins}m before the alert onset, beyond the {window}-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES)"
        if mins is not None
        else f"deploy time relative to the alert onset unknown, so the {window}-minute change onset window "
        "(CHANGE_ONSET_WINDOW_MINUTES) does not apply"
    )
    if metric_client is None:
        hp: dict[str, Any] = {
            "status": "unavailable",
            "evidence": "Cloud Monitoring was not queried (no metric client)",
        }
    else:
        hp = healthy_period(
            metric_client, config.project_id, config.region, service, revision, chg.get("timestamp"),
            onset, config.change_healthy_period_minutes,
            max_5xx_share=config.change_healthy_max_5xx_share,
            max_latency_ms=config.change_healthy_max_latency_ms,
            min_active_minutes=config.change_healthy_min_active_minutes,
        )
    if hp.get("reason") in ("insufficient_traffic", "inconclusive"):
        evidence = f"{beyond}; {hp['evidence']}, so only the onset-window rule was applied"
        logger.warning("Change attribution for %s on %s: %s", revision, service, evidence)
        return {"causal": False, "rule": None, "evidence": evidence, "healthy_period": hp}
    if hp["status"] == "unavailable":
        evidence = (
            f"{beyond}; healthy-period data unavailable ({hp['evidence']}), so only the onset-window "
            "rule was applied"
        )
        logger.warning("Change attribution for %s on %s: %s", revision, service, evidence)
        return {"causal": False, "rule": None, "evidence": evidence, "healthy_period": hp}
    if hp["status"] == "healthy":
        return {"causal": False, "rule": None, "evidence": f"{beyond}; {hp['evidence']}", "healthy_period": hp}
    return {
        "causal": True, "rule": "b", "healthy_period": hp,
        "evidence": f"{beyond}; {hp['evidence']} (CHANGE_HEALTHY_PERIOD_MINUTES {config.change_healthy_period_minutes})",
    }


def is_candidate_cause(c: dict[str, Any]) -> bool:
    """A genuine change that passed the attribution rule (or was not assessed by it)."""
    causation = c.get("causation")
    return is_genuine_change(c) and (not isinstance(causation, dict) or bool(causation.get("causal")))


def unrelated_change_text(c: dict[str, Any]) -> str:
    """One non-causal deploy as listed in the ticket, with its evidence."""
    causation = c.get("causation") or {}
    return f"revision {c.get('revision')} on {c.get('service')} deployed {c.get('timestamp')}: {causation.get('evidence', '')}"


# A non-causal deploy is "likely unrelated" only when a healthy period was positively
# established. Otherwise (insufficient traffic, inconclusive, data unavailable) the period is
# undetermined: the deploy is "not attributed", and only the onset-window rule was applied.
UNRELATED_HEADLINE = "Recent changes (likely unrelated)"
UNATTRIBUTED_HEADLINE = "Recent changes (not attributed: healthy period undetermined)"
NONCAUSAL_HEADLINES = (UNRELATED_HEADLINE, UNATTRIBUTED_HEADLINE)
NONCAUSAL_METRIC_KEYS = ("unrelated_changes", "unrelated_changes_text", "unattributed_changes", "unattributed_changes_text")


def has_healthy_period(c: dict[str, Any]) -> bool:
    """True when the attribution rule positively established a healthy period for the deploy."""
    hp = (c.get("causation") or {}).get("healthy_period")
    return isinstance(hp, dict) and hp.get("status") == "healthy"


def _list_unrelated(
    metrics: dict[str, Any], findings: list[str], evidence_items: list[EvidenceItem], changes: list[dict[str, Any]]
) -> None:
    """State the deploys that failed the attribution rule, with their evidence.

    Under UNRELATED_HEADLINE when a healthy period was established (metrics "unrelated_changes"),
    else under UNATTRIBUTED_HEADLINE (metrics "unattributed_changes").
    """
    groups = (
        ("unrelated", UNRELATED_HEADLINE, [c for c in changes if has_healthy_period(c)]),
        ("unattributed", UNATTRIBUTED_HEADLINE, [c for c in changes if not has_healthy_period(c)]),
    )
    for key, headline, group in groups:
        if not group:
            continue
        listed = "; ".join(unrelated_change_text(c) for c in group)
        metrics[f"{key}_changes"] = [
            {
                "service": c.get("service"),
                "revision": c.get("revision"),
                "timestamp": c.get("timestamp"),
                "deployed_minutes_ago": c.get("deployed_minutes_ago"),
                "evidence": c["causation"]["evidence"],
            }
            for c in group
        ]
        metrics[f"{key}_changes_text"] = listed
        findings.append(f"{headline}: {listed}.")
        evidence_items.append(EvidenceItem(
            headline=headline,
            content="\n".join(f"- {unrelated_change_text(c)}" for c in group),
        ))


def _alert_onset(alert: AlertEvent) -> datetime:
    """The alert onset (started_at), else now."""
    if alert.started_at:
        try:
            return datetime.fromisoformat(alert.started_at.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


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

    def _assess(
        self, changes: list[dict[str, Any]], onset: datetime, config: AgentConfig, queries_executed: dict[str, list[str]]
    ) -> None:
        """Set each change's "causation" (assess_deploy_causation) and record the Monitoring reads."""
        if not changes:
            return
        client = self._metric_client_or_none(config)
        for c in changes:
            c["causation"] = assess_deploy_causation(c, onset, config, client)
            hp = c["causation"].get("healthy_period")
            if hp and hp.get("window_start"):
                queries_executed.setdefault("cloud_monitoring", []).append(
                    f'{request_count_filter(config.region, c.get("service") or "", c.get("revision") or "")} and '
                    f'{REQUEST_LATENCIES_METRIC} of revision {c.get("revision")} per minute, '
                    f'{hp["window_start"]} to {hp["window_end"]}'
                )

    def assess_group_changes(
        self, dossier: EvidenceDossier, services: set[str], alert: AlertEvent, config: AgentConfig
    ) -> EvidenceDossier:
        """Apply the attribution rule to genuine changes on `services` not assessed yet.

        investigate() assesses the changes of the alert's service and its upstream dependencies.
        A multi-service group also names other candidates (atat INC0010341: orders-api under an
        inventory-api origin); their deploys must not be named as a "Deployment regression"
        alternative without the same rule. Returns the dossier with any newly unrelated changes
        listed under "Recent changes (likely unrelated)" or "Recent changes (not attributed: ...)"
        (the same dossier when nothing changed).
        The assessed change records are shared with the input dossier's all_changes: their
        "causation" key is set in place, so the input dossier sees it too.
        """
        changes = dossier.metrics.get("all_changes") or []
        pending = [
            c for c in changes
            if isinstance(c, dict) and c.get("service") in services and "causation" not in c
            and is_genuine_change(c, config.change_window_minutes)
        ]
        if not pending:
            return dossier
        queries = {k: list(v) for k, v in dossier.queries_executed.items()}
        self._assess(pending, _alert_onset(alert), config, queries)
        unrelated = [c for c in changes if isinstance(c.get("causation"), dict) and not c["causation"]["causal"]]
        if not any(c in unrelated for c in pending):
            return dataclasses.replace(dossier, queries_executed=queries)
        metrics = {k: v for k, v in dossier.metrics.items() if k not in NONCAUSAL_METRIC_KEYS}
        # The listing is rebuilt; a "no deployments" statement would now contradict it.
        findings = [
            f for f in dossier.findings
            if not f.startswith(tuple(f"{h}:" for h in NONCAUSAL_HEADLINES))
            and f != no_cloud_run_changes_finding(config.change_window_minutes)
        ]
        items = [
            i for i in dossier.evidence_items
            if i.headline not in NONCAUSAL_HEADLINES
            and not (i.headline == "Deployment History Analysis" and "No recent Cloud Run deployments" in (i.content or ""))
        ]
        _list_unrelated(metrics, findings, items, unrelated)
        logger.info(
            "Change attribution for group services %s: %s", sorted(services),
            "; ".join(metrics[k] for k in ("unrelated_changes_text", "unattributed_changes_text") if k in metrics),
        )
        return dataclasses.replace(dossier, metrics=metrics, findings=findings, evidence_items=items, queries_executed=queries)

    def _metric_client_or_none(self, config: AgentConfig) -> Any:
        """The Cloud Monitoring client, or None when it is unavailable (stated by the caller)."""
        try:
            return self._get_metric_client(config)
        except Exception as exc:
            logger.warning("Cloud Monitoring client unavailable: %s", exc)
            return None

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
        text = f"change timeline unavailable: {reason.rstrip('. ')}"
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
            timeline = await fetch_change_timeline(config, window_hours=change_window_hours(config.change_window_minutes))
        except ToolsNotConfigured as exc:
            if config.is_local:
                logger.info("TOOLS_URL not set (local mode): %s not queried", timeline_source(config))
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
                        source=f"{timeline_source(config)} ({label})", target=alert.resource_name,
                        reason=f"{label} source failed ({errors[label]})",
                        not_assessed=("Apigee proxy deployment history was" if label == APIGEE_HISTORY
                                      else "Cloud Run revision history was"),
                    )
                else:
                    record_change_source(metrics, f"{timeline_source(config)} ({label})")
            return timeline
        self._timeline_unavailable(
            metrics, findings, evidence_items,
            source=timeline_source(config), target=alert.resource_name, reason=reason,
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
                f"MCP tools/call get_change_timeline(window_hours={change_window_hours(config.change_window_minutes)}, "
                f"project_id={config.project_id}, region={config.region}) on {config.tools_url or f'{config.name_prefix}-tools (TOOLS_URL not set)'}"
            ],
        }

        now = _alert_onset(alert)

        # 1. Change timeline from the deployed ata-tools service: Cloud Run revision history and
        # Apigee deployment history. There is no fallback: when it cannot be read, the report
        # says so and change attribution is not assessed.
        timeline = await self._read_change_timeline(alert, config, metrics, findings, evidence_items)
        recent_changes = timeline_cloud_run_changes(timeline, now) if timeline else []
        for c in recent_changes:
            c["change_window_minutes"] = config.change_window_minutes
        metrics["change_window_minutes"] = config.change_window_minutes
        # Change-Induced "No (... in the 2 h before the alert ...)" states the window used.
        w = config.change_window_minutes
        metrics["change_window_text"] = f"{w // 60} h" if w % 60 == 0 else f"{w} min"
        apigee_deployments = (
            timeline_apigee_deployments(timeline, now, config.change_window_minutes) if timeline else None
        )
        if apigee_deployments is not None:
            metrics["apigee_deployments_in_window"] = apigee_deployments["in_window"]
            metrics["apigee_deployments_current"] = apigee_deployments["current"]
            metrics["apigee_deployment_scope"] = apigee_deployments["scope"]

        # 2. Cloud Logging audit query for Services.ReplaceService / UpdateService events
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

        genuine_changes = [
            c for c in recent_changes if c.get("service") in candidate_services and is_genuine_change(c, config.change_window_minutes)
        ]
        # A deploy in the window is the cause only by the onset-window or no-healthy-period rule
        # (assess_deploy_causation); the others are listed as likely unrelated (healthy period
        # established) or not attributed (healthy period undetermined), with evidence.
        # The verdict is kept on the change record, so every reader of all_changes sees it.
        # In a worker thread: the healthy-period reads are blocking Cloud Monitoring calls.
        await asyncio.to_thread(self._assess, genuine_changes, now, config, queries_executed)
        relevant_changes = [c for c in genuine_changes if c["causation"]["causal"]]
        unrelated_changes = [c for c in genuine_changes if not c["causation"]["causal"]]
        _list_unrelated(metrics, findings, evidence_items, unrelated_changes)

        if relevant_changes:
            target_changes = [c for c in relevant_changes if c.get("service") == target]
            chg = target_changes[0] if target_changes else relevant_changes[0]
            change_label = "configuration update" if chg.get("change_type") == "service_update" else "revision deployment"
            rev_label = f"`{chg['revision']}`"
            findings.append(
                f"Recent {change_label} detected on `{chg['service']}` ({chg['deployed_minutes_ago']}m ago): {rev_label}."
            )
            findings.append("Deployment timing aligns with alert onset curve.")
            findings.append(f"Change attribution basis for {chg['revision']}: {chg['causation']['evidence']}.")
            metrics["change_causation"] = chg["causation"]["evidence"]
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
                        f"Correlation: Direct temporal correlation with error curve onset.\n"
                        f"Attribution basis: {chg['causation']['evidence']}."
                    ),
                )
            )
        elif metrics.get(UNAVAILABLE_KEY):
            # A change source could not be read: "no deployments" would be an unverified claim.
            # Change timeline gaps are already stated ("change timeline unavailable: ...").
            other = [g for g in metrics[UNAVAILABLE_KEY] if not str(g.get("source", "")).startswith(timeline_source(config))]
            if other:
                gaps = "; ".join(gap_text(g) for g in other)
                content = f"Change detection incomplete: {gaps}; change attribution not assessed."
                findings.append(content)
                evidence_items.append(EvidenceItem(headline="Deployment History Analysis (incomplete)", content=content))
        elif not metrics.get(CHANGE_SOURCES_KEY):
            # Nothing was read (local run without TOOLS_URL): no finding either way.
            content = (
                "Change history not queried: no change source was available (TOOLS_URL not set in local "
                f"mode, so the {timeline_source(config)} with Cloud Run revision and Apigee deployment "
                "history was not read; the Cloud Run audit log is not read in local mode); change "
                "attribution not assessed."
            )
            findings.append(content)
            evidence_items.append(EvidenceItem(headline="Deployment History Analysis (not queried)", content=content))
        elif not unrelated_changes:
            findings.append(no_cloud_run_changes_finding(config.change_window_minutes))
            evidence_items.append(EvidenceItem(
                headline="Deployment History Analysis",
                content=f"No recent Cloud Run deployments detected ({timeline_source(config)}: {CLOUD_RUN_HISTORY}).",
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
