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
from ..tools.resources import (
    describe_cloud_run_service,
    describe_cloud_sql_instances,
    describe_compute_instances,
    describe_gke_clusters,
)
from .base import BaseSpecialist, SkillTracker, run_sync

logger = logging.getLogger(__name__)


class ResourcesSpecialist(BaseSpecialist):
    """Specialist evaluating compute and database resource health via Two-Pass pattern."""

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
            FunctionTool(describe_cloud_run_service),
            FunctionTool(describe_gke_clusters),
            FunctionTool(describe_compute_instances),
            FunctionTool(describe_cloud_sql_instances),
        ]
        self.adk_tools = adk_tools

        instruction = (
            f"{load_prompt('resources')}\n\n"
            "CRITICAL WORKFLOW REQUIREMENT:\n"
            "1. On Turn 1, invoke 'load_skill' exactly once to load an assigned domain "
            "skill ('cloud-run-troubleshooting', 'gke-troubleshooting', or "
            "'cloud-sql-troubleshooting'). Do NOT invoke list_skills or load other skills.\n"
            "2. On Turn 2, formulate at most ONE targeted diagnostic query (e.g. "
            "describe_cloud_run_service for a Cloud Run service hint).\n"
            "3. STRICT PROHIBITION ON CROSS-PLATFORM HUNTING: If investigating a Cloud Run "
            "service, you are strictly forbidden from searching GKE clusters, Compute Engine "
            "instances, or Cloud SQL instances.\n"
            "4. Receiving 'status': 'NOT_FOUND' or 'conclusive': True, finding 0 resources, "
            "or observing normal status means the investigation is complete and conclusive. "
            "Zero infrastructure defects exist. You are strictly forbidden from calling "
            "load_skill again under any circumstances.\n"
            "5. On Turn 3, you MUST conclude immediately by invoking 'set_model_response' "
            "with your structured Findings report. You are strictly forbidden from calling "
            "load_skill, describe_cloud_run_service, or any other tool after receiving "
            "a conclusive or NOT_FOUND result. Your ONLY valid action on Turn 3 is "
            "to invoke 'set_model_response'."
        )
        default_agent = llm_agent or LlmAgent(
            name="resources_specialist",
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
            name="ResourcesSpecialist",
            description="Inspects Cloud Run, GKE, Compute Engine, and Cloud SQL configurations.",
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
            "resources", service_name=effective_service, tool_filter=["load_skill"]
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

        @functools.wraps(describe_cloud_run_service)
        def _bound_describe_cloud_run_service(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return describe_cloud_run_service(**kwargs)

        @functools.wraps(describe_gke_clusters)
        def _bound_describe_gke_clusters(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return describe_gke_clusters(**kwargs)

        @functools.wraps(describe_compute_instances)
        def _bound_describe_compute_instances(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return describe_compute_instances(**kwargs)

        @functools.wraps(describe_cloud_sql_instances)
        def _bound_describe_cloud_sql_instances(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return describe_cloud_sql_instances(**kwargs)

        tools: list[Any] = [
            skill_toolset,
            FunctionTool(_bound_describe_cloud_run_service),
            FunctionTool(_bound_describe_gke_clusters),
            FunctionTool(_bound_describe_compute_instances),
            FunctionTool(_bound_describe_cloud_sql_instances),
        ]

        instruction = (
            f"{load_prompt('resources')}\n\n"
            "CRITICAL WORKFLOW REQUIREMENT:\n"
            "1. On Turn 1, invoke 'load_skill' exactly once to load an assigned domain "
            "skill ('cloud-run-troubleshooting', 'gke-troubleshooting', or "
            "'cloud-sql-troubleshooting'). Do NOT invoke list_skills or load other skills.\n"
            "2. On Turn 2, formulate at most ONE targeted diagnostic query (e.g. "
            "describe_cloud_run_service for a Cloud Run service hint).\n"
            "3. STRICT PROHIBITION ON CROSS-PLATFORM HUNTING: If investigating a Cloud Run "
            "service, you are strictly forbidden from searching GKE clusters, Compute Engine "
            "instances, or Cloud SQL instances.\n"
            "4. Receiving 'status': 'NOT_FOUND' or 'conclusive': True, finding 0 resources, "
            "or observing normal status means the investigation is complete and conclusive. "
            "Zero infrastructure defects exist. You are strictly forbidden from calling "
            "load_skill again under any circumstances.\n"
            "5. On Turn 3, you MUST conclude immediately by invoking 'set_model_response' "
            "with your structured Findings report. You are strictly forbidden from calling "
            "load_skill, describe_cloud_run_service, or any other tool after receiving "
            "a conclusive or NOT_FOUND result. Your ONLY valid action on Turn 3 is "
            "to invoke 'set_model_response'."
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
            name="resources_specialist",
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
                raise RuntimeError("No LLM configured for ResourcesSpecialist (offline mode)")
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
                raise RuntimeError(
                    "No LLM API key configured for ResourcesSpecialist (offline mode)"
                )

        if model_val is None:
            raise RuntimeError("LLM model is unavailable for ResourcesSpecialist")

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

        tracker.single_service_target = bool(effective_service)
        prompt_lines.extend(
            [
                "",
                "CRITICAL INSTRUCTIONS:",
                (
                    "1. Turn 1: Invoke load_skill exactly once with an assigned domain skill "
                    "('cloud-run-troubleshooting', 'gke-troubleshooting', or "
                    "'cloud-sql-troubleshooting'). Do NOT call list_skills."
                ),
                (
                    "2. Turn 2: Formulate at most ONE targeted diagnostic query (e.g. "
                    "describe_cloud_run_service for a Cloud Run service hint)."
                ),
                (
                    "3. Turn 3: Conclude immediately by invoking 'set_model_response' with your "
                    "structured Findings report. Receiving 'status': 'NOT_FOUND', "
                    "'conclusive': True, or finding 0 resources is definitive diagnostic proof "
                    "that zero infrastructure defects exist. Do NOT invoke any further tools. "
                    "Your ONLY valid action on Turn 3 is to invoke 'set_model_response'."
                ),
                (
                    "4. STRICT PROHIBITION ON CROSS-PLATFORM HUNTING: If investigating a Cloud Run "
                    "service, you are strictly forbidden from searching GKE clusters, Compute "
                    "Engine instances, or Cloud SQL instances."
                ),
            ]
        )
        prompt_text = "\n".join(prompt_lines)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="resources_specialist",
            user_id="sre_agent",
        )
        runner = Runner(
            agent=agent,
            app_name="resources_specialist",
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
                fcs = (
                    [fc.name for fc in event.get_function_calls()]
                    if hasattr(event, "get_function_calls")
                    else []
                )
                role = getattr(getattr(event, "content", None), "role", None)
                logger.warning(
                    "ResourcesSpecialist runner event: author=%s, role=%s, "
                    "function_calls=%s, has_output=%s",
                    getattr(event, "author", None),
                    role,
                    fcs,
                    event.output is not None,
                )
                if event.output is not None:
                    final_output = event.output
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        t = getattr(part, "text", None)
                        if t:
                            final_text = t
        except Exception as exc:
            logger.info("ResourcesSpecialist runner.run_async ended with exception: %s", exc)

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

        raise RuntimeError("ResourcesSpecialist LLM pass yielded no output or text")

    def _baseline_sweep_sync(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        **kwargs: Any,
    ) -> Findings:
        """Synchronous Pass 2 baseline sweep querying Cloud Run, GKE, Compute, and Cloud SQL."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        _end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        service = effective_service or "service"

        # 1. Cloud Run service inspection if service hint provided
        if effective_service:
            try:
                from ..tools.common import get_candidate_service_names

                candidate_names = get_candidate_service_names(
                    effective_service, getattr(self.config, "name_prefix", None)
                )
                svc_desc = None
                resolved_cand = effective_service
                for cand in candidate_names:
                    try:
                        desc = describe_cloud_run_service(
                            project_id=self.project_id,
                            service_name=cand,
                            client=self.client,
                        )
                        if desc and (desc.get("uri") or desc.get("container_image")):
                            svc_desc = desc
                            resolved_cand = desc.get("service_name") or cand
                            break
                    except Exception:
                        pass

                if svc_desc is None:
                    svc_desc = describe_cloud_run_service(
                        project_id=self.project_id,
                        service_name=effective_service,
                        client=self.client,
                    )
                    resolved_cand = str(
                        svc_desc.get("service_name")
                        or effective_service
                        or incident.service_hint
                        or ""
                    )

                queries_run.append(f"describe_cloud_run_service({resolved_cand})")

                # Check resource constraints
                mem_limit = svc_desc.get("memory_limit", "")
                if mem_limit and ("128M" in mem_limit or "256M" in mem_limit):
                    anomalies.append(
                        f"Constrained memory limit detected: {mem_limit} on {resolved_cand}"
                    )

                evidence.append(
                    self.create_evidence_item(
                        kind="cloud_run_service",
                        excerpt=(
                            f"Service {resolved_cand}: "
                            f"image={svc_desc.get('container_image')}, "
                            f"cpu={svc_desc.get('cpu_limit')}, mem={mem_limit}, "
                            f"env_vars={len(svc_desc.get('env_var_names', []))} vars configured"
                        ),
                        timestamp=start_time_dt,
                        console_url=svc_desc.get("console_url"),
                    )
                )
            except Exception as e:
                errors.append(f"Failed to describe Cloud Run service: {str(e)}")

        # 2. GKE Clusters
        try:
            clusters = describe_gke_clusters(project_id=self.project_id, client=self.client)
            queries_run.append("describe_gke_clusters")
            for c in clusters:
                if c.get("status") not in ("RUNNING", "STATUS_UNSPECIFIED"):
                    anomalies.append(
                        f"GKE cluster '{c.get('name')}' is in non-running state: {c.get('status')}"
                    )
        except Exception as e:
            errors.append(f"Failed to describe GKE clusters: {str(e)}")

        # 3. Compute Engine Instances
        try:
            instances = describe_compute_instances(project_id=self.project_id, client=self.client)
            queries_run.append("describe_compute_instances")
            for inst in instances:
                if inst.get("status") not in ("RUNNING", "UNKNOWN"):
                    anomalies.append(
                        f"Compute VM '{inst.get('name')}' is in unexpected state: "
                        f"{inst.get('status')}"
                    )
        except Exception as e:
            errors.append(f"Failed to describe compute instances: {str(e)}")

        # 4. Cloud SQL Instances
        try:
            sql_instances = describe_cloud_sql_instances(
                project_id=self.project_id, client=self.client
            )
            queries_run.append("describe_cloud_sql_instances")
            for sql_inst in sql_instances:
                tier = sql_inst.get("tier", "")
                state = sql_inst.get("state", "")
                if state not in ("RUNNABLE", "STATE_UNSPECIFIED"):
                    anomalies.append(
                        f"Cloud SQL '{sql_inst.get('name')}' is in abnormal state: {state}"
                    )
                evidence.append(
                    self.create_evidence_item(
                        kind="cloud_sql",
                        excerpt=(
                            f"Cloud SQL {sql_inst.get('name')}: "
                            f"{sql_inst.get('database_version')}, "
                            f"tier={tier}, state={state}"
                        ),
                        timestamp=start_time_dt,
                        console_url=sql_inst.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to describe Cloud SQL instances: {str(e)}")

        confidence = 0.70 if anomalies else 0.50
        summary = (
            f"Resources analysis for {service}: checked infrastructure, found "
            f"{len(anomalies)} anomalies and {len(evidence)} evidence items."
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
                logger.info("ResourcesSpecialist Pass 1 completed drill-down before timeout")
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
                logger.warning(
                    "ResourcesSpecialist Pass 1 timed out after %ss: %s", timeout_val, exc
                )
                pass1_err = exc
        except Exception as exc:
            if tracker.skill_first and (
                len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2
            ):
                logger.info("ResourcesSpecialist Pass 1 completed drill-down before error: %s", exc)
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
                logger.warning("ResourcesSpecialist Pass 1 failed with error: %s", exc)
                pass1_err = exc

        try:
            pass2_result = await pass2_task
        except Exception as exc:
            logger.error("ResourcesSpecialist Pass 2 baseline sweep failed: %s", exc)
            pass2_result = Findings(
                summary=f"Resources baseline sweep failed: {exc}",
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
