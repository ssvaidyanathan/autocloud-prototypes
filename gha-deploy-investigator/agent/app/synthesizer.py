import re
from datetime import datetime, timezone
from typing import Any

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

    # Diagnostic patterns (specific compiler / linter / test / exception lines)
    diagnostic_patterns = [
        re.compile(r":\d+:\d*:?\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r":\d+:\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r"\b[A-Z]\d{3,4}\b"),  # e.g. F841, E501
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
        # Strip timestamp prefix if present (e.g. "2026-10-16T21:00:00.0000000Z ")
        content = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", cleaned)
        if not content:
            continue

        matched_diag = any(pat.search(content) for pat in diagnostic_patterns)
        if matched_diag:
            diagnostic_lines.append(content)
        elif any(pat.search(content) for pat in generic_patterns):
            generic_exit_lines.append(content)

    # Return diagnostic lines first, followed by generic exit lines
    return diagnostic_lines + generic_exit_lines


def synthesize_report(
    incident: Incident,
    failed_job: dict[str, Any],
    failed_step: dict[str, Any],
    log_text: str,
) -> Report:
    """Synthesize canonical Report with ProbableRootCause and diagnostic evidence."""
    job_name = failed_job.get("name", "pipeline")
    step_name = failed_step.get("name", "execution")
    now_utc = datetime.now(timezone.utc)

    error_lines = extract_error_lines(log_text)
    primary_error = error_lines[0] if error_lines else f"Step '{step_name}' exited with failure"

    # Formulate root cause
    probable_root_cause = ProbableRootCause(
        text=(
            f"Automated fallback (LLM unavailable): Pipeline step '{step_name}' in job "
            f"'{job_name}' failed: {primary_error}"
        ),
        confidence=0.92 if error_lines else 0.75,
    )

    summary = (
        f"Workflow run for repository '{incident.github_repo or incident.title}' "
        f"failed at stage '{step_name}'. Primary failure: {primary_error}"
    )

    timeline = [
        TimelineItem(
            timestamp=incident.opened_at,
            description=f"Workflow run initiated on branch '{incident.github_branch or 'main'}'",
        ),
        TimelineItem(
            timestamp=now_utc,
            description=f"Step '{step_name}' failed in job '{job_name}'",
        ),
    ]

    excerpt = "\n".join(error_lines[:5]) if error_lines else log_text[-500:]
    evidence = [
        EvidenceItem(
            kind="github_runner_log",
            excerpt=excerpt,
            timestamp=now_utc,
            console_url=incident.external_url,
        )
    ]

    recommended_actions = [
        f"Inspect failure in step '{step_name}' of workflow '{incident.github_repo}'",
        f"Review culprit log output: {primary_error}",
    ]
    if "ruff" in step_name.lower() or "lint" in step_name.lower():
        recommended_actions.append(
            "Run local linter (`ruff check .`) to verify and fix syntax/style violations"
        )
    elif "test" in step_name.lower() or "pytest" in step_name.lower():
        recommended_actions.append(
            "Run failing tests locally (`pytest`) to reproduce and verify fixes"
        )
    elif "build" in step_name.lower() or "docker" in step_name.lower():
        recommended_actions.append("Validate Dockerfile directives and build context locally")

    specialists_status = [
        SpecialistStatus(
            specialist="pipeline_logs",
            status="completed",
            error=None,
        )
    ]

    return Report(
        summary=summary,
        impact=f"Pipeline delivery blocked on branch '{incident.github_branch or 'main'}'",
        timeline=timeline,
        evidence_by_area={"logs": evidence},
        probable_root_cause=probable_root_cause,
        recent_changes=[],
        recommended_actions=recommended_actions,
        specialists_status=specialists_status,
        skills_used=["github-actions-logs-analysis"],
        caveats=[],
        annotations=[],
        queries_appendix={"job_id": [str(failed_job.get("id", ""))]},
        metadata={
            "failing_step": step_name,
            "failing_job": job_name,
            "repo": incident.github_repo,
            "run_id": incident.github_run_id,
        },
    )


__all__ = [
    "extract_error_lines",
    "synthesize_report",
]
