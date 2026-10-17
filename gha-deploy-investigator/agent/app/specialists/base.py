import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from google.adk.tools.base_tool import BaseTool

from ..config import AgentConfig, get_settings
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..contracts.report import CheckRunAnnotation
from ..tools.registry import default_registry

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    """Standardized output of a diagnostic specialist."""

    stage: str
    specialist_name: str
    summary: str
    probable_cause: str
    confidence: float
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    remediation_actions: list[str] = field(default_factory=list)
    suggested_fix: Optional[str] = None
    annotations: list[CheckRunAnnotation] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    queries_run: list[str] = field(default_factory=list)
    skill_first: bool = False


class _PrunedToolMarker(BaseTool):
    """Marker tool with no declaration so ADK retains only set_model_response."""

    def _get_declaration(self) -> Any:
        return None


class SkillTracker:
    """Tracks skills loaded and verifies skill-first execution via ADK tool callbacks."""

    def __init__(self, max_tool_calls: Optional[int] = None) -> None:
        self.skills_used: List[str] = []
        self.skill_first: bool = False
        self.tool_call_count: int = 0
        self.max_tool_calls: int = max_tool_calls or 5
        self.queries_run: List[str] = []
        self.executed_calls: set[tuple[str, str]] = set()
        self.agent: Optional[Any] = None

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
        """Callback invoked before every tool execution by LlmAgent."""
        tool_name = tool if isinstance(tool, str) else getattr(tool, "name", str(tool))
        if tool_name == "set_model_response":
            return None

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

        # Budget enforcement
        if self.max_tool_calls and self.tool_call_count >= self.max_tool_calls:
            self._prune_agent_tools()
            return {
                "status": "budget_reached",
                "message": (
                    f"Diagnostic query budget reached ({self.max_tool_calls} calls). "
                    "Conclude immediately with your findings."
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
        """Handle tool errors gracefully."""
        err_msg = str(error) if error else "Unknown tool error"
        return {"error": f"Tool execution failed: {err_msg}."}

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
                logger.warning("SKILL_LOAD: loaded skill '%s'", cleaned)
                if cleaned and cleaned not in self.skills_used:
                    self.skills_used.append(cleaned)
        elif tool_name != "set_model_response":
            q_str = f"{tool_name}({', '.join(f'{k}={v!r}' for k, v in sorted(args.items()))})"
            if q_str not in self.queries_run:
                self.queries_run.append(q_str)

        self.executed_calls.add((tool_name, serialized_args))

        if self.max_tool_calls and self.tool_call_count >= self.max_tool_calls:
            self._prune_agent_tools()

        return None

    def populate_findings(self, findings: Findings) -> Findings:
        """Populate skills_used, queries_run, and skill_first onto Findings."""
        if not findings.skills_used:
            findings.skills_used = list(self.skills_used)
        else:
            findings.skills_used = list(dict.fromkeys(findings.skills_used + self.skills_used))

        if self.skill_first:
            findings.skill_first = True

        if self.queries_run:
            findings.queries_run = list(dict.fromkeys(findings.queries_run + self.queries_run))

        return findings

    def populate_diagnostic_result(self, diag: DiagnosticResult) -> DiagnosticResult:
        """Populate skills_used, queries_run, and skill_first onto DiagnosticResult."""
        if not diag.skills_used:
            diag.skills_used = list(self.skills_used)
        else:
            diag.skills_used = list(dict.fromkeys(diag.skills_used + self.skills_used))

        if self.skill_first:
            diag.skill_first = True

        if self.queries_run:
            diag.queries_run = list(dict.fromkeys(diag.queries_run + self.queries_run))

        return diag

    def reset(self) -> None:
        """Reset tracking state for a new run."""
        self.skills_used.clear()
        self.skill_first = False
        self.tool_call_count = 0
        self.queries_run.clear()
        self.executed_calls.clear()


class BaseSpecialist(ABC):
    """Abstract base class for diagnostic specialists using Google ADK."""

    def __init__(
        self,
        name: str,
        stage: str,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        skill_tracker: Optional[SkillTracker] = None,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.stage = stage
        self.config = config or get_settings()
        self.model = model or getattr(self.config, "gemini_model", None)
        self.skill_tracker = skill_tracker or SkillTracker()
        self.llm_agent = llm_agent

        # Enforce strictly read-only tools
        default_registry.assert_all_readonly()

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
    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        """Analyze failure logs, query read-only tools, and formulate diagnostic findings."""
        pass
