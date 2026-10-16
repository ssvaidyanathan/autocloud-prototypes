import re
from datetime import datetime, timezone
from typing import Any, Optional

from agent.app.specialists.base import DiagnosticResult

from .contracts.findings import EvidenceItem
from .contracts.incident import Incident
from .contracts.report import (
    ProbableRootCause,
    Report,
    SpecialistStatus,
    TimelineItem,
)


def extract_error_lines(log_text: str) -> list[str]:
    """Scan raw runner log lines for error markers, tracebacks, and diagnostic failures."""
    lines = log_text.splitlines()
    diagnostic_lines: list[str] = []
    generic_exit_lines: list[str] = []

    diagnostic_patterns = [
        re.compile(r":\d+:\d*:?\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r":\d+:\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r"\b[A-Z]\d{3,4}\b"),
        re.compile(r"\bassertionerror\b", re.IGNORECASE),
        re.compile(r"\bexception\b", re.IGNORECASE),
        re.compile(r"\btraceback\b", re.IGNORECASE),
        re.compile(r"\berror\b", re.IGNORECASE),
        re.compile(r"\bfailed\b", re.IGNORECASE),
        re.compile(r"\bfatal\b", re.IGNORECASE),
    ]

    generic_patterns = [
        re.compile(r"exit code [1-9]", re.IGNORECASE),
        re.compile(r"process completed with exit code", re.IGNORECASE),
    ]

    for line in lines:
        cleaned = line.strip()
        content = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", cleaned)
        if not content:
            continue

        matched_diag = any(pat.search(content) for pat in diagnostic_patterns)
        if matched_diag:
            diagnostic_lines.append(content)
        elif any(pat.search(content) for pat in generic_patterns):
            generic_exit_lines.append(content)

    return diagnostic_lines + generic_exit_lines


def synthesize_report(
    incident: Incident,
    failed_job: dict[str, Any],
    failed_step: dict[str, Any],
    log_text: str,
    diagnostic_result: Optional[DiagnosticResult] = None,
) -> Report:
    """Synthesize canonical Report with ProbableRootCause, evidence, and specialist findings."""
    job_name = failed_job.get("name", "pipeline")
    step_name = failed_step.get("name", "execution")
    now_utc = datetime.now(timezone.utc)

    error_lines = extract_error_lines(log_text)
    if diagnostic_result and diagnostic_result.probable_cause:
        primary_error = diagnostic_result.probable_cause
        confidence_val = diagnostic_result.confidence
    elif error_lines:
        primary_error = error_lines[0]
        confidence_val = 0.92
    else:
        primary_error = f"Step '{step_name}' exited with failure"
        confidence_val = 0.75

    probable_root_cause = ProbableRootCause(
        text=(
            f"Automated fallback (LLM unavailable): Pipeline step '{step_name}' in job "
            f"'{job_name}' failed: {primary_error}"
        ),
        confidence=confidence_val,
    )

    stage_label = diagnostic_result.stage if diagnostic_result else "execution"
    summary = (
        f"Workflow run for repository '{incident.github_repo or incident.title}' "
        f"failed at stage '{stage_label}' (step '{step_name}'). Primary failure: {primary_error}"
    )

    timeline = [
        TimelineItem(
            timestamp=incident.opened_at,
            description=f"Workflow run initiated on branch '{incident.github_branch or 'main'}'",
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
                source="ci_logs",
                kind="github_runner_log",
                excerpt=excerpt,
                timestamp=now_utc,
                console_url=incident.external_url,
            )
        )

    recommended_actions: list[str] = []
    if diagnostic_result and diagnostic_result.remediation_actions:
        recommended_actions.extend(diagnostic_result.remediation_actions)
    else:
        recommended_actions.append(
            f"Inspect failure in step '{step_name}' of workflow '{incident.github_repo}'"
        )
        recommended_actions.append(f"Review culprit log output: {primary_error}")

    spec_name = (
        diagnostic_result.specialist_name if diagnostic_result else "pipeline_logs_specialist"
    )
    specialists_status = [
        SpecialistStatus(
            specialist=spec_name,
            status="completed",
            error=None,
        )
    ]

    annotations = diagnostic_result.annotations if diagnostic_result else []

    return Report(
        summary=summary,
        impact=f"Pipeline delivery blocked on branch '{incident.github_branch or 'main'}'",
        timeline=timeline,
        evidence_by_area={"logs": evidence},
        probable_root_cause=probable_root_cause,
        recent_changes=[],
        recommended_actions=recommended_actions,
        specialists_status=specialists_status,
        skills_used=[
            f"{stage_label}-troubleshooting"
            if stage_label in ("lint", "test")
            else "container-build-troubleshooting"
        ],
        caveats=[],
        annotations=annotations,
        queries_appendix={"job_id": [str(failed_job.get("id", ""))]},
        metadata={
            "failing_step": step_name,
            "failing_job": job_name,
            "failing_stage": stage_label,
            "repo": incident.github_repo,
            "run_id": incident.github_run_id,
        },
    )


__all__ = [
    "extract_error_lines",
    "synthesize_report",
]
