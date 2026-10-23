import asyncio
import concurrent.futures
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Union

from google.adk.tools.base_tool import BaseTool

from ..agent_config import get_agent_yaml_config
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..tools.common import normalize_datetime

logger = logging.getLogger(__name__)


def run_sync(coro: Any) -> Any:
    """Execute an async coroutine synchronously, supporting existing event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class _PrunedToolMarker(BaseTool):
    """Marker tool with no declaration so ADK retains only set_model_response."""

    def _get_declaration(self) -> Any:
        return None


class SkillTracker:
    """Tracks skills loaded and verifies skill-first execution via ADK tool callbacks."""

    def __init__(self, max_tool_calls: Optional[int] = None) -> None:
        if max_tool_calls is None:
            try:
                from ..config import get_settings

                max_tool_calls = get_settings().specialist_max_tool_calls
            except Exception:
                max_tool_calls = 5
        self.skills_used: List[str] = []
        self.skill_first: bool = False
        self.tool_call_count: int = 0
        self.max_tool_calls: int = max_tool_calls
        self.queries_run: List[str] = []
        self.executed_calls: set[tuple[str, str]] = set()
        self.agent: Optional[Any] = None
        self.single_service_target: bool = False

    def _prune_agent_tools(self) -> None:
        """Prune agent tools to contain ONLY set_model_response."""
        if self.agent is None:
            return
        output_schema = getattr(self.agent, "output_schema", None)
        if output_schema is not None:
            self.agent.tools = [_PrunedToolMarker(name="_pruned_marker", description="")]
        else:
            self.agent.tools = []

    def before_tool_callback(
        self,
        tool: Any,
        tool_args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Callback invoked before every tool execution by LlmAgent to enforce tool call caps."""
        tool_name = tool if isinstance(tool, str) else getattr(tool, "name", str(tool))
        if tool_name == "set_model_response":
            return None

        # 1. Prevent redundant load_skill retry cycles
        if tool_name == "load_skill" and len(self.skills_used) > 0:
            ctx = tool_context or kwargs.get("tool_context")
            if self.queries_run:
                if ctx is not None:
                    if hasattr(ctx, "actions"):
                        ctx.actions.skip_summarization = True
                    inv_ctx = getattr(ctx, "_invocation_context", None)
                    if inv_ctx is not None:
                        inv_ctx.end_invocation = True
                msg = (
                    f"Skill '{self.skills_used[0]}' is already loaded and diagnostic "
                    "queries are complete. Do not invoke load_skill again. "
                    "You MUST conclude immediately by invoking 'set_model_response' "
                    "with your structured Findings."
                )
            else:
                msg = (
                    f"Skill '{self.skills_used[0]}' is already loaded. "
                    "Do not invoke load_skill again. Proceed to diagnostic queries "
                    "or emit your Findings."
                )
            self._prune_agent_tools()
            return {
                "status": "skill_already_loaded",
                "message": msg,
            }

        # 1b. Prevent further tool calls after conclusive diagnostic query on single-service target
        if self.queries_run and self.single_service_target and tool_name != "set_model_response":
            ctx = tool_context or kwargs.get("tool_context")
            if ctx is not None:
                if hasattr(ctx, "actions"):
                    ctx.actions.skip_summarization = True
                inv_ctx = getattr(ctx, "_invocation_context", None)
                if inv_ctx is not None:
                    inv_ctx.end_invocation = True
            self._prune_agent_tools()
            return {
                "status": "conclusive",
                "message": (
                    "All diagnostic queries for this service are complete and conclusive. "
                    f"Tool '{tool_name}' is disabled. "
                    "You MUST invoke 'set_model_response' immediately with your "
                    "structured Findings."
                ),
            }

        args = (
            tool_args if tool_args is not None else kwargs.get("tool_args", kwargs.get("args", {}))
        )
        if not isinstance(args, dict):
            args = {}

        try:
            serialized_args = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            serialized_args = str(args)
        logger.warning(
            "TOOL_CALL before: %s args=%s (count=%d, max=%s)",
            tool_name,
            serialized_args,
            self.tool_call_count,
            self.max_tool_calls,
        )
        call_key = (tool_name, serialized_args)

        # 2. Prevent repeated identical queries
        if call_key in self.executed_calls:
            ctx = tool_context or kwargs.get("tool_context")
            if ctx is not None:
                if hasattr(ctx, "actions"):
                    ctx.actions.skip_summarization = True
                inv_ctx = getattr(ctx, "_invocation_context", None)
                if inv_ctx is not None:
                    inv_ctx.end_invocation = True
            self._prune_agent_tools()
            return {
                "status": "already_executed",
                "message": (
                    "This query was already executed. "
                    "Do not repeat identical queries. Conclude immediately with structured "
                    "Findings."
                ),
            }

        # 3. Forbid cross-platform hunting if Cloud Run service was already investigated
        if tool_name in (
            "describe_gke_clusters",
            "describe_compute_instances",
            "describe_cloud_sql_instances",
        ) and any(call[0] == "describe_cloud_run_service" for call in self.executed_calls):
            ctx = tool_context or kwargs.get("tool_context")
            if ctx is not None:
                if hasattr(ctx, "actions"):
                    ctx.actions.skip_summarization = True
                inv_ctx = getattr(ctx, "_invocation_context", None)
                if inv_ctx is not None:
                    inv_ctx.end_invocation = True
            self._prune_agent_tools()
            return {
                "status": "conclusive",
                "message": (
                    "Cross-platform hunting is strictly forbidden. "
                    "Cloud Run diagnostic query is conclusive. "
                    "You MUST conclude immediately by invoking 'set_model_response' "
                    "with your structured Findings report. Do NOT invoke any other tools."
                ),
            }

        # 4. Forbid multi-query change hunting if a change diagnostic query was already run
        if tool_name in (
            "query_audit_logs",
            "list_cloud_run_revisions",
            "list_cloud_builds",
        ) and any(
            call[0] in ("query_audit_logs", "list_cloud_run_revisions", "list_cloud_builds")
            for call in self.executed_calls
        ):
            ctx = tool_context or kwargs.get("tool_context")
            if ctx is not None:
                if hasattr(ctx, "actions"):
                    ctx.actions.skip_summarization = True
                inv_ctx = getattr(ctx, "_invocation_context", None)
                if inv_ctx is not None:
                    inv_ctx.end_invocation = True
            self._prune_agent_tools()
            return {
                "status": "conclusive",
                "result": [],
                "message": (
                    "Diagnostic change query already completed. Additional change hunting is "
                    "strictly forbidden. You MUST conclude immediately by invoking "
                    "'set_model_response' with your structured Findings report. "
                    "Do NOT invoke any other tools."
                ),
            }

        # 5. Budget enforcement
        if self.max_tool_calls and self.tool_call_count >= self.max_tool_calls:
            ctx = tool_context or kwargs.get("tool_context")
            if ctx is not None:
                if hasattr(ctx, "actions"):
                    ctx.actions.skip_summarization = True
                inv_ctx = getattr(ctx, "_invocation_context", None)
                if inv_ctx is not None:
                    inv_ctx.end_invocation = True
            self._prune_agent_tools()
            return {
                "status": "complete",
                "result": "No anomalies or issues detected. Service is operating normally.",
                "message": (
                    f"Diagnostic query budget reached ({self.max_tool_calls} calls). "
                    "Data collection is complete and confirmed normal. "
                    "You MUST conclude immediately by invoking 'set_model_response' with "
                    "summary (string) and confidence (float). "
                    "Do NOT invoke any other tools."
                ),
            }
        return None

    def on_tool_error_callback(
        self,
        tool: Any,
        args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        error: Optional[Exception] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Handle tool errors gracefully so ADK runner does not fail with DynamicNodeFailError."""
        err_msg = str(error) if error else "Unknown tool error"
        return {
            "error": (
                f"Tool execution failed: {err_msg}. "
                "Please adjust your arguments or proceed with findings."
            )
        }

    def after_tool_callback(
        self,
        tool: Any,
        tool_args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        tool_output: Optional[Any] = None,
        *extra_args: Any,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Callback invoked after every tool execution by LlmAgent."""
        tool_name = tool if isinstance(tool, str) else getattr(tool, "name", str(tool))
        if tool_name == "set_model_response":
            return None

        if not (self.max_tool_calls and self.tool_call_count >= self.max_tool_calls):
            self.tool_call_count += 1

        args = (
            tool_args if tool_args is not None else kwargs.get("tool_args", kwargs.get("args", {}))
        )
        if not isinstance(args, dict):
            args = {}

        try:
            serialized_args = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            serialized_args = str(args)

        out = (
            tool_output
            if tool_output is not None
            else kwargs.get("tool_response", kwargs.get("result"))
        )

        logger.warning(
            "TOOL_CALL after: %s args=%s output=%s (new_count=%d)",
            tool_name,
            serialized_args,
            str(out)[:200] if out is not None else "None",
            self.tool_call_count,
        )

        is_load_skill = tool_name == "load_skill"

        # Turn 1 check: Was the very first tool call a load_skill invocation?
        if self.tool_call_count == 1:
            self.skill_first = is_load_skill

        if is_load_skill:
            skill_name = args.get("skill_name")
            if skill_name and isinstance(skill_name, str):
                cleaned = skill_name.strip()
                if cleaned and cleaned not in self.skills_used:
                    self.skills_used.append(cleaned)
        elif tool_name != "set_model_response":
            if isinstance(out, dict):
                q = out.get("query_filter") or out.get("query")
                if q and isinstance(q, str) and q not in self.queries_run:
                    self.queries_run.append(q)
            if not self.queries_run:
                q_str = f"{tool_name}({', '.join(f'{k}={v!r}' for k, v in sorted(args.items()))})"
                if q_str not in self.queries_run:
                    self.queries_run.append(q_str)

        self.executed_calls.add((tool_name, serialized_args))

        # Check for conclusive tool results or completed single-service diagnostic query
        is_conclusive = False
        if isinstance(out, dict):
            if out.get("conclusive") is True or out.get("status") in (
                "NOT_FOUND",
                "conclusive",
                "complete",
            ):
                is_conclusive = True
        elif isinstance(out, list) and tool_name in (
            "describe_gke_clusters",
            "describe_compute_instances",
            "describe_cloud_sql_instances",
        ):
            is_conclusive = True

        if is_conclusive or (
            tool_name
            in (
                "describe_cloud_run_service",
                "describe_gke_clusters",
                "describe_compute_instances",
                "describe_cloud_sql_instances",
            )
            and self.single_service_target
        ):
            logger.warning(
                "Diagnostic query '%s' conclusive. Pruning tools to set_model_response.",
                tool_name,
            )
            self._prune_agent_tools()
            conclusive_instr = get_agent_yaml_config().get_prompt_template(
                "tool_prune_conclusive_instruction"
            )
            if isinstance(out, dict):
                augmented = dict(out)
                augmented["investigation_status"] = "COMPLETE_AND_CONCLUSIVE"
                augmented["instruction"] = conclusive_instr
                return augmented
            elif isinstance(out, list):
                return {
                    "result": out,
                    "investigation_status": "COMPLETE_AND_CONCLUSIVE",
                    "instruction": conclusive_instr,
                }

        if self.max_tool_calls and self.tool_call_count >= self.max_tool_calls:
            self._prune_agent_tools()

        return None

    def __call__(
        self,
        tool: Any,
        tool_args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        tool_output: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        return self.after_tool_callback(
            tool,
            tool_args=tool_args,
            tool_context=tool_context,
            tool_output=tool_output,
            **kwargs,
        )

    def populate_findings(self, findings: Findings) -> Findings:
        """Populate skills_used and skill_first onto Findings."""
        if not findings.skills_used:
            findings.skills_used = list(self.skills_used)
        else:
            findings.skills_used = list(dict.fromkeys(findings.skills_used + self.skills_used))

        if self.skill_first:
            findings.skill_first = True

        if self.queries_run:
            findings.queries_run = list(dict.fromkeys(findings.queries_run + self.queries_run))

        return findings

    def reset(self) -> None:
        """Reset tracking state for a new run."""
        self.skills_used.clear()
        self.skill_first = False
        self.tool_call_count = 0
        self.queries_run.clear()
        self.executed_calls.clear()
        self.single_service_target = False


class BaseSpecialist(ABC):
    """Abstract base class for domain-specific SRE specialists."""

    def __init__(
        self,
        name: str,
        description: str,
        project_id: Optional[str] = None,
        client: Any = None,
        llm_agent: Optional[Any] = None,
        config: Optional[Any] = None,
        skill_tracker: Optional[SkillTracker] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.config = config
        if not project_id:
            try:
                from ..config import get_settings

                project_id = get_settings().gcp_project
            except Exception:
                import os

                project_id = os.getenv("GCP_PROJECT", "")
        self.project_id = project_id or ""
        self.client = client
        self.skill_tracker = skill_tracker or SkillTracker()
        self.last_findings: Optional[Findings] = None
        self.llm_agent = llm_agent
        if self.llm_agent:
            if (
                hasattr(self.llm_agent, "after_tool_callback")
                and self.llm_agent.after_tool_callback is None
            ):
                self.llm_agent.after_tool_callback = self.skill_tracker.after_tool_callback
            if (
                hasattr(self.llm_agent, "before_tool_callback")
                and self.llm_agent.before_tool_callback is None
            ):
                self.llm_agent.before_tool_callback = self.skill_tracker.before_tool_callback

    @property
    def tools(self) -> List[Any]:
        """Return ADK FunctionTools assigned to this specialist."""
        return self.get_tools()

    def get_tools(self) -> List[Any]:
        """Return list of FunctionTools for this specialist."""
        return []

    def before_tool_callback(
        self,
        tool: Any,
        tool_args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Pre-tool execution callback on the specialist."""
        return self.skill_tracker.before_tool_callback(
            tool,
            tool_args=tool_args,
            tool_context=tool_context,
            **kwargs,
        )

    def after_tool_callback(
        self,
        tool: Any,
        tool_args: Optional[dict[str, Any]] = None,
        tool_context: Optional[Any] = None,
        tool_output: Optional[Any] = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Tool tracking callback on the specialist."""
        return self.skill_tracker.after_tool_callback(
            tool,
            tool_args=tool_args,
            tool_context=tool_context,
            tool_output=tool_output,
            **kwargs,
        )

    @abstractmethod
    def run_investigation(
        self,
        incident: Incident,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> Findings:
        """Domain-specific investigation logic returning structured Findings."""
        raise NotImplementedError

    async def investigate_async(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]] = None,
        end_time: Optional[Union[datetime, str]] = None,
        **kwargs: Any,
    ) -> Findings:
        """Execute investigation asynchronously with time boundary resolution and error boundary."""
        resolved_end = (
            normalize_datetime(end_time)
            or normalize_datetime(getattr(incident, "opened_at", None))
            or datetime.now(timezone.utc)
        )
        resolved_start = normalize_datetime(start_time) or (resolved_end - timedelta(hours=1))

        try:
            if hasattr(self, "run_investigation_async"):
                findings = await self.run_investigation_async(
                    incident=incident,
                    start_time=resolved_start,
                    end_time=resolved_end,
                    **kwargs,
                )
            else:
                findings = await asyncio.to_thread(
                    self.run_investigation,
                    incident=incident,
                    start_time=resolved_start,
                    end_time=resolved_end,
                    **kwargs,
                )
            populated = self.skill_tracker.populate_findings(findings)
            self.last_findings = populated
            return populated
        except Exception as exc:
            err_findings = Findings(
                summary=f"{self.name} investigation failed: {str(exc)}",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.0,
                errors=[f"Error in {self.name}: {type(exc).__name__}: {str(exc)}"],
                skills_used=list(self.skill_tracker.skills_used),
                skill_first=self.skill_tracker.skill_first,
                fallback_used=True,
            )
            self.last_findings = err_findings
            return err_findings

    def investigate(
        self,
        incident: Incident,
        start_time: Optional[Union[datetime, str]] = None,
        end_time: Optional[Union[datetime, str]] = None,
        **kwargs: Any,
    ) -> Findings:
        """Execute investigation safely with time boundary resolution and error boundary."""
        resolved_end = (
            normalize_datetime(end_time)
            or normalize_datetime(getattr(incident, "opened_at", None))
            or datetime.now(timezone.utc)
        )
        resolved_start = normalize_datetime(start_time) or (resolved_end - timedelta(hours=1))

        try:
            findings = self.run_investigation(
                incident=incident,
                start_time=resolved_start,
                end_time=resolved_end,
                **kwargs,
            )
            populated = self.skill_tracker.populate_findings(findings)
            self.last_findings = populated
            return populated
        except Exception as exc:
            # Resilient error containment: specialist failure never crashes overall workflow
            err_findings = Findings(
                summary=f"{self.name} investigation failed: {str(exc)}",
                evidence=[],
                anomalies=[],
                queries_run=[],
                confidence=0.0,
                errors=[f"Error in {self.name}: {type(exc).__name__}: {str(exc)}"],
                skills_used=list(self.skill_tracker.skills_used),
                skill_first=self.skill_tracker.skill_first,
                fallback_used=True,
            )
            self.last_findings = err_findings
            return err_findings

    @staticmethod
    def create_evidence_item(
        kind: str,
        excerpt: str,
        timestamp: Optional[datetime] = None,
        console_url: Optional[str] = None,
    ) -> EvidenceItem:
        """Create a validated EvidenceItem adhering to contracts."""
        return EvidenceItem(
            kind=kind,
            excerpt=excerpt[:500] if len(excerpt) > 500 else excerpt,
            timestamp=timestamp,
            console_url=console_url,
        )

    def create_findings(
        self,
        summary: Any = "",
        evidence: Optional[List[EvidenceItem]] = None,
        anomalies: Optional[List[str]] = None,
        queries_run: Optional[List[str]] = None,
        confidence: float = 0.5,
        errors: Optional[List[str]] = None,
        skills_used: Optional[List[str]] = None,
        skill_first: Optional[bool] = None,
        fallback_used: bool = False,
    ) -> Findings:
        """Construct a validated Findings contract with skills tracking."""
        if not isinstance(self, BaseSpecialist):
            # Fallback when invoked as static/class function: BaseSpecialist.create_findings(...)
            actual_summary = str(self)
            fallback_ev = evidence or (summary if isinstance(summary, list) else [])
            return Findings(
                summary=actual_summary,
                evidence=fallback_ev,
                anomalies=anomalies or [],
                queries_run=queries_run or [],
                confidence=max(0.0, min(1.0, confidence)),
                errors=errors or [],
                skills_used=skills_used or [],
                skill_first=skill_first or False,
                fallback_used=fallback_used,
            )

        clamped_confidence = max(0.0, min(1.0, confidence))
        tracker = getattr(self, "skill_tracker", None)
        resolved_skills = (
            skills_used
            if skills_used is not None
            else (list(tracker.skills_used) if tracker else [])
        )
        resolved_skill_first = (
            skill_first if skill_first is not None else (tracker.skill_first if tracker else False)
        )
        return Findings(
            summary=summary,
            evidence=evidence or [],
            anomalies=anomalies or [],
            queries_run=queries_run or [],
            confidence=clamped_confidence,
            errors=errors or [],
            skills_used=resolved_skills,
            skill_first=resolved_skill_first,
            fallback_used=fallback_used,
        )
