import math
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

from .clients import (
    get_monitoring_alert_client,
    get_monitoring_metric_client,
    is_local_mode,
)
from .registry import readonly_tool


def _calculate_mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _calculate_p95(values: List[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(math.ceil(0.95 * len(sorted_vals))) - 1
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return sorted_vals[idx]


def _extract_metric_values(raw_series: Any) -> List[float]:
    values: List[float] = []
    if raw_series is None:
        return values
    for ts in raw_series:
        points = getattr(ts, "points", [])
        for pt in points:
            val = getattr(pt, "value", None)
            if val is not None and hasattr(val, "double_value"):
                values.append(float(val.double_value))
            elif val is not None and hasattr(val, "int64_value"):
                values.append(float(val.int64_value))
            elif isinstance(val, dict):
                v = (
                    val.get("doubleValue")
                    or val.get("double_value")
                    or val.get("int64Value")
                    or val.get("int64_value")
                )
                if v is not None:
                    values.append(float(v))
            elif isinstance(val, (int, float)):
                values.append(float(val))
            elif isinstance(pt, (int, float)):
                values.append(float(pt))
    return values


def _build_aggregation(
    alignment_period_seconds: int = 60,
    per_series_aligner: Optional[str] = "ALIGN_RATE",
    cross_series_reducer: Optional[str] = "REDUCE_SUM",
    group_by_fields: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build Cloud Monitoring aggregation dictionary for delta metric alignment and reduction."""
    if not per_series_aligner or per_series_aligner == "ALIGN_NONE":
        return None
    agg: Dict[str, Any] = {
        "alignment_period": {"seconds": alignment_period_seconds},
        "per_series_aligner": per_series_aligner,
    }
    if cross_series_reducer and cross_series_reducer != "REDUCE_NONE":
        agg["cross_series_reducer"] = cross_series_reducer
    if group_by_fields:
        agg["group_by_fields"] = group_by_fields
    return agg


def _expand_service_name_filter(filter_str: str) -> str:
    """Expand resource.labels.service_name equality to candidate names using one_of if needed."""
    if "one_of" in filter_str:
        return filter_str
    import re

    from .common import get_candidate_service_names

    match = re.search(r'resource\.labels\.service_name\s*=\s*"([^"]+)"', filter_str)
    if match:
        svc = match.group(1)
        candidates = get_candidate_service_names(svc)
        if len(candidates) > 1:
            cand_str = ", ".join(f'"{c}"' for c in candidates)
            return filter_str.replace(
                match.group(0), f"resource.labels.service_name = one_of({cand_str})"
            )
    return filter_str


@readonly_tool(
    name="query_time_series",
    description="Query Cloud Monitoring time series for metrics matching a filter.",
)
def query_time_series(
    filter_str: str,
    project_id: str,
    start_time: datetime,
    end_time: datetime,
    alignment_period_seconds: int = 60,
    per_series_aligner: str = "ALIGN_RATE",
    cross_series_reducer: Optional[str] = "REDUCE_SUM",
    group_by_fields: Optional[List[str]] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Query Cloud Monitoring metric time series with compact output and console deep link."""
    effective_filter = _expand_service_name_filter(filter_str)
    encoded_filter = urllib.parse.quote(effective_filter)
    console_url = (
        f"https://console.cloud.google.com/monitoring/metrics-explorer"
        f"?project={project_id}&pageState={encoded_filter}"
    )

    if client is None:
        client = get_monitoring_metric_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Monitoring client unavailable in production environment")

    points_data: List[Dict[str, Any]] = []

    if client is not None:
        request_payload: Dict[str, Any] = {
            "name": f"projects/{project_id}",
            "filter": effective_filter,
            "interval": {
                "start_time": start_time,
                "end_time": end_time,
            },
        }
        agg = _build_aggregation(
            alignment_period_seconds=alignment_period_seconds,
            per_series_aligner=per_series_aligner,
            cross_series_reducer=cross_series_reducer,
            group_by_fields=group_by_fields,
        )
        if agg:
            request_payload["aggregation"] = agg

        try:
            raw_series = client.list_time_series(request=request_payload)
        except (TypeError, ValueError, AttributeError):
            try:
                raw_series = client.list_time_series(
                    name=f"projects/{project_id}",
                    filter=filter_str,
                    interval={
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                    alignment_period_seconds=alignment_period_seconds,
                )
            except Exception:
                raw_series = []

        for ts in raw_series:
            metric_type = getattr(getattr(ts, "metric", None), "type", "unknown")
            pts = getattr(ts, "points", [])
            for pt in pts:
                ts_time = getattr(pt, "interval", None)
                end_time_val = getattr(ts_time, "end_time", None) if ts_time else None
                if end_time_val is not None and hasattr(end_time_val, "isoformat"):
                    ts_str = str(end_time_val.isoformat())
                else:
                    ts_str = str(end_time_val or "")

                val_obj = getattr(pt, "value", None)
                if val_obj is not None and hasattr(val_obj, "double_value"):
                    numeric_val = float(val_obj.double_value)
                elif val_obj is not None and hasattr(val_obj, "int64_value"):
                    numeric_val = float(val_obj.int64_value)
                elif isinstance(val_obj, dict):
                    v = (
                        val_obj.get("doubleValue")
                        or val_obj.get("double_value")
                        or val_obj.get("int64Value")
                        or val_obj.get("int64_value")
                        or 0.0
                    )
                    numeric_val = float(v)
                elif isinstance(val_obj, (int, float)):
                    numeric_val = float(val_obj)
                else:
                    numeric_val = 0.0

                points_data.append(
                    {
                        "metric": metric_type,
                        "timestamp": ts_str,
                        "value": round(numeric_val, 4),
                    }
                )

    return {
        "metric_filter": filter_str,
        "project_id": project_id,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "alignment_period_seconds": alignment_period_seconds,
        "total_points": len(points_data),
        "points": points_data[:100],  # compact truncated output
        "console_url": console_url,
    }


@readonly_tool(
    name="compare_to_baseline",
    description="Compute baseline metric comparison in code (mean, p95, percent change).",
)
def compare_to_baseline(
    filter_str: str,
    project_id: str,
    current_start: datetime,
    current_end: datetime,
    baseline_start: datetime,
    baseline_end: datetime,
    alignment_period_seconds: int = 60,
    per_series_aligner: str = "ALIGN_RATE",
    cross_series_reducer: Optional[str] = "REDUCE_SUM",
    group_by_fields: Optional[List[str]] = None,
    client: Any = None,
) -> Dict[str, Any]:
    """Compute baseline comparison in code for mean, p95, and percentage shifts."""
    console_url = (
        f"https://console.cloud.google.com/monitoring/metrics-explorer?project={project_id}"
    )

    if client is None:
        client = get_monitoring_metric_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Monitoring client unavailable in production environment")

    effective_filter = _expand_service_name_filter(filter_str)
    current_values: List[float] = []
    baseline_values: List[float] = []

    agg = _build_aggregation(
        alignment_period_seconds=alignment_period_seconds,
        per_series_aligner=per_series_aligner,
        cross_series_reducer=cross_series_reducer,
        group_by_fields=group_by_fields,
    )

    if client is not None:
        req_current: Dict[str, Any] = {
            "name": f"projects/{project_id}",
            "filter": effective_filter,
            "interval": {"start_time": current_start, "end_time": current_end},
        }
        if agg:
            req_current["aggregation"] = agg

        try:
            raw_current = client.list_time_series(request=req_current)
        except (TypeError, ValueError, AttributeError):
            try:
                raw_current = client.list_time_series(
                    name=f"projects/{project_id}",
                    filter=effective_filter,
                    interval={"start_time": current_start, "end_time": current_end},
                )
            except Exception:
                raw_current = []
        current_values = _extract_metric_values(raw_current)

        req_baseline: Dict[str, Any] = {
            "name": f"projects/{project_id}",
            "filter": effective_filter,
            "interval": {"start_time": baseline_start, "end_time": baseline_end},
        }
        if agg:
            req_baseline["aggregation"] = agg

        try:
            raw_baseline = client.list_time_series(request=req_baseline)
        except (TypeError, ValueError, AttributeError):
            try:
                raw_baseline = client.list_time_series(
                    name=f"projects/{project_id}",
                    filter=filter_str,
                    interval={"start_time": baseline_start, "end_time": baseline_end},
                )
            except Exception:
                raw_baseline = []
        baseline_values = _extract_metric_values(raw_baseline)

    curr_mean = _calculate_mean(current_values)
    base_mean = _calculate_mean(baseline_values)
    curr_p95 = _calculate_p95(current_values)
    base_p95 = _calculate_p95(baseline_values)

    is_anomalous = False

    if base_mean == 0.0 and curr_mean > 0.0:
        mean_change_percent: Optional[float] = None
        is_anomalous = True
    elif base_mean != 0.0:
        mean_change_percent = (curr_mean - base_mean) / base_mean * 100.0
    else:
        mean_change_percent = 0.0

    if base_p95 == 0.0 and curr_p95 > 0.0:
        p95_change_percent: Optional[float] = None
        is_anomalous = True
    elif base_p95 != 0.0:
        p95_change_percent = (curr_p95 - base_p95) / base_p95 * 100.0
    else:
        p95_change_percent = 0.0

    if not is_anomalous:
        is_anomalous = (mean_change_percent is not None and abs(mean_change_percent) >= 20.0) or (
            p95_change_percent is not None and abs(p95_change_percent) >= 20.0
        )

    return {
        "metric_filter": filter_str,
        "project_id": project_id,
        "current_period": {
            "start": current_start.isoformat(),
            "end": current_end.isoformat(),
            "mean": round(curr_mean, 4),
            "p95": round(curr_p95, 4),
            "sample_count": len(current_values),
        },
        "baseline_period": {
            "start": baseline_start.isoformat(),
            "end": baseline_end.isoformat(),
            "mean": round(base_mean, 4),
            "p95": round(base_p95, 4),
            "sample_count": len(baseline_values),
        },
        "mean_change_percent": (
            round(mean_change_percent, 2) if mean_change_percent is not None else None
        ),
        "p95_change_percent": (
            round(p95_change_percent, 2) if p95_change_percent is not None else None
        ),
        "is_anomalous": is_anomalous,
        "console_url": console_url,
    }


@readonly_tool(
    name="list_alert_policies",
    description="List Cloud Monitoring alerting policies and their active conditions.",
)
def list_alert_policies(
    project_id: str,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List Cloud Monitoring alert policies."""
    if client is None:
        client = get_monitoring_alert_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Monitoring alert client unavailable in production environment")

    policies: List[Dict[str, Any]] = []
    if client is not None:
        raw_policies = client.list_alert_policies(name=f"projects/{project_id}")
        for pol in raw_policies:
            conditions_summary: List[Dict[str, Any]] = []
            for cond in getattr(pol, "conditions", []):
                cond_thresh = getattr(cond, "condition_threshold", None)
                filt = getattr(cond_thresh, "filter", "") if cond_thresh else ""
                conditions_summary.append(
                    {
                        "name": getattr(cond, "name", ""),
                        "display_name": getattr(cond, "display_name", ""),
                        "filter": filt,
                    }
                )
            policies.append(
                {
                    "name": getattr(pol, "name", ""),
                    "display_name": getattr(pol, "display_name", ""),
                    "enabled": getattr(pol, "enabled", True),
                    "conditions": conditions_summary,
                }
            )
    return policies


@readonly_tool(
    name="list_open_alerts",
    description="List active/open incidents and alerts in Cloud Monitoring.",
)
def list_open_alerts(
    project_id: str,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """List open alerts and incident events."""
    if client is None:
        client = get_monitoring_alert_client()

    if client is None and not is_local_mode():
        raise RuntimeError("GCP Monitoring alert client unavailable in production environment")

    alerts: List[Dict[str, Any]] = []
    if client is not None:
        if hasattr(client, "list_incidents"):
            raw_incidents = client.list_incidents(name=f"projects/{project_id}")
            for inc in raw_incidents:
                alerts.append(
                    {
                        "incident_id": getattr(inc, "incident_id", getattr(inc, "name", "")),
                        "policy_name": getattr(inc, "policy_name", ""),
                        "condition_name": getattr(inc, "condition_name", ""),
                        "started_at": str(getattr(inc, "started_at", "")),
                        "summary": getattr(inc, "summary", "")[:256],
                        "state": getattr(inc, "state", "OPEN"),
                    }
                )
    return alerts
