from datetime import datetime
from typing import Any, List, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..prompts import load_prompt
from ..tools.metrics import (
    compare_to_baseline,
    list_alert_policies,
    list_open_alerts,
    query_time_series,
)
from .base import BaseSpecialist

METRIC_METADATA: dict[str, dict[str, str]] = {
    "run.googleapis.com/request_latencies": {"unit": "ms", "type": "latency"},
    "run.googleapis.com/request_count": {"unit": "/s", "type": "rate"},
    "run.googleapis.com/container/memory/utilizations": {"unit": "%", "type": "ratio"},
    "run.googleapis.com/container/cpu/utilizations": {"unit": "%", "type": "ratio"},
}


def get_metric_metadata(metric_key: str) -> dict[str, str]:
    """Retrieve explicit unit and type metadata for a metric, avoiding magnitude heuristics."""
    if metric_key in METRIC_METADATA:
        return METRIC_METADATA[metric_key]
    for key, meta in METRIC_METADATA.items():
        if key in metric_key:
            return meta
    return {"unit": "", "type": "unknown"}


def canonical_metric_value(
    val: float, metric_type: str = "run.googleapis.com/request_latencies"
) -> float:
    """Return the canonical metric value without magnitude guessing.

    Cloud Monitoring run.googleapis.com/request_latencies alignment (e.g.
    ALIGN_PERCENTILE_95) returns values already in milliseconds. Magnitude-based
    scaling (e.g. multiplying by 1000 if <= 100) is strictly eliminated.
    """
    if val <= 0.0:
        return 0.0
    return val


def _to_ms(val: float) -> float:
    """Canonical millisecond conversion: Cloud Monitoring request_latencies values
    are already in milliseconds. Magnitude-based heuristics are strictly eliminated.
    """
    return canonical_metric_value(val, "run.googleapis.com/request_latencies")


def format_metric_value(val: float, metric_or_unit: str) -> str:
    """Format a metric value cleanly using its explicit unit without magnitude guessing."""
    meta = get_metric_metadata(metric_or_unit)
    unit = meta.get("unit") or metric_or_unit
    mtype = meta.get("type", "unknown")

    if mtype == "ratio" or unit == "%":
        return f"{val * 100.0:.1f}%"
    elif mtype == "latency" or unit == "ms":
        return f"{val:.1f}ms"
    elif mtype == "rate" or unit == "/s":
        if val == 0.0:
            return f"0.0{unit}"
        return f"{val:.4f}{unit}"
    elif unit:
        if val == int(val):
            return f"{int(val)}{unit}"
        return f"{val:.4f}{unit}"
    else:
        return f"{val:.4f}"


def _format_ms(val: float) -> str:
    """Format a latency value in milliseconds cleanly."""
    return format_metric_value(val, "run.googleapis.com/request_latencies")


class MetricsSpecialist(BaseSpecialist):
    """Specialist analyzing Cloud Monitoring telemetry and baseline comparisons."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_specialist"):
                model = config.model_specialist
            else:
                try:
                    from ..config import get_settings

                    model = get_settings().model_specialist
                except Exception:
                    model = ""

        adk_tools: list[Any] = [
            FunctionTool(query_time_series),
            FunctionTool(compare_to_baseline),
            FunctionTool(list_alert_policies),
            FunctionTool(list_open_alerts),
        ]
        self.adk_tools = adk_tools
        instruction = load_prompt("metrics")
        agent = llm_agent or LlmAgent(
            name="metrics_specialist",
            model=model or "default",
            instruction=instruction,
            tools=adk_tools,
            output_schema=Findings,
        )
        super().__init__(
            name="MetricsSpecialist",
            description="Analyzes time series telemetry, anomaly detection, and alert statuses.",
            project_id=project_id,
            client=client,
            llm_agent=agent,
            config=config,
        )

    def get_tools(self) -> List[Any]:
        return list(self.adk_tools)

    def run_investigation(
        self,
        incident: Incident,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> Findings:
        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        effective_service = incident.service_hint or (
            getattr(kwargs.get("plan"), "affected_service", None)
            if kwargs.get("plan")
            and getattr(kwargs.get("plan"), "affected_service", None) != "unknown-service"
            else None
        )
        service = effective_service or "service"

        # 1. Check open alerts
        try:
            alerts = list_open_alerts(project_id=self.project_id, client=self.client)
            queries_run.append("list_open_alerts()")
            for alert in alerts:
                anomalies.append(f"Open alert: {alert.get('policy_name')} ({alert.get('summary')})")
                evidence.append(
                    self.create_evidence_item(
                        kind="alert_incident",
                        excerpt=(
                            f"Alert {alert.get('incident_id')} on policy "
                            f"{alert.get('policy_name')}: {alert.get('summary')}"
                        ),
                        timestamp=start_time,
                    )
                )
        except Exception as e:
            errors.append(f"Failed to list open alerts: {str(e)}")

        # 2. Compare to baseline for both 5xx error rate and request latencies
        duration = end_time - start_time
        baseline_end = start_time
        baseline_start = baseline_end - duration

        symptoms_text = f"{incident.title} {incident.description} {' '.join(incident.labels or [])}"
        if "plan" in kwargs and hasattr(kwargs["plan"], "symptoms"):
            symptoms_text += " " + " ".join(kwargs["plan"].symptoms or [])
        is_latency_incident = any(
            kw in symptoms_text.lower()
            for kw in ["latency", "slow", "slowness", "timeout", "timing out", "delay", "lag"]
        )

        service_filter_clause = ""
        if effective_service:
            from ..tools.common import get_candidate_service_names

            candidates = get_candidate_service_names(
                effective_service, getattr(self.config, "name_prefix", None)
            )
            if len(candidates) == 1:
                service_filter_clause = f' AND resource.labels.service_name = "{candidates[0]}"'
            elif len(candidates) > 1:
                cand_str = ", ".join(f'"{c}"' for c in candidates)
                service_filter_clause = f" AND resource.labels.service_name = one_of({cand_str})"

        error_metric_filter = (
            'metric.type = "run.googleapis.com/request_count" AND '
            f'metric.labels.response_code_class = "5xx"{service_filter_clause}'
        )
        latency_metric_filter = (
            f'metric.type = "run.googleapis.com/request_latencies"{service_filter_clause}'
        )

        def _investigate_latency() -> tuple[list[EvidenceItem], list[str]]:
            lat_ev: List[EvidenceItem] = []
            lat_anom: List[str] = []
            try:
                comp = compare_to_baseline(
                    filter_str=latency_metric_filter,
                    project_id=self.project_id,
                    current_start=start_time,
                    current_end=end_time,
                    baseline_start=baseline_start,
                    baseline_end=baseline_end,
                    alignment_period_seconds=60,
                    per_series_aligner="ALIGN_PERCENTILE_95",
                    cross_series_reducer="REDUCE_PERCENTILE_95",
                    client=self.client,
                )
                queries_run.append(f"compare_to_baseline({latency_metric_filter})")

                curr_period = comp.get("current_period", {})
                curr_p95_raw = float(curr_period.get("p95", 0.0) or curr_period.get("mean", 0.0))
                base_period = comp.get("baseline_period", {})
                base_p95_raw = float(base_period.get("p95", 0.0) or base_period.get("mean", 0.0))

                curr_p95_ms = canonical_metric_value(
                    curr_p95_raw, "run.googleapis.com/request_latencies"
                )
                base_p95_ms = canonical_metric_value(
                    base_p95_raw, "run.googleapis.com/request_latencies"
                )

                if base_p95_ms > 0:
                    shift = ((curr_p95_ms - base_p95_ms) / base_p95_ms) * 100.0
                    shift_str = f"{shift:+.1f}%"
                else:
                    shift = None
                    shift_str = "new signal"

                # Check for significant elevation: >2x baseline or current > 2.0s (2000ms)
                is_elevated = (
                    (base_p95_ms > 0 and curr_p95_ms >= 2.0 * base_p95_ms)
                    or (curr_p95_ms > 2000.0)
                    or (base_p95_ms == 0 and curr_p95_ms > 500.0)
                )

                curr_p95_str = format_metric_value(
                    curr_p95_ms, "run.googleapis.com/request_latencies"
                )
                base_p95_str = format_metric_value(
                    base_p95_ms, "run.googleapis.com/request_latencies"
                )

                if is_elevated:
                    if base_p95_ms > 0:
                        lat_anom.append(
                            f"Elevated request latency detected on {service}: "
                            f"p95 latency increased by {shift_str} "
                            f"(from {base_p95_str} to {curr_p95_str})"
                        )
                    else:
                        lat_anom.append(
                            f"Elevated request latency detected on {service}: p95 latency reached "
                            f"{curr_p95_str} (baseline zero)"
                        )
                elif comp.get("is_anomalous", False):
                    lat_anom.append(
                        f"Latency anomaly on {service}: p95 latency {curr_p95_str} "
                        f"(shift: {shift_str} from {base_p95_str})"
                    )

                lat_ev.append(
                    self.create_evidence_item(
                        kind="latency_metric",
                        excerpt=(
                            f"Metric {latency_metric_filter}: "
                            f"current p95={curr_p95_str} "
                            f"vs baseline p95={base_p95_str} ({shift_str})"
                        ),
                        timestamp=start_time,
                        console_url=comp.get("console_url"),
                    )
                )
            except Exception as e:
                errors.append(f"Failed latency baseline comparison: {str(e)}")
            return lat_ev, lat_anom

        def _investigate_error_rate() -> tuple[list[EvidenceItem], list[str]]:
            err_ev: List[EvidenceItem] = []
            err_anom: List[str] = []
            aligner = "ALIGN_RATE"
            try:
                comp = compare_to_baseline(
                    filter_str=error_metric_filter,
                    project_id=self.project_id,
                    current_start=start_time,
                    current_end=end_time,
                    baseline_start=baseline_start,
                    baseline_end=baseline_end,
                    alignment_period_seconds=60,
                    per_series_aligner=aligner,
                    cross_series_reducer="REDUCE_SUM",
                    client=self.client,
                )
                queries_run.append(f"compare_to_baseline({error_metric_filter})")

                mean_shift = comp.get("mean_change_percent")
                p95_shift = comp.get("p95_change_percent")
                is_anom = comp.get("is_anomalous", False)

                curr_period = comp.get("current_period", {})
                curr_mean = float(curr_period.get("mean", 0.0))
                base_mean = float(comp.get("baseline_period", {}).get("mean", 0.0))
                sample_count = curr_period.get("sample_count", 0)

                curr_mean_str = format_metric_value(curr_mean, "run.googleapis.com/request_count")
                base_mean_str = format_metric_value(base_mean, "run.googleapis.com/request_count")

                if base_mean == 0.0 and curr_mean > 0.0:
                    excerpt_text = (
                        f"Metric {error_metric_filter}: current mean={curr_mean_str} "
                        f"vs baseline mean={base_mean_str} (new signal, baseline zero)"
                    )
                    err_anom.append(
                        f"New error signal detected: {error_metric_filter} "
                        f"rate {curr_mean_str} over {base_mean_str} baseline"
                    )
                else:
                    mean_str = (
                        f"{mean_shift:+.1f}% shift" if mean_shift is not None else "new signal"
                    )
                    p95_str = (
                        f"p95 shift={p95_shift:+.1f}%"
                        if p95_shift is not None
                        else "p95 shift=new signal"
                    )
                    excerpt_text = (
                        f"Metric {error_metric_filter}: current mean={curr_mean_str} "
                        f"vs baseline mean={base_mean_str} ({mean_str}, {p95_str})"
                    )
                    if curr_mean > 0.0 or is_anom:
                        err_anom.append(
                            f"Statistically significant divergence in {error_metric_filter}: "
                            f"current mean={curr_mean_str} "
                            f"(mean shift {mean_str}, {p95_str})"
                        )
                    elif sample_count > 0:
                        err_anom.append(
                            f"Baseline comparison for {error_metric_filter} "
                            f"returned {base_mean_str} mean across {sample_count} samples "
                            f"(no 5xx rate detected in metrics window)"
                        )

                err_ev.append(
                    self.create_evidence_item(
                        kind="baseline_comparison",
                        excerpt=excerpt_text,
                        timestamp=start_time,
                        console_url=comp.get("console_url"),
                    )
                )
            except Exception as e:
                errors.append(f"Failed error rate baseline comparison: {str(e)}")
            return err_ev, err_anom

        is_memory_incident = any(
            kw in symptoms_text.lower() for kw in ["memory", "oom", "leak", "exhaustion"]
        )

        memory_metric_filter = (
            'metric.type = "run.googleapis.com/container/memory/utilizations"'
            f"{service_filter_clause}"
        )

        def _investigate_memory() -> tuple[list[EvidenceItem], list[str]]:
            mem_ev: List[EvidenceItem] = []
            mem_anom: List[str] = []
            try:
                comp = compare_to_baseline(
                    filter_str=memory_metric_filter,
                    project_id=self.project_id,
                    current_start=start_time,
                    current_end=end_time,
                    baseline_start=baseline_start,
                    baseline_end=baseline_end,
                    alignment_period_seconds=60,
                    per_series_aligner="ALIGN_MEAN",
                    cross_series_reducer="REDUCE_MEAN",
                    client=self.client,
                )
                queries_run.append(f"compare_to_baseline({memory_metric_filter})")
                curr_period = comp.get("current_period", {})
                curr_mean = float(curr_period.get("mean", 0.0))
                base_period = comp.get("baseline_period", {})
                base_mean = float(base_period.get("mean", 0.0))

                curr_pct = format_metric_value(
                    curr_mean, "run.googleapis.com/container/memory/utilizations"
                )
                base_pct = format_metric_value(
                    base_mean, "run.googleapis.com/container/memory/utilizations"
                )

                if curr_mean >= 0.80 or comp.get("is_anomalous", False):
                    mem_anom.append(
                        f"Elevated memory utilization detected on {service}: "
                        f"current mean {curr_pct} vs baseline {base_pct}"
                    )
                mem_ev.append(
                    self.create_evidence_item(
                        kind="memory_metric",
                        excerpt=(
                            f"Metric {memory_metric_filter}: "
                            f"current mean={curr_pct} vs baseline mean={base_pct}"
                        ),
                        timestamp=start_time,
                        console_url=comp.get("console_url"),
                    )
                )
            except Exception as e:
                errors.append(f"Failed memory baseline comparison: {str(e)}")
            return mem_ev, mem_anom

        if is_memory_incident:
            mem_ev, mem_anom = _investigate_memory()
            err_ev, err_anom = _investigate_error_rate()
            lat_ev, lat_anom = _investigate_latency()
            evidence.extend(mem_ev)
            evidence.extend(err_ev)
            evidence.extend(lat_ev)
            anomalies.extend(mem_anom)
            anomalies.extend(err_anom)
            anomalies.extend(lat_anom)
        elif is_latency_incident:
            lat_ev, lat_anom = _investigate_latency()
            err_ev, err_anom = _investigate_error_rate()
            evidence.extend(lat_ev)
            evidence.extend(err_ev)
            anomalies.extend(lat_anom)
            anomalies.extend(err_anom)
        else:
            err_ev, err_anom = _investigate_error_rate()
            lat_ev, lat_anom = _investigate_latency()
            evidence.extend(err_ev)
            evidence.extend(lat_ev)
            anomalies.extend(err_anom)
            anomalies.extend(lat_anom)

        confidence = 0.90 if anomalies else 0.45
        summary = (
            f"Metrics analysis for {service}: detected {len(anomalies)} anomalous telemetry shifts "
            f"and recorded {len(evidence)} evidence points."
        )

        return self.create_findings(
            summary=summary,
            evidence=evidence,
            anomalies=anomalies,
            queries_run=queries_run,
            confidence=confidence,
            errors=errors,
        )
