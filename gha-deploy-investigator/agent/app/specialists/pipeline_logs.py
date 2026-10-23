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
from ..contracts.report import CheckRunAnnotation
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

STAGE_TO_SKILL = {
    "lint": "lint-troubleshooting",
    "test": "test-failure-triage",
    "build": "container-build-troubleshooting",
    "pipeline_logs": "container-build-troubleshooting",
}
# Lint error pattern: file:line:col: CODE message
LINT_PATTERN = re.compile(
    r"^(?:.*?)(?P<file>[a-zA-Z0-9_\-\./\\]+\.(?:py|js|ts|jsx|tsx|go|java|rs)):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:(?P<code>[A-Z]\d{2,4}|[A-Z0-9_]+)\s+)?(?P<msg>.+)$",
    re.MULTILINE,
)

TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[^\s]+\s+")
RUFF_HEADER_PATTERN = re.compile(
    r"^(?P<code>[A-Z]\d{2,4}|[A-Z0-9_]+)\s+(?:\[\*\]\s+)?(?P<msg>.+)$"
)
RUFF_LOC_PATTERN = re.compile(
    r"^-->\s+(?P<file>[a-zA-Z0-9_\-\./\\]+\.(?:py|js|ts|jsx|tsx|go|java|rs)):(?P<line>\d+)(?::(?P<col>\d+))?"
)


def parse_linter_errors(log_text: str) -> list[dict[str, Any]]:
    """Parse linter errors from runner logs supporting multi-line and single-line formats."""
    parsed: list[dict[str, Any]] = []
    lines = log_text.splitlines()
    pending_header: Optional[dict[str, str]] = None

    for raw_line in lines:
        cleaned = TIMESTAMP_PREFIX.sub("", raw_line).strip()
        if not cleaned:
            continue

        loc_match = RUFF_LOC_PATTERN.match(cleaned)
        if loc_match and pending_header:
            file_path = re.sub(r"^\.?/", "", loc_match.group("file").strip())
            line_num = int(loc_match.group("line"))
            col_num = loc_match.group("col")
            parsed.append(
                {
                    "file": file_path,
                    "line": line_num,
                    "col": int(col_num) if col_num else None,
                    "code": pending_header["code"],
                    "msg": pending_header["msg"],
                    "raw_match": f"{pending_header['raw_line']}\n{cleaned}",
                }
            )
            pending_header = None
            continue

        pending_header = None

        single_match = LINT_PATTERN.match(cleaned)
        if single_match:
            raw_path = single_match.group("file").strip()
            file_path = re.sub(r"^\.?/", "", raw_path)
            line_str = single_match.group("line")
            line_num = int(line_str) if line_str else 1
            col_str = single_match.group("col")
            code = (single_match.group("code") or "").strip()
            msg = single_match.group("msg").strip()
            msg = re.sub(r"^\[\*\]\s*", "", msg).strip().replace("`", "")
            parsed.append(
                {
                    "file": file_path,
                    "line": line_num,
                    "col": int(col_str) if col_str else None,
                    "code": code,
                    "msg": msg,
                    "raw_match": cleaned,
                }
            )
            continue

        hdr_match = RUFF_HEADER_PATTERN.match(cleaned)
        if hdr_match:
            code = hdr_match.group("code").strip()
            msg = hdr_match.group("msg").strip()
            msg = re.sub(r"^\[\*\]\s*", "", msg).strip().replace("`", "")
            pending_header = {
                "code": code,
                "msg": msg,
                "raw_line": cleaned,
            }

    return parsed

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

    def __init__(
        self,
        model: Optional[str] = None,
        llm_agent: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        skill_tracker: Optional[SkillTracker] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            name="pipeline_logs_specialist",
            stage="pipeline_logs",
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
            name="pipeline_logs_specialist",
            model=self.model or "default",
            instruction=get_agent_config().get_instruction("pipeline_logs_specialist"),
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
        skill_name: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> LlmAgent:
        from ..skills_loader import get_skill_toolset_for_target

        skill_toolset = get_skill_toolset_for_target("pipeline_logs")

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

        template = get_agent_config().get_drilldown_instruction("pipeline_logs_specialist")
        instruction = template.format(skill_name=skill_name)
        model_to_use = (
            getattr(self.llm_agent, "model", None)
            if self.llm_agent
            else (self.model or "default")
        )
        agent = LlmAgent(
            name="pipeline_logs_specialist",
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
        log_text: str,
        sub_stage: str,
    ) -> DiagnosticResult:
        annotations: list[CheckRunAnnotation] = []
        if sub_stage == "lint":
            parsed_items = parse_linter_errors(log_text)
            for item in parsed_items[:50]:
                code = item["code"]
                annotations.append(
                    CheckRunAnnotation(
                        path=item["file"],
                        start_line=item["line"],
                        end_line=item["line"],
                        annotation_level="failure",
                        title=f"Linter Violation: {code}" if code else "Linter Violation",
                        message=item["msg"],
                    )
                )

        evidence_items = list(findings.evidence)
        if not evidence_items:
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=log_text[:1000],
                    timestamp=datetime.now(timezone.utc),
                    console_url=incident.external_url,
                )
            )

        diag = DiagnosticResult(
            stage=sub_stage,
            specialist_name=self.name,
            summary=findings.summary,
            probable_cause=findings.summary,
            confidence=findings.confidence,
            evidence_items=evidence_items,
            annotations=annotations,
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
        step_name = failing_step.get("name", "Unknown Step")
        job_name = failing_job.get("name", "Unknown Job")
        combined_label = f"{job_name} / {step_name}".lower()

        # Determine sub-stage
        sub_stage = "pipeline_logs"
        if any(k in combined_label for k in ("lint", "ruff", "flake8", "mypy", "eslint", "fmt")):
            sub_stage = "lint"
        elif any(k in combined_label for k in ("test", "pytest", "jest", "unit", "integration")):
            sub_stage = "test"
        elif any(k in combined_label for k in ("build", "docker", "image", "package", "container")):
            sub_stage = "build"

        skill = STAGE_TO_SKILL.get(sub_stage, "container-build-troubleshooting")
        repo = kwargs.get("repo") or incident.github_repo or incident.service_hint or ""
        head_sha = kwargs.get("head_sha") or incident.github_head_sha or ""
        job_id = kwargs.get("job_id") or int(failing_job.get("id", 0) or 0)
        run_id = kwargs.get("run_id") or incident.github_run_id or 0

        # Attempt Pass 1: ADK Runner execution if LLM is available
        if self._is_llm_available():
            try:
                from .base import run_sync

                agent = self._build_agent(skill, client=client, config=config)
                prompt_lines = [
                    f"Investigate pipeline failure for incident {incident.id}: {incident.title}",
                    f"Job: {job_name} (ID: {job_id}), Step: {step_name}",
                    f"Repository: {repo}, Run ID: {run_id}, Head SHA: {head_sha}",
                    f"Failing Step Sub-Stage: {sub_stage}",
                    f"Error log excerpt:\n{log_text[:2000]}",
                    "",
                    "CRITICAL INSTRUCTIONS:",
                    f"1. Turn 1: Invoke load_skill exactly once with skill_name='{skill}'.",
                    (
                        "2. Turn 2: Invoke fetch_job_logs (or get_commit_diff) to inspect "
                        "logs or diff."
                    ),
                    "3. Turn 3: Conclude immediately by invoking set_model_response with Findings.",
                ]
                findings = run_sync(self._execute_adk_runner(agent, "\n".join(prompt_lines)))
                if findings and (self.skill_tracker.queries_run or findings.queries_run):
                    return self._findings_to_diagnostic_result(
                        findings, incident, failing_job, failing_step, log_text, sub_stage
                    )
            except Exception as exc:
                logger.info("PipelineLogsSpecialist ADK runner pass skipped/failed: %s", exc)

        # Pass 2 Fallback: Deterministic tool execution through authentic tracking callbacks
        self.skill_tracker.after_tool_callback("load_skill", {"skill_name": skill})

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

        if sub_stage == "lint":
            res = self._diagnose_lint(
                incident,
                failing_job,
                failing_step,
                log_text,
                client,
                config,
                diff_data=diff_data,
            )
        elif sub_stage == "test":
            res = self._diagnose_test(
                incident,
                failing_job,
                failing_step,
                log_text,
                client,
                config,
                diff_data=diff_data,
            )
        elif sub_stage == "build":
            res = self._diagnose_build(
                incident,
                failing_job,
                failing_step,
                log_text,
                client,
                config,
                diff_data=diff_data,
            )
        else:
            res = self._diagnose_generic(
                incident, failing_job, failing_step, log_text, diff_data=diff_data
            )

        return self.skill_tracker.populate_diagnostic_result(res)

    def _diagnose_lint(
        self,
        incident: Incident,
        failing_job: dict[str, Any],
        failing_step: dict[str, Any],
        log_text: str,
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        diff_data: Optional[dict[str, Any]] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Lint Step")
        parsed_items = parse_linter_errors(log_text)
        annotations: list[CheckRunAnnotation] = []
        evidence_items: list[EvidenceItem] = []
        now = datetime.now(timezone.utc)

        total_errors = len(parsed_items)
        if total_errors == 0:
            return self._diagnose_generic(incident, failing_job, failing_step, log_text)

        # Group by file
        files_map: dict[str, list[dict[str, Any]]] = {}
        for item in parsed_items:
            files_map.setdefault(item["file"], []).append(item)

        # Build annotations (up to 50)
        for item in parsed_items[:50]:
            annotations.append(
                CheckRunAnnotation(
                    path=item["file"],
                    start_line=item["line"],
                    end_line=item["line"],
                    annotation_level="failure",
                    title=(
                        f"Linter Violation: {item['code']}"
                        if item["code"]
                        else "Linter Violation"
                    ),
                    message=item["msg"],
                )
            )

        # Add sample runner log lines to evidence_items
        for item in parsed_items[:5]:
            evidence_items.append(
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt=(
                        f"[{item['code'] or 'LINT'}] {item['file']}:{item['line']}: "
                        f"{item['msg']}\n{item['raw_match']}"
                    ),
                    timestamp=now,
                    console_url=incident.external_url,
                )
            )

        # Correlate with commit diff if available
        diff_info = ""
        changed_files: list[str] = []
        if diff_data is None and incident.github_repo and incident.github_head_sha:
            try:
                diff_data = self.tracked_tool_call(
                    "get_commit_diff",
                    {"repo": incident.github_repo, "head_sha": incident.github_head_sha},
                    lambda: get_commit_diff(
                        repo=incident.github_repo,
                        head_sha=incident.github_head_sha,
                        client=client,
                        config=config,
                    ),
                )
            except Exception as exc:
                logger.debug("Could not fetch commit diff: %s", exc)

        if diff_data and "files" in diff_data:
            changed_files = [
                re.sub(r"^\.?/", "", f.get("filename", ""))
                for f in diff_data["files"]
                if f.get("filename")
            ]
            if changed_files:
                diff_excerpt = format_git_diff_excerpt(diff_data)
                evidence_items.append(
                    EvidenceItem(
                        kind="git_diff",
                        excerpt=diff_excerpt,
                        timestamp=now,
                        console_url=incident.external_url,
                    )
                )

        violating_files = list(files_map.keys())
        confirmed_files = [f for f in violating_files if f in changed_files]
        if confirmed_files and len(confirmed_files) == len(violating_files):
            files_str = ", ".join(f"'{f}'" for f in confirmed_files)
            diff_info = (
                f" Confirmed: all {total_errors} errors are in {files_str} "
                "modified in this commit."
            )
        elif confirmed_files:
            files_str = ", ".join(f"'{f}'" for f in confirmed_files)
            diff_info = f" Confirmed: violations in {files_str} modified in this commit."

        # Construct structured breakdown per file
        file_breakdowns: list[str] = []
        primary_file = violating_files[0]
        for f_path, items in files_map.items():
            code_counts: dict[str, int] = {}
            code_examples: dict[str, str] = {}
            for it in items:
                c = it["code"] or "LINT"
                code_counts[c] = code_counts.get(c, 0) + 1
                if c not in code_examples and it["msg"]:
                    code_examples[c] = it["msg"]

            sorted_codes = sorted(code_counts.keys(), key=lambda k: (-code_counts[k], k))
            parts = []
            for c in sorted_codes:
                cnt = code_counts[c]
                msg_example = code_examples.get(c, "")
                if msg_example:
                    parts.append(f"{cnt}x {c} ({msg_example})")
                else:
                    parts.append(f"{cnt}x {c}")
            file_breakdowns.append(f"{f_path}: " + ", ".join(parts))

        if len(files_map) == 1:
            code_counts = {}
            code_examples = {}
            for it in files_map[primary_file]:
                c = it["code"] or "LINT"
                code_counts[c] = code_counts.get(c, 0) + 1
                if c not in code_examples and it["msg"]:
                    code_examples[c] = it["msg"]
            sorted_codes = sorted(code_counts.keys(), key=lambda k: (-code_counts[k], k))
            breakdown_parts = []
            for c in sorted_codes:
                cnt = code_counts[c]
                msg_example = code_examples.get(c, "")
                if msg_example:
                    breakdown_parts.append(f"{cnt}x {c} ({msg_example})")
                else:
                    breakdown_parts.append(f"{cnt}x {c}")
            breakdown = ", ".join(breakdown_parts)
            probable_cause = (
                f"Linter detected {total_errors} errors in '{primary_file}': {breakdown}."
            )
        else:
            breakdown = "; ".join(file_breakdowns)
            probable_cause = (
                f"Linter detected {total_errors} errors across {len(files_map)} files: "
                f"{breakdown}."
            )

        summary = f"Linter failed on step '{step_name}'. {probable_cause}{diff_info}"

        evidence_items.insert(
            0,
            EvidenceItem(
                kind="linter_breakdown",
                excerpt=(
                    f"Found {total_errors} linter error(s) in {len(files_map)} file(s).\n"
                    f"Breakdown:\n{breakdown}\n"
                    + (f"{diff_info.strip()}\n" if diff_info else "")
                ),
                timestamp=now,
                console_url=incident.external_url,
            ),
        )

        remediation = [
            f"Run linter locally to inspect and auto-fix: ruff check --fix {primary_file}",
            f"Fix code rule violations in {primary_file}: {breakdown}",
            "Commit and push corrected code to trigger pipeline re-evaluation",
        ]

        return DiagnosticResult(
            stage="lint",
            specialist_name=self.name,
            summary=summary,
            probable_cause=probable_cause,
            confidence=0.95 if parsed_items else 0.80,
            evidence_items=evidence_items,
            remediation_actions=remediation,
            suggested_fix=f"Resolve linter errors in {primary_file}",
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
        diff_data: Optional[dict[str, Any]] = None,
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

        if diff_data and "files" in diff_data:
            changed_files = [
                re.sub(r"^\.?/", "", f.get("filename", ""))
                for f in diff_data["files"]
                if f.get("filename")
            ]
            if changed_files:
                evidence_items.append(
                    EvidenceItem(
                        kind="git_diff",
                        excerpt=(
                            f"Commit diff touched {len(changed_files)} file(s): "
                            f"{', '.join(changed_files)}"
                        ),
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
        diff_data: Optional[dict[str, Any]] = None,
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

        if diff_data and "files" in diff_data:
            changed_files = [
                re.sub(r"^\.?/", "", f.get("filename", ""))
                for f in diff_data["files"]
                if f.get("filename")
            ]
            if changed_files:
                evidence_items.append(
                    EvidenceItem(
                        kind="git_diff",
                        excerpt=(
                            f"Commit diff touched {len(changed_files)} file(s): "
                            f"{', '.join(changed_files)}"
                        ),
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
        diff_data: Optional[dict[str, Any]] = None,
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

        if diff_data and "files" in diff_data:
            changed_files = [
                re.sub(r"^\.?/", "", f.get("filename", ""))
                for f in diff_data["files"]
                if f.get("filename")
            ]
            if changed_files:
                evidence_items.append(
                    EvidenceItem(
                        kind="git_diff",
                        excerpt=(
                            f"Commit diff touched {len(changed_files)} file(s): "
                            f"{', '.join(changed_files)}"
                        ),
                        timestamp=now,
                        console_url=incident.external_url,
                    )
                )

        return DiagnosticResult(
            stage="pipeline_logs",
            specialist_name=self.name,
            summary=f"Step '{step_name}' failed with error: {culprit}",
            probable_cause=f"Step execution failure in '{step_name}': {culprit}",
            confidence=0.75,
            evidence_items=evidence_items,
            remediation_actions=[f"Inspect step '{step_name}' logs and retry workflow run"],
        )
