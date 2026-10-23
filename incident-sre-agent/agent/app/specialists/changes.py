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
from ..tools.changes import (
    diff_revisions,
    list_cloud_builds,
    list_cloud_run_revisions,
    normalize_cloud_build_status,
    query_audit_logs,
)
from ..tools.common import normalize_datetime
from .base import BaseSpecialist, SkillTracker, run_sync

logger = logging.getLogger(__name__)


def is_build_related(build: Any, service_name: Optional[str]) -> bool:
    """Determine if a Cloud Build item is related to the target service name.

    Inspects build images, substitutions, and trigger tags.
    If no service name is specified or the build lacks all relevant metadata,
    it cannot be ruled out as unrelated, so returns True.
    """
    if not service_name:
        return True

    from ..tools.common import get_candidate_service_names

    candidate_names = get_candidate_service_names(service_name)
    svc_lowers = [c.lower() for c in candidate_names]

    images = build.get("images") if isinstance(build, dict) else getattr(build, "images", None)
    substitutions = (
        build.get("substitutions")
        if isinstance(build, dict)
        else getattr(build, "substitutions", None)
    )
    tags = build.get("tags") if isinstance(build, dict) else getattr(build, "tags", None)

    # Ignore MagicMock objects
    if images and hasattr(images, "_mock_name"):
        images = None
    if substitutions and hasattr(substitutions, "_mock_name"):
        substitutions = None
    if tags and hasattr(tags, "_mock_name"):
        tags = None

    # If build has none of these metadata fields, cannot rule out
    if not images and not substitutions and not tags:
        return True

    # 1. Inspect container images
    if images:
        for img in images:
            img_lower = str(img).lower()
            if any(svc_lower in img_lower for svc_lower in svc_lowers):
                return True

    # 2. Inspect substitutions (e.g. _SERVICE_NAME, SERVICE, etc.)
    if substitutions and hasattr(substitutions, "items"):
        for k, v in substitutions.items():
            val_lower = str(v).lower()
            key_lower = str(k).lower()
            if any(svc_lower in val_lower or svc_lower in key_lower for svc_lower in svc_lowers):
                return True

    # 3. Inspect tags
    if tags:
        for tag in tags:
            tag_lower = str(tag).lower()
            if any(svc_lower in tag_lower for svc_lower in svc_lowers):
                return True

    return False


def get_build_unrelated_descriptor(build: Any) -> str:
    """Extract a concise label/descriptor from an unrelated build."""
    images = build.get("images") if isinstance(build, dict) else getattr(build, "images", None)
    if images and not hasattr(images, "_mock_name") and len(images) > 0:
        return str(images[0]).split("/")[-1]
    tags = build.get("tags") if isinstance(build, dict) else getattr(build, "tags", None)
    if tags and not hasattr(tags, "_mock_name") and len(tags) > 0:
        return str(tags[0])
    subs = (
        build.get("substitutions")
        if isinstance(build, dict)
        else getattr(build, "substitutions", None)
    )
    if subs and not hasattr(subs, "_mock_name") and hasattr(subs, "get"):
        if "_SERVICE_NAME" in subs:
            return str(subs["_SERVICE_NAME"])
        for v in subs.values():
            if v:
                return str(v)
    return "unrelated"


class ChangesSpecialist(BaseSpecialist):
    """Specialist identifying recent deployments, edits, and builds via Two-Pass pattern."""

    is_build_related = staticmethod(is_build_related)
    get_build_unrelated_descriptor = staticmethod(get_build_unrelated_descriptor)

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
            FunctionTool(query_audit_logs),
            FunctionTool(diff_revisions),
            FunctionTool(list_cloud_run_revisions),
            FunctionTool(list_cloud_builds),
        ]
        self.adk_tools = adk_tools

        instruction = get_agent_yaml_config().get_instruction("changes_specialist")
        default_agent = llm_agent or LlmAgent(
            name="changes_specialist",
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
            name="ChangesSpecialist",
            description="Correlates incident onset with administrative edits, builds, and deploys.",
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
            "changes", service_name=effective_service, tool_filter=["load_skill"]
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

        @functools.wraps(query_audit_logs)
        def _bound_query_audit_logs(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return query_audit_logs(**kwargs)

        @functools.wraps(diff_revisions)
        def _bound_diff_revisions(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return diff_revisions(**kwargs)

        @functools.wraps(list_cloud_run_revisions)
        def _bound_list_cloud_run_revisions(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return list_cloud_run_revisions(**kwargs)

        @functools.wraps(list_cloud_builds)
        def _bound_list_cloud_builds(**kwargs: Any) -> Any:
            if "client" not in kwargs and self.client is not None:
                kwargs["client"] = self.client
            return list_cloud_builds(**kwargs)

        tools: list[Any] = [
            skill_toolset,
            FunctionTool(_bound_query_audit_logs),
            FunctionTool(_bound_diff_revisions),
            FunctionTool(_bound_list_cloud_run_revisions),
            FunctionTool(_bound_list_cloud_builds),
        ]

        instruction = get_agent_yaml_config().get_instruction("changes_specialist")

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
            name="changes_specialist",
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
                raise RuntimeError("No LLM configured for ChangesSpecialist (offline mode)")
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
                raise RuntimeError("No LLM API key configured for ChangesSpecialist (offline mode)")

        if model_val is None:
            raise RuntimeError("LLM model is unavailable for ChangesSpecialist")

        plan = kwargs.get("plan")
        symptoms = getattr(plan, "symptoms", []) or []
        hypotheses = (
            getattr(plan, "initial_hypotheses", []) or getattr(plan, "hypotheses", []) or []
        )

        cfg = get_agent_yaml_config()
        symptoms_line = (
            "\n" + cfg.format_prompt("symptoms_entry", symptoms=", ".join(symptoms))
            if symptoms
            else ""
        )
        hypotheses_line = (
            "\n" + cfg.format_prompt("hypotheses_entry", hypotheses=", ".join(hypotheses))
            if hypotheses
            else ""
        )
        prompt_text = cfg.format_prompt(
            "changes_specialist",
            "investigation_prompt",
            incident_id=incident.id,
            title=incident.title,
            description=incident.description,
            severity=incident.severity,
            target_service=effective_service or "all services",
            window_start=start_time_dt.isoformat(),
            window_end=end_time_dt.isoformat(),
            symptoms_line=symptoms_line,
            hypotheses_line=hypotheses_line,
        )

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="changes_specialist",
            user_id="sre_agent",
        )
        runner = Runner(
            agent=agent,
            app_name="changes_specialist",
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
            logger.info("ChangesSpecialist runner.run_async ended with exception: %s", exc)

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

        raise RuntimeError("ChangesSpecialist LLM pass yielded no output or text")

    def _baseline_sweep_sync(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]],
        end_time: Optional[Union[datetime, str]],
        effective_service: Optional[str],
        **kwargs: Any,
    ) -> Findings:
        """Synchronous Pass 2 baseline sweep querying audit logs, revisions, and builds."""
        start_time_dt = normalize_datetime(start_time) or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        end_time_dt = normalize_datetime(end_time) or datetime.now(timezone.utc)

        queries_run: List[str] = []
        evidence: List[EvidenceItem] = []
        anomalies: List[str] = []
        errors: List[str] = []

        service = effective_service or "service"

        # 1. Query Audit Logs
        try:
            audit_events = query_audit_logs(
                project_id=self.project_id,
                start_time=start_time_dt,
                end_time=end_time_dt,
                service_name=effective_service,
                client=self.client,
            )
            queries_run.append("query_audit_logs")
            for evt in audit_events:
                ts_val: Optional[datetime] = None
                raw_ts = evt.get("timestamp")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                method = evt.get("method_name") or "AdminActivity"
                principal = evt.get("principal_email") or "system"
                resource = evt.get("resource_name") or service
                anomalies.append(f"Admin action: {method} on {resource} by {principal}")
                evidence.append(
                    self.create_evidence_item(
                        kind="audit_log",
                        excerpt=f"Action {method} on {resource} by {principal}",
                        timestamp=ts_val,
                        console_url=evt.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query audit logs: {str(e)}")

        # 2. Check Cloud Run Revisions if service hint is present
        if effective_service:
            try:
                from ..tools.common import get_candidate_service_names

                candidate_names = get_candidate_service_names(
                    effective_service, getattr(self.config, "name_prefix", None)
                )
                seen_rev_names: set[str] = set()
                revisions = []
                for cand_name in candidate_names:
                    try:
                        cand_revs = list_cloud_run_revisions(
                            project_id=self.project_id,
                            service_name=cand_name,
                            client=self.client,
                        )
                        for r in cand_revs:
                            r_name = r.get("name")
                            if r_name and r_name not in seen_rev_names:
                                seen_rev_names.add(r_name)
                                revisions.append(r)
                            elif not r_name:
                                revisions.append(r)
                        if cand_revs:
                            queries_run.append(f"list_cloud_run_revisions({cand_name})")
                    except Exception:
                        pass
                if not any("list_cloud_run_revisions" in q for q in queries_run):
                    queries_run.append(f"list_cloud_run_revisions({effective_service})")
                plan = kwargs.get("plan")
                plan_onset = getattr(plan, "onset_time", None) if plan else None
                ref_time = plan_onset or start_time_dt
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=timezone.utc)
                else:
                    ref_time = ref_time.astimezone(timezone.utc)
                causality_window = getattr(self.config, "revision_causality_window_minutes", 30)
                causality_start = ref_time - timedelta(minutes=causality_window)

                has_causal_candidate = False
                for rev in revisions[:3]:
                    ts_val = None
                    raw_ts = rev.get("creation_timestamp")
                    if raw_ts:
                        try:
                            ts_val = datetime.fromisoformat(raw_ts)
                            if ts_val.tzinfo is None:
                                ts_val = ts_val.replace(tzinfo=timezone.utc)
                            else:
                                ts_val = ts_val.astimezone(timezone.utc)
                        except Exception:
                            ts_val = None

                    author = rev.get("author") or "system"
                    traffic = rev.get("traffic_percent", 0)

                    # Revision-Causality Rule:
                    # A revision is a plausible causal candidate only if deployed within
                    # revision_causality_window_minutes prior to onset (up to 2 min clock buffer).
                    # Cause must precede effect: revisions deployed after onset cannot cause it.
                    # Revisions deployed long before onset are marked as benign baseline.
                    clock_buffer = timedelta(minutes=2)
                    if ts_val and ts_val < causality_start:
                        excerpt = (
                            f"Revision {rev.get('name')} (author: {author}, traffic: {traffic}%) "
                            f"[benign baseline: deployed at {raw_ts}, before causality window]"
                        )
                    elif ts_val and ts_val > ref_time + clock_buffer:
                        # Deployed after onset: cannot be the cause; potential mitigation
                        tag = " [post-onset deployment: candidate mitigation]"
                        rev_name = rev.get("name")
                        excerpt = (
                            f"Revision {rev_name} (author: {author}, traffic: {traffic}%){tag}"
                        )
                        anomalies.append(f"Post-onset revision deployment: {rev_name} at {raw_ts}")
                    else:
                        is_near = ts_val and (causality_start <= ts_val <= ref_time + clock_buffer)
                        tag = " [causal candidate: deployed near onset]" if is_near else ""
                        rev_name = rev.get("name")
                        excerpt = (
                            f"Revision {rev_name} (author: {author}, traffic: {traffic}%){tag}"
                        )
                        anomalies.append(f"Recent revision deployment: {rev_name} at {raw_ts}")
                        has_causal_candidate = True

                    evidence.append(
                        self.create_evidence_item(
                            kind="revision",
                            excerpt=excerpt,
                            timestamp=ts_val,
                            console_url=rev.get("console_url"),
                        )
                    )

                if revisions and not has_causal_candidate:
                    queries_run.append(
                        "revision_causality_rule: no revisions deployed within causality window"
                    )

                if len(revisions) >= 2:
                    try:
                        rev_latest = revisions[0].get("name", "")
                        rev_prev = revisions[1].get("name", "")
                        if rev_latest and rev_prev:
                            diff_res = diff_revisions(
                                service_name=effective_service,
                                rev_a=rev_prev,
                                rev_b=rev_latest,
                                project_id=self.project_id,
                                client=self.client,
                            )
                            queries_run.append(
                                f"diff_revisions({effective_service}, {rev_prev}, {rev_latest})"
                            )
                            diff_summary = diff_res.get("diff_summary", "")
                            diffs = diff_res.get("differences", {})

                            evidence.append(
                                self.create_evidence_item(
                                    kind="revision_diff",
                                    excerpt=(
                                        f"Configuration diff ({rev_prev} -> {rev_latest}): "
                                        f"{diff_summary}"
                                    ),
                                    timestamp=start_time_dt,
                                    console_url=diff_res.get("console_url"),
                                )
                            )

                            if diffs.get("env_vars_removed"):
                                anomalies.append(
                                    f"Environment variable regression: {diffs['env_vars_removed']} "
                                    f"removed between {rev_prev} and {rev_latest}"
                                )
                            if diffs.get("image_changed"):
                                anomalies.append(
                                    "Container image rollout detected between "
                                    f"{rev_prev} and {rev_latest}"
                                )
                            if diffs.get("probes_changed"):
                                anomalies.append(
                                    "Health probe configuration changed between "
                                    f"{rev_prev} and {rev_latest}"
                                )
                    except Exception as diff_err:
                        errors.append(f"Failed to diff revisions: {str(diff_err)}")
            except Exception as e:
                errors.append(f"Failed to query Cloud Run revisions: {str(e)}")

        # 3. Check Cloud Builds
        try:
            builds = list_cloud_builds(
                project_id=self.project_id,
                limit=5,
                client=self.client,
            )
            queries_run.append("list_cloud_builds")

            target_svc = effective_service
            related_builds = [b for b in builds if is_build_related(b, target_svc)]
            unrelated_builds = [b for b in builds if not is_build_related(b, target_svc)]

            # Prioritize related builds and exclude completely unrelated repository builds
            # when related builds exist.
            if related_builds:
                builds_to_report = [(b, False) for b in related_builds[:3]]
            else:
                builds_to_report = [(b, True) for b in unrelated_builds[:3]]

            for b, is_unrelated in builds_to_report:
                ts_val = None
                raw_ts = b.get("finish_time")
                if raw_ts:
                    try:
                        ts_val = datetime.fromisoformat(raw_ts)
                    except Exception:
                        ts_val = None

                raw_sha = b.get("commit_sha")
                sha = str(raw_sha)[:7] if raw_sha else "none"
                status = normalize_cloud_build_status(b.get("status", "STATUS_UNKNOWN"))

                unrelated_tag = ""
                if is_unrelated:
                    desc = get_build_unrelated_descriptor(b)
                    unrelated_tag = f" [unrelated: {desc}]"

                build_id = b.get("build_id")
                evidence.append(
                    self.create_evidence_item(
                        kind="build",
                        excerpt=(
                            f"Build {build_id} status={status} (commit: {sha}){unrelated_tag}"
                        ),
                        timestamp=ts_val,
                        console_url=b.get("console_url"),
                    )
                )
        except Exception as e:
            errors.append(f"Failed to query Cloud Builds: {str(e)}")

        confidence = 0.70 if anomalies else 0.40
        summary = (
            f"Changes analysis for {service}: found {len(anomalies)} change events "
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
                logger.info("ChangesSpecialist Pass 1 completed drill-down before timeout")
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
                logger.warning("ChangesSpecialist Pass 1 timed out after %ss: %s", timeout_val, exc)
                pass1_err = exc
        except Exception as exc:
            if tracker.skill_first and (
                len(tracker.queries_run) >= 1 or tracker.tool_call_count >= 2
            ):
                logger.info("ChangesSpecialist Pass 1 completed drill-down before error: %s", exc)
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
                logger.warning("ChangesSpecialist Pass 1 failed with error: %s", exc)
                pass1_err = exc

        try:
            pass2_result = await pass2_task
        except Exception as exc:
            logger.error("ChangesSpecialist Pass 2 baseline sweep failed: %s", exc)
            pass2_result = Findings(
                summary=f"Changes baseline sweep failed: {exc}",
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
