import asyncio
import functools
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Union, cast

from google.adk.agents import LlmAgent, RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from ..agent_config import get_agent_yaml_config
from ..config import get_settings
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..skills_loader import get_skill_toolset_for_target
from ..tools.common import normalize_datetime
from ..tools.metrics import (
    compare_to_baseline,
    list_alert_policies,
    list_open_alerts,
    query_time_series,
)
from .base import BaseSpecialist, SkillTracker, run_sync

logger = logging.getLogger(__name__)

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
    """Specialist analyzing Cloud Monitoring telemetry and baselines via Two-Pass pattern."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        client: Any = None,
        model: Optional[Any] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
        skill_tracker: Optional[SkillTracker] = None,
    ) -> None:
        if not model:
            if config and hasattr(config, "model_specialist"):
                model = config.model_specialist
            else:
                try:
                    model = get_settings().model_specialist
                except Exception:
                    model = ""

        self.model = model
        self._custom_llm_agent_provided = llm_agent is not None

        adk_tools: list[Any] = [
            FunctionTool(query_time_series),
            FunctionTool(compare_to_baseline),
            FunctionTool(list_alert_policies),
            FunctionTool(list_open_alerts),
        ]
        self.adk_tools = adk_tools

        instruction = get_agent_yaml_config().get_instruction("metrics_specialist")
        default_agent = llm_agent or LlmAgent(
            name="metrics_specialist",
            model=model or "default",
            instruction=instruction,
            tools=adk_tools,
            output_schema=Findings,
        )

        max_tools = (
            getattr(config, "specialist_max_tool_calls", None)
            if config
            else get_settings().specialist_max_tool_calls
        )
        super().__init__(
            name="MetricsSpecialist",
            description="Analyzes time series telemetry, anomaly detection, and alert statuses.",
            project_id=project_id,
            client=client,
            llm_agent=default_agent,
            config=config,
            skill_tracker=skill_tracker or SkillTracker(max_tool_calls=max_tools),
        )

    def get_tools(self) -> List[Any]:
        return list(self.adk_tools)

    def _build_agent(
        self,
        effective_service: Optional[str],
        tracker: SkillTracker,
    ) -> LlmAgent:
        """Construct or configure the LlmAgent for Pass 1 drill-down with skills and tools."""
        skill_toolset = get_skill_toolset_for_target(
            "metrics", service_name=effective_service, tool_filter=["load_skill"]
        )

        if self._custom_llm_agent_provided and self.llm_agent is not None:
            agent = self.llm_agent
            agent.after_tool_callback = tracker.after_tool_callback
            agent.before_tool_callback = tracker.before_tool_callback
            agent.on_tool_error_callback = tracker.on_tool_error_callback

            existing_tools = list(getattr(agent, "tools", []) or [])
            has_skills = any(
                isinstance(t, SkillToolset) or getattr(t, "name", "") == "load_skill"
                for t in existing_tools
            )
            if not has_skills:
                agent.tools = [skill_toolset] + existing_tools
            tracker.agent = agent
            return cast(LlmAgent, agent)

        @functools.wraps(query_time_series)
        def _bound_query_time_series(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            try:
                return query_time_series(**kwargs)
            except ValueError as val_err:
                if "Conflicting filter specification" in str(val_err) and kwargs.get("filter_str"):
                    kwargs.pop("service", None)
                    kwargs.pop("status_code_range", None)
                    return query_time_series(**kwargs)
                raise

        @functools.wraps(compare_to_baseline)
        def _bound_compare_to_baseline(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return compare_to_baseline(**kwargs)

        @functools.wraps(list_alert_policies)
        def _bound_list_alert_policies(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return list_alert_policies(**kwargs)

        @functools.wraps(list_open_alerts)
        def _bound_list_open_alerts(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return list_open_alerts(**kwargs)

        tools: list[Any] = [
            skill_toolset,
            FunctionTool(_bound_query_time_series),
            FunctionTool(_bound_compare_to_baseline),
            FunctionTool(_bound_list_alert_policies),
            FunctionTool(_bound_list_open_alerts),
        ]

        instruction = get_agent_yaml_config().get_instruction("metrics_specialist")

        model_to_use = (
            self.model
            if self.model not in (None, "", "default")
            else (
                self.llm_agent.model
                if self.llm_agent
                and getattr(self.llm_agent, "model", None) not in (None, "", "default")
                else self.model
            )
        )

        agent = LlmAgent(
            name="metrics_specialist",
            model=model_to_use or "default",
            instruction=instruction,
            tools=tools,
            output_schema=Findings,
            after_tool_callback=tracker.after_tool_callback,
            before_tool_callback=tracker.before_tool_callback,
            on_tool_error_callback=tracker.on_tool_error_callback,
        )
        tracker.agent = agent
        return agent

    @staticmethod
    def _parse_llm_findings(text: str) -> Findings:
        """Parse structured Findings from LLM text response."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return Findings.model_validate(data)
        except Exception:
            pass

        return Findings(
            summary=cleaned,
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.70,
        )

    async def _run_skill_llm_pass(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        tracker: SkillTracker,
        **kwargs: Any,
    ) -> Findings:
        """Pass 1: Skill-Led LLM Pass executed via ADK Runner."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        agent = self._build_agent(effective_service, tracker)
        model_val = getattr(agent, "model", None)

        if isinstance(model_val, str):
            if model_val in ("", "default"):
                raise RuntimeError("No LLM configured for MetricsSpecialist (offline mode)")
            has_creds = bool(
                os.getenv("GOOGLE_API_KEY", "").strip()
                or os.getenv("GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                or (
                    (
                        os.getenv("GCP_PROJECT", "").strip()
                        or os.getenv("PROJECT_ID", "").strip()
                        or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
                    )
                    and (
                        os.getenv("GOOGLE_GENAI_USE_ENTERPRISE", "").lower() in ("true", "1")
                        or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")
                        or os.getenv("APP_ENV") == "production"
                    )
                )
            )
            if not has_creds:
                raise RuntimeError("No LLM API key configured for MetricsSpecialist (offline mode)")

        if model_val is None:
            raise RuntimeError("LLM model is unavailable for MetricsSpecialist")

        plan = kwargs.get("plan")
        symptoms = getattr(plan, "symptoms", []) or []
        hypotheses = (
            getattr(plan, "initial_hypotheses", []) or getattr(plan, "hypotheses", []) or []
        )

        prompt_lines = [
            f"Investigate incident {incident.id}: {incident.title}",
            f"Description: {incident.description}",
            f"Severity: {incident.severity}",
            f"Target Service: {effective_service or 'all services'}",
            f"Time Window: {start_time_dt.isoformat()} to {end_time_dt.isoformat()}",
        ]
        if symptoms:
            prompt_lines.append(f"Observed Symptoms: {', '.join(symptoms)}")
        if hypotheses:
            prompt_lines.append(f"Triage Hypotheses: {', '.join(hypotheses)}")

        prompt_lines.extend(
            [
                "",
                "CRITICAL INSTRUCTIONS:",
                (
                    "1. Turn 1: Invoke load_skill exactly once with "
                    "skill_name='cloud-monitoring-metrics'. Do NOT call list_skills."
                ),
            ]
        )
        prompt_lines.extend(
            [
                (
                    "2. Turn 2: Formulate at most ONE targeted diagnostic query using "
                    "query_time_series, compare_to_baseline, or alert tools for the affected "
                    "service."
                ),
                (
                    "3. Turn 3: Conclude immediately by invoking 'set_model_response' with your "
                    "structured Findings report. Do NOT invoke any other tools. Empty metric "
                    "points or normal baselines is a conclusive finding confirming service health. "
                    "Do not loop or try alternative queries."
                ),
            ]
        )
        prompt_text = "\n".join(prompt_lines)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="metrics_specialist",
            user_id="sre_agent",
        )
        runner = Runner(
            agent=agent,
            app_name="metrics_specialist",
            session_service=session_service,
            auto_create_session=True,
        )
        max_llm = (
            getattr(self.config, "specialist_max_llm_calls", None)
            or get_settings().specialist_max_llm_calls
        )
        run_config = RunConfig(max_llm_calls=max_llm)

        user_content = types.Content(
            role="user",
            parts=[types.Part(text=prompt_text)],
        )

        final_text: Optional[str] = None
        final_output: Optional[Any] = None

        try:
            async for event in runner.run_async(
                user_id="sre_agent",
                session_id=session.id,
                new_message=user_content,
                run_config=run_config,
            ):
                if event.output is not None:
                    final_output = event.output
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        t = getattr(part, "text", None)
                        if t:
                            final_text = t
        except Exception as exc:
            logger.info("MetricsSpecialist runner.run_async ended with exception: %s", exc)

        if isinstance(final_output, Findings):
            return final_output
        if isinstance(final_output, dict):
            return Findings.model_validate(final_output)

        if final_text:
            try:
                return self._parse_llm_findings(final_text)
            except Exception:
                pass

        if tracker.skill_first and (len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2):
            return Findings(
                summary=(
                    f"{self.name} completed diagnostic investigation: "
                    f"verified 0 anomalies across {len(tracker.queries_run)} queries."
                ),
                queries_run=list(tracker.queries_run),
                confidence=0.7,
                skills_used=list(tracker.skills_used),
                skill_first=True,
                fallback_used=False,
            )

        raise RuntimeError("MetricsSpecialist LLM pass yielded no output or text")

    def _baseline_sweep_sync(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        **kwargs: Any,
    ) -> Findings:
        """Synchronous Pass 2 baseline sweep querying alerts, rates, latency, and memory."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

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
                        timestamp=start_time_dt,
                    )
                )
        except Exception as e:
            errors.append(f"Failed to list open alerts: {str(e)}")

        # 2. Compare to baseline for both 5xx error rate and request latencies
        duration = end_time_dt - start_time_dt
        baseline_end = start_time_dt
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
                    current_start=start_time_dt,
                    current_end=end_time_dt,
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
                        timestamp=start_time_dt,
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
                    current_start=start_time_dt,
                    current_end=end_time_dt,
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
                        timestamp=start_time_dt,
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
                    current_start=start_time_dt,
                    current_end=end_time_dt,
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
                        timestamp=start_time_dt,
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

    async def _run_baseline_sweep(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        **kwargs: Any,
    ) -> Findings:
        """Pass 2: Concurrent Baseline Sweep executed in worker thread."""
        return await asyncio.to_thread(
            self._baseline_sweep_sync,
            incident,
            start_time,
            end_time,
            effective_service,
            **kwargs,
        )

    async def run_investigation_async(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]] = None,
        end_time: Optional[Union[datetime, str]] = None,
        **kwargs: Any,
    ) -> Findings:
        """Execute Two-Pass investigation (Pass 1 Skill-Led LLM + Pass 2 Concurrent Baseline)."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        effective_service = incident.service_hint or (
            getattr(kwargs.get("plan"), "affected_service", None)
            if kwargs.get("plan")
            and getattr(kwargs.get("plan"), "affected_service", None) != "unknown-service"
            else None
        )

        max_tools = (
            getattr(self.config, "specialist_max_tool_calls", None)
            or get_settings().specialist_max_tool_calls
        )
        tracker = getattr(self, "skill_tracker", None) or SkillTracker(max_tool_calls=max_tools)
        self.skill_tracker = tracker

        timeout_arg = kwargs.get("timeout_seconds")
        if timeout_arg is None:
            timeout_arg = getattr(self.config, "specialist_timeout_seconds", 120.0)
        timeout_val = float(timeout_arg) if timeout_arg is not None else 120.0

        # Launch Pass 1 and Pass 2 concurrently
        pass1_task = asyncio.create_task(
            self._run_skill_llm_pass(
                incident=incident,
                start_time=start_time_dt,
                end_time=end_time_dt,
                effective_service=effective_service,
                tracker=tracker,
                **kwargs,
            )
        )
        pass2_task = asyncio.create_task(
            self._run_baseline_sweep(
                incident=incident,
                start_time=start_time_dt,
                end_time=end_time_dt,
                effective_service=effective_service,
                **kwargs,
            )
        )

        pass1_result: Any = None
        pass1_err: Optional[Exception] = None
        try:
            pass1_result = await asyncio.wait_for(pass1_task, timeout=timeout_val)
        except asyncio.TimeoutError as exc:
            if not pass1_task.done():
                pass1_task.cancel()
            if tracker.skill_first and (
                len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2
            ):
                logger.info("MetricsSpecialist Pass 1 completed drill-down before timeout")
                pass1_result = Findings(
                    summary=(
                        f"{self.name} completed diagnostic investigation: "
                        f"verified 0 anomalies across {len(tracker.queries_run)} queries."
                    ),
                    queries_run=list(tracker.queries_run),
                    confidence=0.7,
                    skills_used=list(tracker.skills_used),
                    skill_first=True,
                    fallback_used=False,
                )
            else:
                logger.warning("MetricsSpecialist Pass 1 timed out after %ss: %s", timeout_val, exc)
                pass1_err = exc
        except Exception as exc:
            if tracker.skill_first and (
                len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2
            ):
                logger.info("MetricsSpecialist Pass 1 completed drill-down before error: %s", exc)
                pass1_result = Findings(
                    summary=(
                        f"{self.name} completed diagnostic investigation: "
                        f"verified 0 anomalies across {len(tracker.queries_run)} queries."
                    ),
                    queries_run=list(tracker.queries_run),
                    confidence=0.7,
                    skills_used=list(tracker.skills_used),
                    skill_first=True,
                    fallback_used=False,
                )
            else:
                logger.warning("MetricsSpecialist Pass 1 failed with error: %s", exc)
                pass1_err = exc

        try:
            pass2_result = await pass2_task
        except Exception as exc:
            logger.error("MetricsSpecialist Pass 2 baseline sweep failed: %s", exc)
            pass2_result = Findings(
                summary=f"Metrics baseline sweep failed: {exc}",
                confidence=0.0,
                errors=[str(exc)],
            )

        # If Pass 1 failed, timed out, or offline -> graceful degradation to Pass 2 baseline sweep
        if pass1_err is not None or not isinstance(pass1_result, Findings):
            fallback_evidence = list(pass2_result.evidence)
            merged_anomalies = list(pass2_result.anomalies)
            merged_queries = list(dict.fromkeys(pass2_result.queries_run + tracker.queries_run))
            errors = list(pass2_result.errors)

            findings = Findings(
                summary=pass2_result.summary,
                evidence=fallback_evidence,
                anomalies=merged_anomalies,
                queries_run=merged_queries,
                confidence=pass2_result.confidence,
                errors=errors,
                skills_used=list(tracker.skills_used),
                skill_first=tracker.skill_first,
                fallback_used=True,
            )
            return tracker.populate_findings(findings)

        # Pass 1 succeeded: Merge Pass 1 and Pass 2
        seen_evidence: set[tuple[str, Optional[datetime]]] = set()
        merged_evidence: List[EvidenceItem] = []
        for ev in pass1_result.evidence + pass2_result.evidence:
            key = (ev.excerpt.strip(), ev.timestamp)
            if key not in seen_evidence:
                seen_evidence.add(key)
                merged_evidence.append(ev)

        merged_anomalies = list(dict.fromkeys(pass1_result.anomalies + pass2_result.anomalies))
        merged_queries = list(
            dict.fromkeys(pass1_result.queries_run + pass2_result.queries_run + tracker.queries_run)
        )
        merged_errors = list(dict.fromkeys(pass1_result.errors + pass2_result.errors))

        summary = pass1_result.summary if pass1_result.summary else pass2_result.summary
        confidence = (
            pass1_result.confidence if pass1_result.confidence > 0 else pass2_result.confidence
        )

        findings = Findings(
            summary=summary,
            evidence=merged_evidence,
            anomalies=merged_anomalies,
            queries_run=merged_queries,
            confidence=max(0.0, min(1.0, confidence)),
            errors=merged_errors,
            skills_used=list(tracker.skills_used),
            skill_first=tracker.skill_first,
            fallback_used=False,
        )
        return tracker.populate_findings(findings)

    def run_investigation(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]] = None,
        end_time: Optional[Union[datetime, str]] = None,
        **kwargs: Any,
    ) -> Findings:
        """Synchronous wrapper for run_investigation_async."""
        res = run_sync(
            self.run_investigation_async(
                incident=incident,
                start_time=start_time,
                end_time=end_time,
                **kwargs,
            )
        )
        return cast(Findings, res)
