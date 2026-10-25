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

# ---------------------------------------------------------------------------
# Decisive log excerpt selection
# ---------------------------------------------------------------------------

# Lines emitted by the GitHub Actions runner after the step body finished
# (post-step cleanup, credential removal, orphan-process teardown).
RUNNER_CLEANUP_LINE_PATTERN = re.compile(
    r"^(?:"
    r"(?:##\[(?:group|endgroup)\])?\s*post[- ]job clean ?up"
    r"|##\[endgroup\]\s*$"
    r"|cleaning up orphan process"
    r"|terminate orphan process"
    r"|removing credentials? config"
    r"|(?:removing|cleaning up|revoking)\b.*\bcredentials?\b"
    r"|.*\bgit\b.*\bconfig\b.*--unset"
    r"|finishing:"
    r"|\[command\]\S*git(?:\s|$)"
    r"|git version \d"
    r"|temporarily overriding home="
    r"|adding repository directory to the temporary git global config"
    r"|http\.https://\S+\.extraheader\s*$"
    r")",
    re.IGNORECASE,
)
# Section header that opens the runner post-job cleanup block.
RUNNER_CLEANUP_ANCHOR_PATTERN = re.compile(
    r"^(?:##\[group\])?\s*post[- ]job clean ?up", re.IGNORECASE
)
# Generic error indicators across build tools, test runners, and shells.
# Upper-case-only forms (``FAIL``, ``FAILED``, ``Error N``) are scoped case-sensitive so
# that prose such as "0 failed" does not match; zero-count forms (``failed: 0``,
# ``0 FAILED``, ``FAILED: 0``) are excluded explicitly.
ERROR_MARKER_PATTERN = re.compile(
    r"error:|failed:(?!\s*0\b)|fatal:|failure:|\berror\s*\[|\[error\]|\berr:|\berr!"
    r"|exception:|traceback \(most recent call last\)"
    r"|\berror\s+(?-i:[A-Z]+\d+):"
    r"|(?-i:(?<!\b0\s)(?<!\b0\s\s)\bFAIL(?:ED)?\b(?!\s*:\s*0\b))"
    r"|(?-i:\bError [1-9]\d*\s*$)"
    r"|\bcommand not found\b",
    re.IGNORECASE,
)
# Runner summary lines that only restate the exit status, not the cause.
GENERIC_EXIT_LINE_PATTERN = re.compile(
    r"process completed with exit code \d+|exited with (?:exit )?code \d+",
    re.IGNORECASE,
)
# Lines that announce which command a step is running.
COMMAND_HEADER_PATTERN = re.compile(r"^(?:##\[group\])?Run\s|^\[command\]")

EXCERPT_CONTEXT_BEFORE = 6
EXCERPT_CONTEXT_AFTER = 10
EXCERPT_GAP_MARKER = "..."


def strip_runner_cleanup(log_text: str) -> list[str]:
    """Return log lines (timestamps removed) with trailing runner cleanup stripped.

    Everything from the first post-job cleanup header that follows the last error
    marker is dropped, then remaining trailing cleanup/blank lines are removed from
    the bottom up until real step output is reached.
    """
    lines = [TIMESTAMP_PREFIX.sub("", raw).rstrip() for raw in log_text.splitlines()]

    last_marker = -1
    for idx, line in enumerate(lines):
        if ERROR_MARKER_PATTERN.search(line):
            last_marker = idx
    for idx in range(last_marker + 1, len(lines)):
        if RUNNER_CLEANUP_ANCHOR_PATTERN.match(lines[idx].strip()):
            lines = lines[:idx]
            break

    while lines and (
        not lines[-1].strip() or RUNNER_CLEANUP_LINE_PATTERN.match(lines[-1].strip())
    ):
        lines.pop()
    return lines


def _render_excerpt(lines: list[str], selected: set[int]) -> str:
    rendered: list[str] = []
    previous: Optional[int] = None
    for idx in sorted(selected):
        if previous is not None and idx != previous + 1:
            rendered.append(EXCERPT_GAP_MARKER)
        rendered.append(lines[idx])
        previous = idx
    return "\n".join(rendered)


class _ExcerptSelection:
    """Set of selected line indices with incrementally tracked rendered size.

    The rendered size matches ``len(_render_excerpt(lines, selected))`` without
    re-rendering: line lengths, plus one ``EXCERPT_GAP_MARKER`` between
    non-contiguous segments, plus one newline between consecutive rendered items.
    """

    def __init__(self, lines: list[str], max_chars: int) -> None:
        self.lines = lines
        self.max_chars = max_chars
        self.selected: set[int] = set()
        self._chars = 0
        self._segments = 0

    def _size(self, chars: int, count: int, segments: int) -> int:
        if count == 0:
            return 0
        gaps = segments - 1
        return chars + gaps * len(EXCERPT_GAP_MARKER) + (count + gaps - 1)

    @property
    def chars_used(self) -> int:
        """Rendered size of the current selection."""
        return self._size(self._chars, len(self.selected), self._segments)

    def try_add(self, idx: int) -> bool:
        """Add ``idx`` if the rendered excerpt still fits; return whether it is selected."""
        if idx in self.selected:
            return True
        has_prev = idx - 1 in self.selected
        has_next = idx + 1 in self.selected
        segments = self._segments + 1 - int(has_prev) - int(has_next)
        chars = self._chars + len(self.lines[idx])
        if self._size(chars, len(self.selected) + 1, segments) > self.max_chars:
            return False
        self.selected.add(idx)
        self._chars = chars
        self._segments = segments
        return True


def _tail_excerpt(lines: list[str], max_chars: int) -> str:
    start = _tail_start_index(lines, max_chars)
    if start >= len(lines):
        return lines[-1][-max_chars:] if lines else ""
    return "\n".join(lines[start:])


def _tail_start_index(lines: list[str], max_chars: int) -> int:
    """Return the earliest line index ``_tail_excerpt(lines, max_chars)`` would include."""
    size = 0
    start = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        cost = len(lines[idx]) + (1 if start < len(lines) else 0)
        if size + cost > max_chars:
            break
        size += cost
        start = idx
    return start


def _error_marker_indices(lines: list[str]) -> tuple[list[int], list[int]]:
    """Return ``(markers, decisive)`` line indices.

    ``markers`` are lines matching ``ERROR_MARKER_PATTERN``; ``decisive`` is the
    subset that is not a generic exit-status restatement.
    """
    markers = [idx for idx, line in enumerate(lines) if ERROR_MARKER_PATTERN.search(line)]
    decisive = [idx for idx in markers if not GENERIC_EXIT_LINE_PATTERN.search(lines[idx])]
    return markers, decisive


def extract_decisive_log_excerpt(log_text: str, max_chars: int = 1500) -> str:
    """Select the error-bearing portion of a step log within ``max_chars``.

    1. Strips trailing runner post-step cleanup output.
    2. If decisive error markers exist (markers other than generic exit-status
       lines), keeps the decisive error lines plus the command header and
       surrounding context, prioritising the error lines themselves. Leftover
       budget is then filled in this order, each stage stopping at the first
       line that no longer fits:

       a. the contiguous lead-up, read backwards from the line before the last
          decisive marker (compiler/build causes usually precede the summary
          error);
       b. the inter-marker middle, read forwards from the first decisive marker
          up to the last one;
       c. for a terminal marker only, the cleaned tail after the last decisive
          marker, read backwards from the end of the log.

       For a terminal marker, up to half the budget is reserved for the
       contiguous lead-up (the last decisive line and the lines before it)
       before any other marker's context is added. Guarantee: a cause within
       ``max_chars // 2`` characters above the last decisive marker is kept,
       regardless of the number of earlier false positives.

       If the last decisive marker is non-terminal (it precedes the window a
       plain tail excerpt would cover, or real non-exit output follows its
       ``EXCERPT_CONTEXT_AFTER`` context lines), a small core is reserved first:
       the first and last decisive lines, the line immediately before and after
       each, and the command header. All remaining budget then goes to the
       contiguous cleaned tail, read backwards from the end, before wider
       context, the lead-up (a) and the middle (b); stage (c) is skipped.
       Guarantee: a cause within the last
       ``max_chars - core_chars`` characters of the cleaned log is kept, where
       ``core_chars`` is the rendered size of the core plus its separator
       (``EXCERPT_GAP_MARKER`` and newlines) before the tail.
    3. Otherwise (no markers, or only generic exit lines) falls back to the
       cleaned tail of the log.
    """
    if not log_text:
        return ""
    lines = strip_runner_cleanup(log_text)
    if not any(line.strip() for line in lines):
        return ""

    clip = max(max_chars - len(EXCERPT_GAP_MARKER), 1)
    lines = [
        line if len(line) <= max_chars else line[:clip] + EXCERPT_GAP_MARKER for line in lines
    ]

    markers, decisive = _error_marker_indices(lines)
    if not decisive:
        return _tail_excerpt(lines, max_chars)
    decisive_set = set(decisive)
    generic = [idx for idx in markers if idx not in decisive_set]

    # Each entry is (line index, expansion key). Context expansion around a marker
    # stops in a direction once a line no longer fits, keeping context contiguous.
    priority: list[tuple[int, Optional[tuple[int, int]]]] = [
        (idx, None) for idx in [decisive[-1], decisive[0], *reversed(decisive), *generic]
    ]
    header: list[int] = []
    for idx in range(decisive[0] - 1, -1, -1):
        if COMMAND_HEADER_PATTERN.match(lines[idx].strip()):
            header.append(idx)
            break
    priority.extend((idx, None) for idx in header)
    for distance in range(1, max(EXCERPT_CONTEXT_BEFORE, EXCERPT_CONTEXT_AFTER) + 1):
        for idx in reversed(decisive):
            if distance <= EXCERPT_CONTEXT_BEFORE:
                priority.append((idx - distance, (idx, -1)))
            if distance <= EXCERPT_CONTEXT_AFTER:
                priority.append((idx + distance, (idx, 1)))

    # The last decisive marker is non-terminal when it precedes the window a plain
    # tail excerpt would cover, or when real output follows its trailing context
    # (e.g. a false-positive config echo above an unrecognised cause). Then only a
    # small core (the first/last decisive lines, their adjacent lines and the
    # command header) is reserved before the contiguous cleaned tail takes all
    # remaining budget, so an unrecognised cause near the end is kept.
    has_trailing_activity = any(
        lines[i].strip() and not GENERIC_EXIT_LINE_PATTERN.search(lines[i])
        for i in range(decisive[-1] + EXCERPT_CONTEXT_AFTER + 1, len(lines))
    )
    is_non_terminal = (
        decisive[-1] < _tail_start_index(lines, max_chars) or has_trailing_activity
    )

    selection = _ExcerptSelection(lines, max_chars)
    tail = range(len(lines) - 1, decisive[-1], -1)
    if is_non_terminal:
        core = [decisive[-1], decisive[0], *header]
        for marker in (decisive[-1], decisive[0]):
            core += [marker - 1, marker + 1]
        for idx in core:
            if 0 <= idx < len(lines):
                selection.try_add(idx)
        for idx in range(len(lines) - 1, -1, -1):
            if not selection.try_add(idx):
                break
    else:
        # Terminal marker: reserve up to half the budget for the contiguous lead-up
        # before other markers' context can spend it.
        for idx in [decisive[-1], *range(decisive[-1] - 1, -1, -1)]:
            if selection.chars_used > max_chars // 2 or not selection.try_add(idx):
                break
    blocked: set[tuple[int, int]] = set()
    for idx, key in priority:
        if idx < 0 or idx >= len(lines) or idx in selection.selected:
            continue
        if key is not None and key in blocked:
            continue
        if not selection.try_add(idx) and key is not None:
            blocked.add(key)
    if not selection.selected:
        return lines[decisive[-1]][:max_chars]

    # Spend leftover budget on the contiguous lead-up to the last decisive marker
    # first (compiler causes usually precede the summary error), then on the
    # inter-marker middle, so an earlier false-positive marker cannot starve the
    # lead-up. For a terminal marker the remaining cleaned tail follows; for a
    # non-terminal one it is already seeded.
    lead_up = range(decisive[-1] - 1, -1, -1)
    middle = range(decisive[0], decisive[-1])
    for fill in (lead_up, middle) if is_non_terminal else (lead_up, middle, tail):
        for idx in fill:
            if not selection.try_add(idx):
                break
    return _render_excerpt(lines, selection.selected)


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
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
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

        # Ground downstream synthesis in the commit diff even when the ADK
        # runner's findings omitted it.
        if not any(e.kind == "git_diff" for e in evidence_items):
            diff_data = self._resolve_diff_data(incident, None, client, config)
            if diff_data:
                self._append_git_diff_evidence(
                    evidence_items, diff_data, incident, datetime.now(timezone.utc)
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
                from ..agent_config import get_agent_config
                from .base import run_sync

                agent = self._build_agent(skill, client=client, config=config)
                template = get_agent_config().get_prompt_template("pipeline_logs_runner")
                runner_prompt = template.format(
                    incident_id=incident.id,
                    incident_title=incident.title,
                    job_name=job_name,
                    job_id=job_id,
                    step_name=step_name,
                    repo=repo,
                    run_id=run_id,
                    head_sha=head_sha,
                    sub_stage=sub_stage,
                    log_excerpt=log_text[:2000],
                    skill_name=skill,
                )
                findings = run_sync(self._execute_adk_runner(agent, runner_prompt))
                if findings and (self.skill_tracker.queries_run or findings.queries_run):
                    return self._findings_to_diagnostic_result(
                        findings,
                        incident,
                        failing_job,
                        failing_step,
                        log_text,
                        sub_stage,
                        client=client,
                        config=config,
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
                incident,
                failing_job,
                failing_step,
                log_text,
                client,
                config,
                diff_data=diff_data,
            )

        return self.skill_tracker.populate_diagnostic_result(res)

    def _resolve_diff_data(
        self,
        incident: Incident,
        diff_data: Optional[dict[str, Any]],
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
    ) -> Optional[dict[str, Any]]:
        """Return diff_data, fetching the incident's commit diff when not already provided."""
        if diff_data is not None or not (incident.github_repo and incident.github_head_sha):
            return diff_data
        try:
            return self.tracked_tool_call(
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
            return None

    @staticmethod
    def _append_git_diff_evidence(
        evidence_items: list[EvidenceItem],
        diff_data: Optional[dict[str, Any]],
        incident: Incident,
        now: datetime,
    ) -> list[str]:
        """Append a git_diff evidence item carrying unified diff hunks.

        Returns the normalized list of changed file paths (empty if no diff).
        """
        if not (isinstance(diff_data, dict) and "files" in diff_data):
            return []
        changed_files = [
            re.sub(r"^\.?/", "", f.get("filename", ""))
            for f in diff_data["files"]
            if isinstance(f, dict) and f.get("filename")
        ]
        if changed_files:
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
        return changed_files

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
            return self._diagnose_generic(
                incident,
                failing_job,
                failing_step,
                log_text,
                client,
                config,
                diff_data=diff_data,
            )

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
        diff_data = self._resolve_diff_data(incident, diff_data, client, config)
        changed_files = self._append_git_diff_evidence(evidence_items, diff_data, incident, now)

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

        tail_log = extract_decisive_log_excerpt(log_text, max_chars=1500)
        evidence_items: list[EvidenceItem] = []
        evidence_items.append(
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"Test '{failing_test}' failed: {assertion_msg}\n{tail_log}",
                timestamp=now,
                console_url=incident.external_url,
            )
        )

        diff_data = self._resolve_diff_data(incident, diff_data, client, config)
        self._append_git_diff_evidence(evidence_items, diff_data, incident, now)

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

        build_log_tail = extract_decisive_log_excerpt(log_text, max_chars=1500)
        evidence_items.append(
            EvidenceItem(
                kind="github_runner_log",
                excerpt=f"Build failure in '{step_name}': {probable_cause}\n{build_log_tail}",
                timestamp=now,
                console_url=incident.external_url,
            )
        )

        diff_data = self._resolve_diff_data(incident, diff_data, client, config)
        self._append_git_diff_evidence(evidence_items, diff_data, incident, now)

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
        client: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        diff_data: Optional[dict[str, Any]] = None,
    ) -> DiagnosticResult:
        step_name = failing_step.get("name", "Unknown Step")
        lines = [line.strip() for line in strip_runner_cleanup(log_text) if line.strip()]
        markers, decisive = _error_marker_indices(lines)
        if decisive:
            culprit = lines[decisive[-1]]
        elif markers:
            culprit = lines[markers[-1]]
        elif lines:
            culprit = lines[-1]
        else:
            culprit = "Process terminated with non-zero exit code"

        now = datetime.now(timezone.utc)
        evidence_items = [
            EvidenceItem(
                kind="github_runner_log",
                excerpt=(
                    f"Step '{step_name}' failed: {culprit}\n"
                    f"{extract_decisive_log_excerpt(log_text, max_chars=1500)}"
                ),
                timestamp=now,
                console_url=incident.external_url,
            )
        ]

        diff_data = self._resolve_diff_data(incident, diff_data, client, config)
        self._append_git_diff_evidence(evidence_items, diff_data, incident, now)

        return DiagnosticResult(
            stage="pipeline_logs",
            specialist_name=self.name,
            summary=f"Step '{step_name}' failed with error: {culprit}",
            probable_cause=f"Step execution failure in '{step_name}': {culprit}",
            confidence=0.75,
            evidence_items=evidence_items,
            remediation_actions=[f"Inspect step '{step_name}' logs and retry workflow run"],
        )
