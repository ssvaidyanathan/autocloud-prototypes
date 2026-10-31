"""Onset detection and temporal reasoning for incident investigations.

Identifies the timestamp when symptoms actually started using request logs,
error logs, and metric deviations, anchors the investigation window, and separates
prior resolved incidents from the active incident.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, Union

from .config import AgentConfig, get_settings
from .tools.common import normalize_datetime

logger = logging.getLogger(__name__)

# Routine operational messages that must not be treated as failure symptoms
BENIGN_PLATFORM_EVENT_PATTERNS = (
    "autoscaling",
    "instance started due to configured scaling factors",
    "probe succeeded",
    "starting new instance",
)

# Platform failure symptom patterns for onset detection
SYMPTOM_PLATFORM_EVENT_PATTERNS = (
    "oom",
    "memory limit exceeded",
    "memory limit",
    "terminated by kernel oom-killer",
    "terminated by kernel",
    "exit code 137",
    "exit 137",
    "exit code",
    "container terminated",
    "container termination",
    "container failed",
    "restarting container",
    "container restart",
    "readiness probe failed",
    "startup probe failed",
    "probe failed",
    "probe failure",
    "failed to start and listen",
    "sigkill",
)


@dataclass
class PriorIncident:
    """Represents a prior, resolved incident episode within the query window."""

    start_time: datetime
    end_time: datetime
    error_count: int
    description: str


@dataclass
class OnsetDetectionResult:
    """Outcome of onset detection and window anchoring."""

    onset_time: Optional[datetime]
    anchored_start_time: datetime
    end_time: datetime
    is_fallback: bool
    prior_incidents: List[PriorIncident] = field(default_factory=list)
    summary: str = ""


def detect_onset_from_telemetry(
    service_name: str,
    end_time: Optional[Union[datetime, str]] = None,
    window_minutes: Optional[int] = None,
    pre_buffer_minutes: Optional[int] = None,
    project_id: Optional[str] = None,
    logging_client: Any = None,
    metrics_client: Any = None,
    request_logs_fn: Optional[Callable[..., Any]] = None,
    logs_fn: Optional[Callable[..., Any]] = None,
    metrics_fn: Optional[Callable[..., Any]] = None,
    platform_events_fn: Optional[Callable[..., Any]] = None,
    config: Optional[AgentConfig] = None,
    prior_quiescent_minutes: Optional[int] = None,
) -> OnsetDetectionResult:
    """Inspect recent telemetry to detect actual symptom onset and separate prior incidents.

    Scans the lookback window in discrete intervals to find the earliest timestamp
    where error rates or latency deviated from normal baseline. If an earlier error
    spike subsided with >= prior_quiescent_minutes of healthy traffic before onset,
    it is tagged as a prior incident.
    """
    cfg = config or get_settings()
    eff_window_minutes = (
        window_minutes if window_minutes is not None else getattr(cfg, "onset_window_minutes", 60)
    )
    eff_pre_buffer = (
        pre_buffer_minutes
        if pre_buffer_minutes is not None
        else getattr(cfg, "onset_pre_buffer_minutes", 10)
    )
    eff_quiescent_minutes = (
        prior_quiescent_minutes
        if prior_quiescent_minutes is not None
        else getattr(cfg, "prior_incident_quiescent_minutes", 5)
    )
    eff_project_id = project_id or getattr(cfg, "gcp_project", None)

    resolved_end = normalize_datetime(end_time) or datetime.now(timezone.utc)
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=timezone.utc)
    else:
        resolved_end = resolved_end.astimezone(timezone.utc)

    scan_start = resolved_end - timedelta(minutes=eff_window_minutes)

    # Telemetry error event collection: (timestamp, error_kind, description)
    error_events: List[tuple[datetime, str, str]] = []

    # 1. Query Request Logs for 4xx / 5xx / 403 / 500 errors and high-latency requests
    try:
        query_fn = request_logs_fn
        if query_fn is None:
            from .tools.logs import query_request_logs

            query_fn = query_request_logs

        req_results: List[Any] = []
        # Query 1: HTTP 4xx/5xx errors
        try:
            req_results.append(
                query_fn(
                    service_name=service_name,
                    project_id=eff_project_id,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                    status_class="400-599",
                    max_entries=200,
                )
            )
        except TypeError:
            req_results.append(
                query_fn(
                    service_name=service_name,
                    project_id=eff_project_id,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                )
            )

        # Query 2: requests taking > 2000ms
        try:
            req_results.append(
                query_fn(
                    service_name=service_name,
                    project_id=eff_project_id,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                    min_latency_ms=2000,
                    max_entries=200,
                )
            )
        except TypeError:
            try:
                req_results.append(
                    query_fn(
                        service_name=service_name,
                        project_id=eff_project_id,
                        start_time=scan_start,
                        end_time=resolved_end,
                        client=logging_client,
                        max_entries=200,
                    )
                )
            except TypeError:
                pass

        for req_data in req_results:
            if isinstance(req_data, dict):
                for item in req_data.get("sample_entries", []):
                    status_code = item.get("status")
                    if status_code is None:
                        status_code = item.get("status_code", 0)
                    try:
                        status_code = int(status_code)
                    except (ValueError, TypeError):
                        status_code = 0
                    latency_ms = item.get("latency_ms", 0)
                    route = item.get("route", "")
                    is_symptom = (
                        status_code >= 500
                        or status_code == 503
                        or status_code == 403
                        or (route != "/health" and latency_ms > 2000)
                    )
                    if is_symptom:
                        raw_ts = item.get("timestamp")
                        ts = normalize_datetime(raw_ts)
                        if ts:
                            error_events.append(
                                (
                                    ts,
                                    f"HTTP {status_code}",
                                    item.get("excerpt", f"Status {status_code}"),
                                )
                            )
    except Exception as exc:
        logger.debug("Onset detection request log query skipped or failed: %s", exc)

    # 2. Query Platform Events (OOM terminations, restarts, probe failures)
    try:
        plat_fn = platform_events_fn
        if plat_fn is None:
            from .tools.resources import get_platform_events

            plat_fn = get_platform_events

        try:
            plat_res = plat_fn(
                service_name=service_name,
                project_id=eff_project_id,
                start_time=scan_start,
                end_time=resolved_end,
                logging_client=logging_client,
            )
        except TypeError:
            try:
                plat_res = plat_fn(
                    service_name=service_name,
                    start_time=scan_start,
                    end_time=resolved_end,
                )
            except TypeError:
                plat_res = plat_fn(service_name=service_name)

        if isinstance(plat_res, dict):
            plat_events: List[Any] = list(plat_res.get("events", []))
            if not plat_events:
                for cat in ("restarts", "probe_failures", "system_events"):
                    sub = plat_res.get(cat)
                    if isinstance(sub, dict):
                        plat_events.extend(sub.get("events", []))
                    elif isinstance(sub, list):
                        plat_events.extend(sub)
            for pe in plat_events:
                if isinstance(pe, dict):
                    raw_ts = pe.get("timestamp")
                    ts = normalize_datetime(raw_ts)
                    if not ts:
                        continue

                    message = str(pe.get("message") or "")
                    reason = str(pe.get("reason") or "")
                    event_name = str(pe.get("event") or "")
                    probe_type = str(pe.get("probe_type") or "")
                    exit_code = pe.get("exit_code")
                    exit_code_str = f"exit code {exit_code}" if exit_code is not None else ""
                    combined_text = (
                        f"{message} {reason} {event_name} {probe_type} {exit_code_str}".lower()
                    )

                    # Strictly drop benign platform operational messages
                    # (e.g. autoscaling, probe succeeded)
                    if any(b in combined_text for b in BENIGN_PLATFORM_EVENT_PATTERNS):
                        continue

                    # Keep all OOM kills, container terminations/exits, and probe
                    # failures as symptoms
                    is_symptom = (
                        any(s in combined_text for s in SYMPTOM_PLATFORM_EVENT_PATTERNS)
                        or exit_code == 137
                    )
                    if not is_symptom:
                        continue

                    event_desc = (
                        pe.get("message") or pe.get("event") or pe.get("reason") or "Platform event"
                    )
                    error_events.append((ts, "Platform Event", event_desc))
    except Exception as exc:
        logger.debug("Onset platform event query skipped or failed: %s", exc)

    # 3. Query Application and System Logs (severity >= WARNING / ERROR)
    try:
        if logs_fn is not None:
            try:
                log_data = logs_fn(
                    service_name=service_name,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                    order_by="timestamp desc",
                    max_entries=200,
                )
            except TypeError:
                log_data = logs_fn(
                    service_name=service_name,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                )
        else:
            from .tools.logs import query_logs

            log_filter = (
                f'resource.type = "cloud_run_revision" AND severity >= WARNING '
                f'AND resource.labels.service_name = "{service_name}"'
            )
            try:
                log_data = query_logs(
                    filter_str=log_filter,
                    resource_names=[f"projects/{eff_project_id}"] if eff_project_id else [],
                    max_entries=200,
                    order_by="timestamp desc",
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                )
            except TypeError:
                log_data = query_logs(
                    filter_str=log_filter,
                    resource_names=[f"projects/{eff_project_id}"] if eff_project_id else [],
                    max_entries=200,
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=logging_client,
                )

        if isinstance(log_data, dict):
            for entry in log_data.get("entries", []):
                sev = str(entry.get("severity", "")).upper()
                if sev in ["ERROR", "CRITICAL", "WARNING"]:
                    raw_ts = entry.get("timestamp")
                    ts = normalize_datetime(raw_ts)
                    if ts:
                        error_events.append(
                            (
                                ts,
                                f"Log {sev}",
                                entry.get("payload_excerpt", ""),
                            )
                        )
    except Exception as exc:
        logger.debug("Onset detection log query skipped or failed: %s", exc)

    # Sort collected error events chronologically
    error_events.sort(key=lambda x: x[0])

    # Record timestamps of corroborating request logs, error logs, and platform events
    # before metric evaluation
    corroborating_log_times: List[datetime] = [ev[0] for ev in error_events]

    # 4. Query Cloud Monitoring Metrics for error rates or latency deviations
    candidate_latency_points: List[tuple[datetime, float]] = []
    try:
        if metrics_fn is not None:
            met_data = metrics_fn(
                service_name=service_name,
                start_time=scan_start,
                end_time=resolved_end,
                client=metrics_client,
            )
            if isinstance(met_data, dict):
                for pt in met_data.get("points", []):
                    val = float(pt.get("value", 0.0))
                    kind = str(pt.get("kind", ""))
                    raw_ts = pt.get("timestamp")
                    ts = normalize_datetime(raw_ts)
                    if ts:
                        is_latency = "latency" in kind.lower() or (
                            val > 2000.0 and "5xx" not in kind.lower()
                        )
                        if is_latency:
                            if val > 2000.0:
                                candidate_latency_points.append((ts, val))
                        elif val > 0.0:
                            error_events.append(
                                (
                                    ts,
                                    "Metric 5xx spike",
                                    f"Value: {val}",
                                )
                            )
        else:
            from .tools.metrics import query_time_series

            try:
                met_data = query_time_series(
                    service=service_name,
                    status_code_range="5xx",
                    start_time=scan_start,
                    end_time=resolved_end,
                    client=metrics_client,
                    project_id=eff_project_id,
                )
                if isinstance(met_data, dict):
                    for pt in met_data.get("points", []):
                        val = float(pt.get("value", 0.0))
                        if val > 0.0:
                            raw_ts = pt.get("timestamp")
                            ts = normalize_datetime(raw_ts)
                            if ts:
                                error_events.append(
                                    (
                                        ts,
                                        "Metric 5xx spike",
                                        f"Value: {val}",
                                    )
                                )
            except Exception as e5xx:
                logger.debug("5xx metric query failed: %s", e5xx)

            try:
                lat_filter = (
                    f'metric.type = "run.googleapis.com/request_latencies" '
                    f'AND resource.labels.service_name = "{service_name}"'
                )
                lat_data = query_time_series(
                    filter_str=lat_filter,
                    start_time=scan_start,
                    end_time=resolved_end,
                    per_series_aligner="ALIGN_PERCENTILE_99",
                    client=metrics_client,
                    project_id=eff_project_id,
                )
                if isinstance(lat_data, dict):
                    for pt in lat_data.get("points", []):
                        val = float(pt.get("value", 0.0))
                        if val > 2000.0:
                            raw_ts = pt.get("timestamp")
                            ts = normalize_datetime(raw_ts)
                            if ts:
                                candidate_latency_points.append((ts, val))
            except Exception as e_lat:
                logger.debug("Latency metric query failed: %s", e_lat)
    except Exception as exc:
        logger.debug("Onset detection metric query skipped or failed: %s", exc)

    # Filter candidate latency metric points (> 2000 ms) for sustained deviation
    if candidate_latency_points:
        distinct_lat_times = sorted({pt[0] for pt in candidate_latency_points})
        sustained_lat_times: set[datetime] = set()

        if len(distinct_lat_times) >= 2:
            for i, cur_t in enumerate(distinct_lat_times):
                has_prev = i > 0 and (cur_t - distinct_lat_times[i - 1]) <= timedelta(minutes=3)
                has_next = i < len(distinct_lat_times) - 1 and (
                    distinct_lat_times[i + 1] - cur_t
                ) <= timedelta(minutes=3)
                if has_prev or has_next:
                    sustained_lat_times.add(cur_t)

        for pt_ts, pt_val in candidate_latency_points:
            is_sustained = pt_ts in sustained_lat_times
            is_corroborated = any(
                abs(pt_ts - log_ts) <= timedelta(minutes=3) for log_ts in corroborating_log_times
            )

            if is_sustained or is_corroborated:
                error_events.append(
                    (
                        pt_ts,
                        "Latency metric surge",
                        f"P99 latency: {pt_val:.1f}ms",
                    )
                )
            else:
                logger.debug(
                    "Discarded isolated latency metric blip at %s (val=%.1fms)",
                    pt_ts.isoformat(),
                    pt_val,
                )

    # Deduplicate error_events by timestamp and description
    deduped_events: List[tuple[datetime, str, str]] = []
    seen_events: set[tuple[datetime, str]] = set()
    for ev in error_events:
        key = (ev[0], ev[2])
        if key not in seen_events:
            seen_events.add(key)
            deduped_events.append(ev)
    error_events = deduped_events

    # Fallback path if no telemetry errors observed
    if not error_events:
        fallback_start = max(
            scan_start,
            resolved_end - timedelta(hours=getattr(cfg, "max_query_window_hours", 24)),
        )
        return OnsetDetectionResult(
            onset_time=None,
            anchored_start_time=fallback_start,
            end_time=resolved_end,
            is_fallback=True,
            prior_incidents=[],
            summary="No error spike observed in telemetry; fell back to ticket lookback.",
        )

    # Sort events chronologically
    error_events.sort(key=lambda x: x[0])

    # Cluster into episodes based on quiescent gaps (>= prior_quiescent_minutes)
    episodes: List[List[tuple[datetime, str, str]]] = []
    current_ep: List[tuple[datetime, str, str]] = []

    for ev in error_events:
        if not current_ep:
            current_ep.append(ev)
        else:
            prev_ts = current_ep[-1][0]
            if (ev[0] - prev_ts) > timedelta(minutes=eff_quiescent_minutes):
                episodes.append(current_ep)
                current_ep = [ev]
            else:
                current_ep.append(ev)

    if current_ep:
        episodes.append(current_ep)

    # The active incident is the latest episode
    active_ep = episodes[-1]
    onset_time = active_ep[0][0]

    # Prior incidents are any earlier episodes that ended before onset_time
    prior_incidents: List[PriorIncident] = []
    for ep in episodes[:-1]:
        ep_start = ep[0][0]
        ep_end = ep[-1][0]
        prior_incidents.append(
            PriorIncident(
                start_time=ep_start,
                end_time=ep_end,
                error_count=len(ep),
                description=(
                    f"Resolved error episode from {ep_start.strftime('%H:%M:%SZ')} "
                    f"to {ep_end.strftime('%H:%M:%SZ')} ({len(ep)} events)"
                ),
            )
        )

    # Anchor investigation window: onset_time - pre_buffer_minutes
    anchored_start = onset_time - timedelta(minutes=eff_pre_buffer)
    max_window_delta = timedelta(hours=getattr(cfg, "max_query_window_hours", 24))
    if (resolved_end - anchored_start) > max_window_delta:
        anchored_start = resolved_end - max_window_delta

    summary = (
        f"Detected symptom onset at {onset_time.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"with {len(active_ep)} error events; anchored start at "
        f"{anchored_start.strftime('%Y-%m-%dT%H:%M:%SZ')} (-{eff_pre_buffer}m buffer)."
    )
    if prior_incidents:
        summary += f" Identified {len(prior_incidents)} prior resolved episode(s)."

    return OnsetDetectionResult(
        onset_time=onset_time,
        anchored_start_time=anchored_start,
        end_time=resolved_end,
        is_fallback=False,
        prior_incidents=prior_incidents,
        summary=summary,
    )
