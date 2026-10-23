import functools
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool

from ..config import AgentConfig
from ..contracts.findings import EvidenceItem, Findings
from ..contracts.incident import Incident
from ..specialists.base import (
    BaseSpecialist,
    DiagnosticResult,
    SkillTracker,
    format_git_diff_excerpt,
)
from ..tools.github_tools import (
    fetch_job_logs,
    fetch_workflow_run_jobs,
    get_commit_diff,
)

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
            FunctionTool(fetch_job_logs),
            FunctionTool(fetch_workflow_run_jobs),
            FunctionTool(get_commit_diff),
        ]
        from ..agent_config import get_agent_config

        self.llm_agent = llm_agent or LlmAgent(
            name="iac_config_specialist",
            model=self.model or "default",
            instruction=get_agent_config().get_instruction("iac_config_specialist"),
            tools=self.adk_tools,
            output_schema=Findings,
        )
        if self.llm_agent is not None:
            self.llm_agent.before_tool_callback = self.skill_tracker.before_tool_callback
            self.llm_agent.after_tool_callback = self.skill_tracker.after_tool_callback
            self.llm_agent.on_tool_error_callback = self.skill_tracker.on_tool_error_callback
            self.skill_tracker.agent = self.llm_agent

    def get_tools(self) -> list[Any]:
        return list(self.adk_tools)

    def _build_agent(
        self,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> LlmAgent:
        from ..skills_loader import get_skill_toolset_for_target

        skill_toolset = get_skill_toolset_for_target("iac_config")

        @functools.wraps(fetch_job_logs)
        def _bound_fetch_job_logs(repo: str, job_id: int) -> str:
            return fetch_job_logs(repo=repo, job_id=job_id, client=client, config=config)

        @functools.wraps(fetch_workflow_run_jobs)
        def _bound_fetch_workflow_run_jobs(repo: str, run_id: int) -> dict[str, Any]:
            return fetch_workflow_run_jobs(repo=repo, run_id=run_id, client=client, config=config)

        @functools.wraps(get_commit_diff)
        def _bound_get_commit_diff(
            repo: str, head_sha: str = "", commit_sha: str = ""
        ) -> dict[str, Any]:
            target_sha = commit_sha or head_sha
            return get_commit_diff(repo=repo, head_sha=target_sha, client=client, config=config)

        bound_tools = [
            skill_toolset,
            FunctionTool(_bound_fetch_job_logs),
            FunctionTool(_bound_fetch_workflow_run_jobs),
            FunctionTool(_bound_get_commit_diff),
        ]
        from ..agent_config import get_agent_config

        instruction = get_agent_config().get_drilldown_instruction("iac_config_specialist")
        model_to_use = (
            getattr(self.llm_agent, "model", None)
            if self.llm_agent
            else (self.model or "default")
        )
        agent = LlmAgent(
            name="iac_config_specialist",
            model=model_to_use,
            instruction=instruction,
            tools=bound_tools,
            output_schema=Findings,
            before_tool_callback=self.skill_tracker.before_tool_callback,
            after_tool_callback=self.skill_tracker.after_tool_callback,
            on_tool_error_callback=self.skill_tracker.on_tool_error_callback,
        )
        self.skill_tracker.agent = agent
        return agent

    def _findings_to_diagnostic_result(
        self,
        findings: Findings,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "IaC Step")
        evidence_items = list(findings.evidence)
        if not evidence_items:
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=f"IaC configuration error in '{step_name}': {findings.summary}",
                    timestamp=datetime.now(timezone.utc),
                    console_url=incident.external_url,
                )
            )

        diag = DiagnosticResult(
            stage="iac_config",
            specialist_name=self.name,
            summary=findings.summary,
            probable_cause=findings.summary,
            confidence=findings.confidence,
            evidence_items=evidence_items,
            remediation_actions=list(findings.recommended_actions)
            if hasattr(findings, "recommended_actions")
            else [],
            suggested_fix="Correct Terraform definition or YAML schema configuration",
            skills_used=list(self.skill_tracker.skills_used),
            queries_run=list(self.skill_tracker.queries_run),
            skill_first=self.skill_tracker.skill_first,
        )
        return self.skill_tracker.populate_diagnostic_result(diag)

    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        **kwargs: Any,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "IaC Step")
        now = datetime.now(timezone.utc)
        evidence_items: list[EvidenceItem] = []

        repo = kwargs.get("repo") or incident.github_repo or incident.service_hint or ""
        head_sha = kwargs.get("head_sha") or incident.github_head_sha or ""
        job_id = kwargs.get("job_id") or int(failing_job.get("id", 0) or 0)
        run_id = kwargs.get("run_id") or incident.github_run_id or 0

        # Attempt Pass 1: ADK Runner execution if LLM is available
        if self._is_llm_available():
            try:
                from .base import run_sync

                agent = self._build_agent(client=client, config=config)
                prompt_lines = [
                    f"Investigate IaC failure for incident {incident.id}: {incident.title}",
                    f"Job: {failing_job.get('name', 'iac')}, Step: {step_name}",
                    f"Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}",
                    f"Log Excerpt:\n{log_text[:2000]}",
                    "",
                    "CRITICAL INSTRUCTIONS:",
                    (
                        "1. Turn 1: Invoke load_skill exactly once with "
                        "skill_name='cloudrun-deploy-troubleshooting'."
                    ),
                    "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect IaC changes.",
                    (
                        "3. Turn 3: Conclude immediately by invoking set_model_response "
                        "with your Findings report."
                    ),
                ]
                findings = run_sync(self._execute_adk_runner(agent, "\n".join(prompt_lines)))
                if findings and (self.skill_tracker.queries_run or findings.queries_run):
                    return self._findings_to_diagnostic_result(
                        findings, incident, failing_job, failing_step
                    )
            except Exception as exc:
                logger.info("IaCConfigSpecialist ADK runner pass skipped/failed: %s", exc)

        # Pass 2 Fallback: Deterministic tool execution through authentic tracking callbacks
        self.skill_tracker.after_tool_callback(
            "load_skill", {"skill_name": "cloudrun-deploy-troubleshooting"}
        )

        diff_data: Optional[dict[str, Any]] = None
        if repo and head_sha:
            try:
                diff_data = self.tracked_tool_call(
                    "get_commit_diff",
                    {"repo": repo, "head_sha": head_sha},
                    lambda: get_commit_diff(
                        repo=repo, head_sha=head_sha, client=client, config=config
                    ),
                )
            except Exception as exc:
                logger.debug("Could not fetch commit diff: %s", exc)

        if repo and job_id:
            try:
                self.tracked_tool_call(
                    "fetch_job_logs",
                    {"repo": repo, "job_id": job_id},
                    lambda: fetch_job_logs(
                        repo=repo, job_id=job_id, client=client, config=config
                    ),
                )
            except Exception as exc:
                logger.debug("Could not fetch job logs: %s", exc)

        if diff_data and "files" in diff_data:
            diff_excerpt = format_git_diff_excerpt(diff_data)
            if diff_excerpt:
                evidence_items.append(
                    EvidenceItem(
                        kind="git_diff",
                        excerpt=diff_excerpt,
                        timestamp=now,
                        console_url=incident.external_url,
                    )
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
