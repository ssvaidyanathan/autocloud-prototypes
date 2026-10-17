import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..config import AgentConfig
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..specialists.base import BaseSpecialist, DiagnosticResult, SkillTracker
from ..tools.github_tools import fetch_job_logs, get_commit_diff

logger = logging.getLogger(__name__)

TERRAFORM_ERROR_PATTERN = re.compile(
    r"(?:Error:\s*(?P<msg>.+?)(?:\n\s*on\s+(?P<file>[^\s:]+)\s+line\s+(?P<line>\d+))?"
    r"|Error applying plan:\s*(?P<plan_err>.+))",
    re.DOTALL,
)


class IaCConfigSpecialist(BaseSpecialist):
    """Diagnoses Infrastructure-as-Code (Terraform, YAML manifests) errors."""

    def __init__(
        self,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        skill_tracker: Optional[SkillTracker] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name="iac_config_specialist",
            stage="iac_config",
            model=model,
            config=config,
            skill_tracker=skill_tracker,
            **kwargs,
        )
        self.adk_tools = [
            FunctionTool(get_commit_diff),
            FunctionTool(fetch_job_logs),
        ]
        self.llm_agent = llm_agent or LlmAgent(
            name="iac_config_specialist",
            model=self.model or "default",
            instruction=(
                "You are a specialist diagnosing Infrastructure-as-Code "
                "(Terraform, YAML manifests) errors."
            ),
            tools=self.adk_tools,
            output_schema=Findings,
        )

    def get_tools(self) -> list[Any]:
        return list(self.adk_tools)

    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "IaC Step")
        now = datetime.now(timezone.utc)
        evidence_items: list[EvidenceItem] = []

        self.skill_tracker.after_tool_callback(
            "load_skill", {"skill_name": "cloudrun-deploy-troubleshooting"}
        )

        tf_match = TERRAFORM_ERROR_PATTERN.search(log_text)
        if tf_match:
            msg = (tf_match.group("msg") or tf_match.group("plan_err") or "").strip()
            file_name = tf_match.group("file") or "main.tf"
            line_str = tf_match.group("line") or "1"
            probable_cause = f"Terraform execution failure in {file_name}:{line_str}: {msg[:200]}."
            remediation = [
                f"Inspect Terraform configuration in {file_name} around line {line_str}",
                (
                    "Run 'terraform validate' and 'terraform plan' locally to identify "
                    "syntax or provider schema issues"
                ),
            ]
        else:
            probable_cause = (
                f"Infrastructure configuration or manifest error in step '{step_name}'."
            )
            remediation = [
                (
                    f"Verify infrastructure manifests and cloud provider credentials "
                    f"for step '{step_name}'"
                )
            ]

        tail_log = log_text[-1500:] if len(log_text) > 1500 else log_text
        evidence_items.append(
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"IaC configuration error in '{step_name}': {probable_cause}\n{tail_log}",
                timestamp=now,
                console_url=incident.external_url,
            )
        )

        diag = DiagnosticResult(
            stage="iac_config",
            specialist_name=self.name,
            summary=f"IaC / Configuration error in step '{step_name}': {probable_cause}",
            probable_cause=probable_cause,
            confidence=0.85 if tf_match else 0.70,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix="Correct Terraform definition or YAML schema configuration",
        )
        return self.skill_tracker.populate_diagnostic_result(diag)
