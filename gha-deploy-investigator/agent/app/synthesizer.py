import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.adk.agents import LlmAgent

from .config import AgentConfig, get_settings
from .contracts.findings import EvidenceItem
from .contracts.incident import Incident
from .contracts.report import (
    ProbableRootCause,
    Report,
    SpecialistStatus,
    TimelineItem,
)
from .log_utils import extract_error_lines
from .skills_loader import SkillsLoader, get_skill_toolset_for_target
from .specialists.base import DiagnosticResult

logger = logging.getLogger(__name__)


STAGE_TO_SKILL = {
    "lint": "lint-troubleshooting",
    "test": "test-failure-triage",
    "build": "container-build-troubleshooting",
    "deploy": "cloudrun-deploy-troubleshooting",
    "iac_config": "cloudrun-deploy-troubleshooting",
    "pipeline_logs": "container-build-troubleshooting",
}


class SynthesizerAgent:
    """Synthesizer agent compiling findings from specialists into a final Incident Report."""

    def __init__(
        self,
        skills_loader: Optional[SkillsLoader] = None,
        llm_client: Any = None,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        **kwargs: Any,
    ) -> None:
        self.config = config or get_settings()
        self.skills_loader = skills_loader or SkillsLoader()
        self.model = model or getattr(self.config, "gemini_model", None)
        self.llm_client = llm_client

        toolset = get_skill_toolset_for_target(
            "dispatcher", skills_dir=self.skills_loader.skills_dir
        )
        self.skill_toolset = toolset
        instruction = (
            "You are the Incident Report Synthesizer Agent. Compile specialist findings, "
            "telemetry evidence, and causal root cause into a cohesive Report "
            "adhering to the schema."
        )
        self.llm_agent = llm_agent or LlmAgent(
            name="synthesizer",
            model=self.model or "default",
            instruction=instruction,
            tools=[toolset],
            output_schema=Report,
        )
        self.synthesizer_mode: str = "heuristic_fallback"

    def synthesize(
        self,
        incident: Incident,
        failed_job: Optional[dict[str, Any]] = None,
        failed_step: Optional[dict[str, Any]] = None,
        log_text: str = "",
        diagnostic_result: Optional[DiagnosticResult] = None,
        findings: Optional[Dict[str, Any]] = None,
        plan: Optional[Any] = None,
    ) -> Report:
        """Synthesize canonical Report with ProbableRootCause, evidence, and specialist findings."""
        failed_job = failed_job or {}
        failed_step = failed_step or {}
        job_name = failed_job.get("name", "pipeline")
        step_name = failed_step.get("name", "execution")
        now_utc = datetime.now(timezone.utc)

        # Attempt Vertex AI Gemini synthesis
        llm_result: Optional[dict[str, Any]] = None
        if self.llm_client is not None and hasattr(self.llm_client, "synthesize"):
            try:
                llm_result = self.llm_client.synthesize(
                    incident=incident,
                    job=failed_job,
                    step=failed_step,
                    log_text=log_text,
                )
            except Exception as exc:
                logger.warning("Custom LLM client synthesis failed: %s", exc)

        if llm_result is None:
            try:
                from .llm import generate_gemini_synthesis

                diag_summary = None
                spec_findings = None
                if diagnostic_result:
                    diag_summary = diagnostic_result.summary
                    structured_errors: list[dict[str, Any]] = []
                    for ann in diagnostic_result.annotations:
                        title_str = ann.title or ""
                        code_match = re.search(r"Linter Violation:\s*([A-Za-z0-9_]+)", title_str)
                        c_code = code_match.group(1) if code_match else (ann.title or "LINT")
                        structured_errors.append(
                            {
                                "code": c_code,
                                "file": ann.path,
                                "line": ann.start_line,
                                "message": ann.message,
                            }
                        )

                    diff_evidence = [
                        e.excerpt for e in diagnostic_result.evidence_items if e.kind == "git_diff"
                    ]
                    linter_evidence = [
                        e.excerpt
                        for e in diagnostic_result.evidence_items
                        if e.kind == "linter_breakdown"
                    ]

                    spec_findings = {
                        "probable_cause": diagnostic_result.probable_cause,
                        "summary": diagnostic_result.summary,
                        "remediation_actions": diagnostic_result.remediation_actions,
                        "structured_errors": structured_errors,
                        "diff_info": diff_evidence[0] if diff_evidence else None,
                        "linter_breakdown": linter_evidence[0] if linter_evidence else None,
                    }

                llm_result = generate_gemini_synthesis(
                    failing_job=failed_job,
                    failing_step=failed_step,
                    log_text=log_text,
                    config=self.config,
                    specialist_findings=spec_findings,
                    diagnostic_summary=diag_summary,
                )
            except Exception as llm_exc:
                logger.info("Vertex AI Gemini synthesis not available or failed: %s", llm_exc)

        error_lines = extract_error_lines(log_text)
        if llm_result and llm_result.get("root_cause"):
            self.synthesizer_mode = "llm"
            raw_rc = str(llm_result["root_cause"]).strip()
            raw_rc = re.sub(r"^```(?:json)?\s*", "", raw_rc, flags=re.IGNORECASE)
            raw_rc = re.sub(r"\s*```$", "", raw_rc).strip()
            if raw_rc.startswith("{") and '"root_cause"' in raw_rc:
                m_rc = re.search(r'"root_cause"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"?', raw_rc)
                if m_rc:
                    raw_rc = m_rc.group(1).replace(r"\"", '"').strip()
            root_cause_text = raw_rc
            confidence_val = float(llm_result.get("confidence", 0.95))
            primary_error = root_cause_text
        elif diagnostic_result and diagnostic_result.probable_cause:
            self.synthesizer_mode = "heuristic_fallback"
            root_cause_text = diagnostic_result.probable_cause
            confidence_val = diagnostic_result.confidence
            primary_error = diagnostic_result.probable_cause
        elif error_lines:
            self.synthesizer_mode = "heuristic_fallback"
            primary_error = error_lines[0]
            root_cause_text = (
                f"Pipeline step '{step_name}' in job '{job_name}' failed: {primary_error}"
            )
            confidence_val = 0.92
        else:
            self.synthesizer_mode = "heuristic_fallback"
            primary_error = f"Step '{step_name}' exited with failure"
            root_cause_text = (
                f"Pipeline step '{step_name}' in job '{job_name}' failed: {primary_error}"
            )
            confidence_val = 0.75

        probable_root_cause = ProbableRootCause(
            text=root_cause_text,
            confidence=confidence_val,
        )

        stage_label = diagnostic_result.stage if diagnostic_result else "execution"
        summary = (
            f"Workflow run for repository '{incident.github_repo or incident.title}' "
            f"failed at stage '{stage_label}' (step '{step_name}'). "
            f"Primary failure: {primary_error}"
        )

        branch_name = incident.github_branch or "main"
        timeline = [
            TimelineItem(
                timestamp=incident.opened_at,
                description=f"Workflow run initiated on branch '{branch_name}'",
            ),
            TimelineItem(
                timestamp=now_utc,
                description=f"Step '{step_name}' failed in job '{job_name}' (stage: {stage_label})",
            ),
        ]

        evidence: list[EvidenceItem] = []
        if diagnostic_result and diagnostic_result.evidence_items:
            evidence.extend(diagnostic_result.evidence_items)
        else:
            excerpt = "\n".join(error_lines[:5]) if error_lines else log_text[-500:]
            evidence.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=excerpt,
                    timestamp=now_utc,
                    console_url=incident.external_url,
                )
            )

        recommended_actions: list[str] = []
        if llm_result and llm_result.get("remediation_actions"):
            recommended_actions.extend(llm_result["remediation_actions"])
        if diagnostic_result and diagnostic_result.remediation_actions:
            for act in diagnostic_result.remediation_actions:
                if act not in recommended_actions:
                    recommended_actions.append(act)

        has_iam_actas = any(
            ("actas" in line.lower() or ("iam" in line.lower() and "permission" in line.lower()))
            for line in error_lines
        )
        if not has_iam_actas and llm_result:
            rc_low = str(llm_result.get("root_cause", "")).lower()
            if "actas" in rc_low or ("iam" in rc_low and "permission" in rc_low):
                has_iam_actas = True

        if has_iam_actas:
            iam_act = (
                "Grant 'roles/iam.serviceAccountUser' (iam.serviceaccounts.actAs permission) "
                "to the deployment service account on the Cloud Run runtime service account"
            )
            if not any(
                "actas" in a.lower() or "serviceaccountuser" in a.lower()
                for a in recommended_actions
            ):
                recommended_actions.insert(0, iam_act)

        if not recommended_actions:
            recommended_actions.append(
                f"Inspect failure in step '{step_name}' of workflow '{incident.github_repo}'"
            )
            recommended_actions.append(f"Review culprit log output: {primary_error}")

        spec_name = (
            diagnostic_result.specialist_name
            if diagnostic_result
            else "pipeline_logs_specialist"
        )
        specialists_status = [
            SpecialistStatus(
                specialist=spec_name,
                status="completed",
                error=None,
            )
        ]

        annotations = diagnostic_result.annotations if diagnostic_result else []

        # Skills resolution
        skills_used: List[str] = []
        if diagnostic_result and diagnostic_result.skills_used:
            skills_used.extend(diagnostic_result.skills_used)
        elif plan and getattr(plan, "skills_to_load", None):
            skills_used.extend(plan.skills_to_load)
        else:
            skill = STAGE_TO_SKILL.get(stage_label, "container-build-troubleshooting")
            skills_used.append(skill)
        skills_used = list(dict.fromkeys(skills_used))

        queries_appendix: dict[str, list[str]] = {
            "job_id": [str(failed_job.get("id", ""))]
        }
        if diagnostic_result and diagnostic_result.queries_run:
            queries_appendix["queries_run"] = list(diagnostic_result.queries_run)

        return Report(
            summary=summary,
            impact=f"Pipeline delivery blocked on branch '{incident.github_branch or 'main'}'",
            timeline=timeline,
            evidence_by_area={"logs": evidence},
            probable_root_cause=probable_root_cause,
            recent_changes=[],
            recommended_actions=recommended_actions,
            specialists_status=specialists_status,
            skills_used=skills_used,
            caveats=[],
            annotations=annotations,
            queries_appendix=queries_appendix,
            metadata={
                "failing_step": step_name,
                "failing_job": job_name,
                "failing_stage": stage_label,
                "repo": incident.github_repo,
                "run_id": incident.github_run_id,
                "synthesizer_mode": self.synthesizer_mode,
            },
        )


def synthesize_report(
    incident: Incident,
    failed_job: dict[str, Any],
    failed_step: dict[str, Any],
    log_text: str,
    diagnostic_result: Optional[DiagnosticResult] = None,
) -> Report:
    """Convenience function delegating to SynthesizerAgent."""
    agent = SynthesizerAgent()
    return agent.synthesize(
        incident=incident,
        failed_job=failed_job,
        failed_step=failed_step,
        log_text=log_text,
        diagnostic_result=diagnostic_result,
    )


__all__ = [
    "STAGE_TO_SKILL",
    "SynthesizerAgent",
    "extract_error_lines",
    "synthesize_report",
]
