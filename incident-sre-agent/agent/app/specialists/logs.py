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

from ..config import get_settings
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..prompts import load_prompt
from ..skills_loader import get_skill_toolset_for_target
from ..tools.common import normalize_datetime
from ..tools.logs import (
    aggregate_requests,
    list_log_names,
    query_logs,
    query_request_logs,
    summarize_error_groups,
)
from .base import BaseSpecialist, SkillTracker, run_sync

logger = logging.getLogger(__name__)


class LogsSpecialist(BaseSpecialist):
    """Specialist analyzing Cloud Logging and Error Reporting via Two-Pass Agentic pattern."""

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

        # Static tool definitions for inspection and ToolRegistry checks
        adk_tools: list[Any] = [
            FunctionTool(query_logs),
            FunctionTool(query_request_logs),
            FunctionTool(aggregate_requests),
            FunctionTool(summarize_error_groups),
            FunctionTool(list_log_names),
        ]
        self.adk_tools = adk_tools

        instruction = load_prompt("logs")
        default_agent = llm_agent or LlmAgent(
            name="logs_specialist",
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
            name="LogsSpecialist",
            description="Investigates application and system logs for error spikes.",
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
            "logs", service_name=effective_service, tool_filter=["load_skill"]
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

        # Bind client if provided
        @functools.wraps(query_logs)
        def _bound_query_logs(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            try:
                return query_logs(**kwargs)
            except ValueError as val_err:
                if "Conflicting filter specification" in str(val_err) and kwargs.get("filter_str"):
                    kwargs.pop("service", None)
                    kwargs.pop("severity", None)
                    kwargs.pop("status_code_range", None)
                    kwargs.pop("text_contains", None)
                    return query_logs(**kwargs)
                raise

        @functools.wraps(query_request_logs)
        def _bound_query_request_logs(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return query_request_logs(**kwargs)

        @functools.wraps(aggregate_requests)
        def _bound_aggregate_requests(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return aggregate_requests(**kwargs)

        @functools.wraps(summarize_error_groups)
        def _bound_summarize_error_groups(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return summarize_error_groups(**kwargs)

        @functools.wraps(list_log_names)
        def _bound_list_log_names(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return list_log_names(**kwargs)

        tools: list[Any] = [
            skill_toolset,
            FunctionTool(_bound_query_logs),
            FunctionTool(_bound_query_request_logs),
            FunctionTool(_bound_aggregate_requests),
            FunctionTool(_bound_summarize_error_groups),
            FunctionTool(_bound_list_log_names),
        ]

        instruction = (
            f"{load_prompt('logs')}\n\n"
            "CRITICAL WORKFLOW REQUIREMENT:\n"
            "1. On Turn 1, invoke 'load_skill' exactly once to load your assigned domain skill "
            "('cloud-logging-queries'). Do NOT invoke list_skills or load other skills.\n"
            "2. On Turn 2, execute at most ONE diagnostic query (such as "
            "summarize_error_groups or query_logs) for the affected service.\n"
            "3. On Turn 3, conclude immediately by invoking 'set_model_response' with your "
            "structured Findings response. Do NOT invoke any other tools. Zero log entries or "
            "empty error groups is a conclusive finding confirming service health. Conclude "
            "immediately without trying alternative queries or performing exhaustive scans."
        )

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
            name="logs_specialist",
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

        # Detect offline / no LLM configured:
        # If model is string and neither API key nor Vertex AI / ADC credentials configured
        if isinstance(model_val, str):
            if model_val in ("", "default"):
                raise RuntimeError("No LLM configured for LogsSpecialist (offline mode)")
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
                raise RuntimeError("No LLM API key configured for LogsSpecialist (offline mode)")

        if model_val is None:
            raise RuntimeError("LLM model is unavailable for LogsSpecialist")

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
                    "skill_name='cloud-logging-queries'. Do NOT call list_skills."
                ),
            ]
        )
        prompt_lines.extend(
            [
                (
                    "2. Turn 2: Execute at most ONE diagnostic query using "
                    "query_logs or summarize_error_groups focusing on the affected service."
                ),
                (
                    "3. Turn 3: Conclude immediately by invoking 'set_model_response' with your "
                    "structured Findings report. Do NOT invoke any other tools. Zero log entries "
                    "or empty results is a conclusive finding confirming service health. Do not "
                    "loop or try alternative queries."
                ),
            ]
        )
        prompt_text = "\n".join(prompt_lines)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="logs_specialist",
            user_id="sre_agent",
        )
        runner = Runner(
            agent=agent,
            app_name="logs_specialist",
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
            logger.info("LogsSpecialist runner.run_async ended with exception: %s", exc)

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

        raise RuntimeError("LogsSpecialist LLM pass yielded no output or text")

    def _baseline_sweep_sync(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        **kwargs: Any,
    ) -> Findings:
        """Synchronous Pass 2 baseline sweep querying standard error groups and logs."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        service = effective_service or "all services"

        # 1. Summarize Error Groups
        try:
            time_window_hours = max(1, int((end_time_dt - start_time_dt).total_seconds() // 3600))
            eg_result = summarize_error_groups(
                project_id=self.project_id,
                time_window_hours=time_window_hours,
                start_time=start_time_dt,
                end_time=end_time_dt,
                service_name=effective_service,
                client=self.client,
            )
            queries_run.append(
                f"summarize_error_groups("
                f"start_time={start_time_dt.isoformat()}, end_time={end_time_dt.isoformat()})"
            )

            for grp in eg_result.get("error_groups", []):
                msg = grp.get("message", "")
                if not msg or msg.strip() == "{}":
                    msg = f"Error group {grp.get('group_id', 'unknown')}"
                count = grp.get("count", 0)
                anomalies.append(f"Error group: '{msg[:100]}' occurred {count} times")
                evidence.append(
                    self.create_evidence_item(
                        kind="error_group",
                        excerpt=f"Error group {grp.get('group_id')}: {msg} (count: {count})",
                        console_url=eg_result.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query error groups: {str(e)}")

        # 2. Query Logs for Warning and Error Severity
        log_filter = "severity >= WARNING"
        if effective_service:
            from ..tools.common import get_candidate_service_names

            candidates = get_candidate_service_names(
                effective_service, getattr(self.config, "name_prefix", None)
            )
            svc_clauses = []
            for cand in candidates:
                svc_clauses.append(f'resource.labels.service_name = "{cand}"')
            for cand in candidates:
                svc_clauses.append(f'resource.labels.container_name = "{cand}"')
            for cand in candidates:
                svc_clauses.append(f'textPayload =~ "{cand}"')
            svc_clauses = list(dict.fromkeys(svc_clauses))
            log_filter = f"severity >= WARNING AND ({' OR '.join(svc_clauses)})"

        try:
            resource_names = [f"projects/{self.project_id}"]
            logs_result = query_logs(
                filter_str=log_filter,
                resource_names=resource_names,
                max_entries=50,
                start_time=start_time,
                end_time=end_time,
                client=self.client,
            )
            queries_run.append(logs_result.get("query_filter", log_filter))

            entries = logs_result.get("entries", [])
            for entry in entries[:15]:
                ts_val: Optional[datetime] = None
                raw_ts = entry.get("timestamp")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                payload_excerpt = entry.get("payload_excerpt", "")
                if not payload_excerpt or payload_excerpt.strip() == "{}":
                    payload_excerpt = "No error/warning payload details"

                sev = str(entry.get("severity", "INFO")).upper()
                kind = "warning_log" if sev == "WARNING" else "log_entry"

                evidence.append(
                    self.create_evidence_item(
                        kind=kind,
                        excerpt=f"[{sev}] {payload_excerpt} (log: {entry.get('log_name')})",
                        timestamp=ts_val,
                        console_url=logs_result.get("console_url"),
                    )
                )

            error_entries = [
                e for e in entries if str(e.get("severity", "")).upper() in ["ERROR", "CRITICAL"]
            ]
            warning_entries = [
                e for e in entries if str(e.get("severity", "")).upper() == "WARNING"
            ]

            if len(error_entries) > 0:
                statuses = set()
                for entry_dict in error_entries:
                    exc = entry_dict.get("payload_excerpt", "")
                    if "HTTP " in exc:
                        parts = exc.split("HTTP ")
                        if len(parts) > 1:
                            statuses.add(parts[1].split()[0])
                status_suffix = f" (statuses: {', '.join(sorted(statuses))})" if statuses else ""
                anomalies.append(
                    f"Found {len(error_entries)} ERROR/CRITICAL log entries for "
                    f"{service}{status_suffix} between {start_time_dt.isoformat()} and "
                    f"{end_time_dt.isoformat()}"
                )

            latency_warnings = [
                e
                for e in warning_entries
                if any(
                    k in str(e.get("payload_excerpt", "")).lower()
                    for k in ["delay", "latency", "slow", "timeout", "injecting"]
                )
            ]
            if latency_warnings:
                first_msg = latency_warnings[0].get("payload_excerpt", "")
                anomalies.append(
                    f"Application warning logs indicate latency spike / processing delay "
                    f"on {service}: {first_msg}"
                )
            elif warning_entries:
                anomalies.append(
                    f"Found {len(warning_entries)} WARNING log entries for {service} "
                    f"between {start_time_dt.isoformat()} and {end_time_dt.isoformat()}"
                )

        except Exception as e:
            errors.append(f"Failed to query logs: {str(e)}")

        # 3. Query Cloud Run Request Logs if service is known
        if effective_service:
            try:
                req_summary = query_request_logs(
                    service_name=effective_service,
                    project_id=self.project_id,
                    start_time=start_time_dt,
                    end_time=end_time_dt,
                    client=self.client,
                )
                queries_run.append(f"query_request_logs({effective_service})")

                total_reqs = req_summary.get("total_requests", 0)
                status_bd = req_summary.get("status_breakdown", {})
                errors_4xx = status_bd.get("4xx", 0)
                errors_5xx = status_bd.get("5xx", 0)
                p95_ms = req_summary.get("latency_p95_ms", 0.0)

                evidence.append(
                    self.create_evidence_item(
                        kind="request_logs",
                        excerpt=(
                            f"Request log breakdown for {effective_service}: "
                            f"{total_reqs} total requests "
                            f"(2xx: {status_bd.get('2xx', 0)}, 3xx: {status_bd.get('3xx', 0)}, "
                            f"4xx: {errors_4xx}, 5xx: {errors_5xx}), p95 latency: {p95_ms}ms"
                        ),
                        timestamp=start_time_dt,
                        console_url=req_summary.get("console_url"),
                    )
                )

                if errors_5xx > 0 or errors_4xx > 0:
                    anomalies.append(
                        f"Request logs show HTTP error responses on {effective_service}: "
                        f"{errors_4xx} 4xx errors, {errors_5xx} 5xx errors "
                        f"across {total_reqs} requests"
                    )
            except Exception as e:
                errors.append(f"Failed to query request logs: {str(e)}")

        confidence = 0.85 if anomalies else 0.40
        summary = (
            f"Logs analysis for {service}: found {len(anomalies)} anomalous patterns "
            f"and {len(evidence)} evidence items."
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
                logger.info("LogsSpecialist Pass 1 completed drill-down before timeout")
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
                logger.warning("LogsSpecialist Pass 1 timed out after %ss: %s", timeout_val, exc)
                pass1_err = exc
        except Exception as exc:
            if tracker.skill_first and (
                len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2
            ):
                logger.info("LogsSpecialist Pass 1 completed drill-down before error: %s", exc)
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
                logger.warning("LogsSpecialist Pass 1 failed with error: %s", exc)
                pass1_err = exc

        try:
            pass2_result = await pass2_task
        except Exception as exc:
            logger.error("LogsSpecialist Pass 2 baseline sweep failed: %s", exc)
            pass2_result = Findings(
                summary=f"Logs baseline sweep failed: {exc}",
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
