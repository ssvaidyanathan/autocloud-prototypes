import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from agent.app.config import AgentConfig
from agent.app.contracts.findings import EvidenceItem
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import CheckRunAnnotation
from agent.app.specialists.base import BaseSpecialist, DiagnosticResult
from agent.app.tools.github_tools import get_commit_diff

logger = logging.getLogger(__name__)

# Lint error pattern: file:line:col: CODE message
LINT_PATTERN = re.compile(
    r"^(?:.*?)(?P<file>[a-zA-Z0-9_\-\./\\]+\.(?:py|js|ts|jsx|tsx|go|java|rs)):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:(?P<code>[A-Z]\d{2,4}|[A-Z0-9_]+)\s+)?(?P<msg>.+)$",
    re.MULTILINE,
)

# Alternative lint pattern: file: line X, col Y, error - msg
ESLINT_PATTERN = re.compile(
    r"^\s*(?P<line>\d+):(?P<col>\d+)\s+error\s+(?P<msg>.+?)\s+(?P<code>[\w\-/]+)$",
    re.MULTILINE,
)

# Pytest failure patterns
PYTEST_FAILURE_PATTERN = re.compile(
    r"(?:FAILED|ERROR)\s+(?P<test>[a-zA-Z0-9_\-\./\\]+(?:::[a-zA-Z0-9_]+)+)\s*(?:-\s*(?P<msg>.+))?",
    re.MULTILINE,
)
PYTEST_ASSERT_PATTERN = re.compile(
    r"^E\s+(?:AssertionError:?\s*|assert\s+)?(?P<msg>.+)$",
    re.MULTILINE,
)

# Docker & packaging patterns
DOCKER_COPY_PATTERN = re.compile(
    r"(?:COPY|ADD)\s+failed:\s*(?:file not found in build context or excluded by "
    r"\.dockerignore:\s*)?(?P<msg>.+)",
    re.IGNORECASE,
)
PIP_CONFLICT_PATTERN = re.compile(
    r"(?:ResolutionImpossible:\s*|conflicting dependencies:\s*)(?P<msg>.+)",
    re.IGNORECASE,
)
DOCKER_RUN_FAILED_PATTERN = re.compile(
    r"(?:The command '(?P<cmd>[^']+)' returned a non-zero code:\s*(?P<code>\d+)"
    r"|ERROR:\s*failed to solve:\s*(?P<msg>.+))",
    re.IGNORECASE,
)


class PipelineLogsSpecialist(BaseSpecialist):
    """Diagnoses lint, test, and container packaging failures from CI logs and git diffs."""

    def __init__(self) -> None:
        super().__init__(name="pipeline_logs_specialist", stage="pipeline_logs")

    def diagnose(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Unknown Step")
        job_name = failing_job.get("name", "Unknown Job")
        combined_label = f"{job_name} / {step_name}".lower()

        # Determine sub-stage
        if any(k in combined_label for k in ("lint", "ruff", "flake8", "mypy", "eslint", "fmt")):
            return self._diagnose_lint(
                incident, failing_job, failing_step, log_text, client, config
            )
        elif any(k in combined_label for k in ("test", "pytest", "jest", "unit", "integration")):
            return self._diagnose_test(
                incident, failing_job, failing_step, log_text, client, config
            )
        elif any(k in combined_label for k in ("build", "docker", "image", "package", "container")):
            return self._diagnose_build(
                incident, failing_job, failing_step, log_text, client, config
            )

        # Fallback to general log diagnosis
        return self._diagnose_generic(incident, failing_job, failing_step, log_text)

    def _diagnose_lint(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Lint Step")
        matches = list(LINT_PATTERN.finditer(log_text))
        annotations: list[CheckRunAnnotation] = []
        evidence_items: list[EvidenceItem] = []

        now = datetime.now(timezone.utc)
        culprit_file = "codebase"
        culprit_rule = "Lint Rule"
        culprit_msg = "Static analysis check failed"

        for match in matches[:5]:
            f_path = match.group("file").strip()
            line_str = match.group("line")
            line = int(line_str) if line_str else 1
            code = (match.group("code") or "").strip()
            msg = match.group("msg").strip()
            culprit_file = f_path
            culprit_rule = code or "Syntax/Rule Error"
            culprit_msg = msg

            annotations.append(
                CheckRunAnnotation(
                    path=f_path,
                    start_line=line,
                    end_line=line,
                    annotation_level="failure",
                    title=f"Linter Violation: {code}" if code else "Linter Violation",
                    message=msg,
                )
            )

            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=f"[{code or 'LINT'}] {f_path}:{line}: {msg}\n{match.group(0).strip()}",
                    timestamp=now,
                    console_url=incident.external_url,
                )
            )

        # Correlate with commit diff if available
        diff_info = ""
        if incident.github_repo and incident.github_head_sha:
            try:
                diff_data = get_commit_diff(
                    repo=incident.github_repo,
                    head_sha=incident.github_head_sha,
                    client=client,
                    config=config,
                )
                if diff_data and "files" in diff_data:
                    changed_files = [f.get("filename") for f in diff_data["files"]]
                    if culprit_file in changed_files:
                        diff_info = (
                            f" Confirmed: culprit file '{culprit_file}' "
                            "was modified in this commit."
                        )
            except Exception as exc:
                logger.debug("Could not fetch commit diff: %s", exc)

        summary = (
            f"Linter failed on step '{step_name}'. Rule '{culprit_rule}' violated in "
            f"'{culprit_file}': {culprit_msg}.{diff_info}"
        )
        probable_cause = (
            f"Static analysis rule violation '{culprit_rule}' in '{culprit_file}': {culprit_msg}."
        )

        remediation = [
            (
                f"Run linter locally to inspect and auto-fix: ruff check --fix {culprit_file} "
                "(or npm run lint -- --fix)"
            ),
            f"Fix code rule violation '{culprit_rule}' in {culprit_file}",
            "Commit and push corrected code to trigger pipeline re-evaluation",
        ]

        return DiagnosticResult(
            stage="lint",
            specialist_name=self.name,
            summary=summary,
            probable_cause=probable_cause,
            confidence=0.95 if matches else 0.80,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix=f"Resolve '{culprit_rule}' in {culprit_file}",
            annotations=annotations,
        )

    def _diagnose_test(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Test Step")
        test_matches = list(PYTEST_FAILURE_PATTERN.finditer(log_text))
        assert_matches = list(PYTEST_ASSERT_PATTERN.finditer(log_text))
        now = datetime.now(timezone.utc)

        failing_test = "test suite"
        assertion_msg = "Assertion failed"
        if test_matches:
            failing_test = test_matches[0].group("test")
            if test_matches[0].group("msg"):
                assertion_msg = test_matches[0].group("msg").strip()
        elif assert_matches:
            assertion_msg = assert_matches[0].group("msg").strip()

        tail_log = log_text[-1500:] if len(log_text) > 1500 else log_text
        evidence_items: list[EvidenceItem] = []
        evidence_items.append(
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"Test '{failing_test}' failed: {assertion_msg}\n{tail_log}",
                timestamp=now,
                console_url=incident.external_url,
            )
        )

        summary = (
            f"Unit/Integration test failure in step '{step_name}': "
            f"Test '{failing_test}' failed with: {assertion_msg}."
        )
        probable_cause = f"Test assertion failure in '{failing_test}': {assertion_msg}."

        remediation = [
            f"Run failing test locally: pytest -k '{failing_test}' -v -vv",
            f"Inspect and update assertions in {failing_test} to match updated behavior",
            "Verify edge case inputs and mock fixtures in test suite",
        ]

        return DiagnosticResult(
            stage="test",
            specialist_name=self.name,
            summary=summary,
            probable_cause=probable_cause,
            confidence=0.90 if test_matches else 0.80,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix=f"Fix assertion in {failing_test}",
        )

    def _diagnose_build(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Build Step")
        now = datetime.now(timezone.utc)
        evidence_items: list[EvidenceItem] = []

        copy_match = DOCKER_COPY_PATTERN.search(log_text)
        conflict_match = PIP_CONFLICT_PATTERN.search(log_text)
        run_match = DOCKER_RUN_FAILED_PATTERN.search(log_text)

        if copy_match:
            detail = copy_match.group("msg").strip()
            probable_cause = (
                f"Docker build COPY failed: Missing build context artifact or file excluded "
                f"by .dockerignore: {detail}."
            )
            remediation = [
                "Verify file paths specified in Dockerfile COPY/ADD exist in repository",
                "Check .dockerignore to ensure build files are not inadvertently excluded",
                "Ensure preceding build steps generated target artifacts prior to build",
            ]
        elif conflict_match:
            detail = conflict_match.group("msg").strip()
            probable_cause = f"Dependency resolution conflict during packaging: {detail}."
            remediation = [
                "Reconcile dependency version constraints in pyproject.toml or requirements.txt",
                "Run dependency solver locally (pip check / poetry lock / uv lock)",
            ]
        elif run_match:
            detail = run_match.group(0).strip()
            probable_cause = f"Container build execution command failed: {detail}."
            remediation = [
                "Inspect RUN commands in Dockerfile and reproduce build locally ('docker build .')",
                "Check system package dependencies and environment variables in container",
            ]
        else:
            probable_cause = f"Container build or packaging failed in step '{step_name}'."
            remediation = [
                "Inspect container build logs and Dockerfile build instructions",
                "Verify base image and network connectivity to package registries",
            ]

        build_log_tail = log_text[-1500:] if len(log_text) > 1500 else log_text
        evidence_items.append(
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"Build failure in '{step_name}': {probable_cause}\n{build_log_tail}",
                timestamp=now,
                console_url=incident.external_url,
            )
        )

        return DiagnosticResult(
            stage="build",
            specialist_name=self.name,
            summary=f"Build/Packaging failure in step '{step_name}': {probable_cause}",
            probable_cause=probable_cause,
            confidence=0.90 if (copy_match or conflict_match or run_match) else 0.75,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix="Fix container build instruction or dependency conflict",
        )

    def _diagnose_generic(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Unknown Step")
        lines = [line.strip() for line in log_text.splitlines() if line.strip()]
        error_lines = [
            line_item
            for line_item in lines
            if any(k in line_item.lower() for k in ("error", "fatal", "failed", "exception"))
        ]
        culprit = (
            error_lines[-1]
            if error_lines
            else (lines[-1] if lines else "Process terminated with non-zero exit code")
        )

        now = datetime.now(timezone.utc)
        evidence_items = [
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"Step '{step_name}' failed: {culprit}",
                timestamp=now,
                console_url=incident.external_url,
            )
        ]

        return DiagnosticResult(
            stage="pipeline_logs",
            specialist_name=self.name,
            summary=f"Step '{step_name}' failed with error: {culprit}",
            probable_cause=f"Step execution failure in '{step_name}': {culprit}",
            confidence=0.75,
            evidence_items=evidence_items,
            remediation_actions=[f"Inspect step '{step_name}' logs and retry workflow run"],
        )
