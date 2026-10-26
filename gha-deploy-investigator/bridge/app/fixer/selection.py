"""Choose which FixProposal (if any) the bridge should execute for a completed investigation.

Order of preference:
1. ``deterministic_linter``: the lint fixer (S1) when lint diagnostics are in the evidence.
2. ``commit_hunk_revert``: a hunk-scoped revert of the failing commit when the diagnosis
   names a configuration/build file that commit changed (see ``commit_hunk_revert``).
3. ``config_tuning``: the memory-config fixer (S4) for deploy-stage memory exhaustion.
4. ``agent``: the agent's own proposal (``report.fix_proposal``) when it is ``proposed``.

A deterministic fixer that claims the diagnosed failure takes precedence over the agent's
LLM patch; the agent proposal is only considered when no deterministic fixer applies.
Selection happens once, up front: if the chosen proposal later fails to apply, there is no
retry with another candidate.

File contents are read through a read-only ``fetch_file(path)`` callback bound to the
failing commit, and the failing commit object through a read-only ``fetch_commit()``
callback. Deterministic fixers never raise: any failure means "no proposal".
"""

import logging
import re
from typing import Any, Callable, Optional

from ..contracts.incident import Incident
from ..contracts.report import FixProposal, Report
from .commit_hunk_revert import generate_commit_hunk_revert
from .s1_lint import generate_lint_fix
from .s4_config import (
    CONFIG_FILE_EXTENSIONS,
    generate_memory_config_fix,
    is_editable_path,
    is_oom_failure,
)

logger = logging.getLogger(__name__)

FetchFile = Callable[[str], Optional[str]]
FetchCommit = Callable[[], Optional[dict[str, Any]]]

# Upper bound on files fetched to look for tunable memory settings.
MAX_CONFIG_CANDIDATES: int = 10
_CONFIG_PATH_RE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.(?:"
    + "|".join(re.escape(ext) for ext in CONFIG_FILE_EXTENSIONS)
    + r"))\b"
)
# Evidence kinds that reproduce the failing commit's diff. They name every changed file and
# line, so they must not count as the diagnosis pointing at a particular change.
COMMIT_DIFF_EVIDENCE_KINDS: frozenset[str] = frozenset({"git_diff"})


def _diagnosis_texts(report: Report) -> list[str]:
    """Report texts stating what failed and why: summary, root cause, causal remediation."""
    texts: list[str] = [report.summary]
    if report.probable_root_cause:
        texts.append(report.probable_root_cause.text)
    texts.extend(report.causal_remediation)
    return texts


def _evidence_texts(report: Report, exclude_kinds: frozenset[str] = frozenset()) -> list[str]:
    texts: list[str] = []
    for items in report.evidence_by_area.values():
        texts.extend(
            item.excerpt for item in items if item.excerpt and item.kind not in exclude_kinds
        )
    return texts


def candidate_config_paths(report: Report) -> list[str]:
    """Collect editable configuration file paths referenced in the report, in order."""
    diagnosis = _diagnosis_texts(report)
    texts: list[str] = [diagnosis[0], report.impact, *diagnosis[1:]]
    texts.extend(report.recommended_actions)
    texts.extend(change.description for change in report.recent_changes)
    texts.extend(_evidence_texts(report))
    found: list[str] = [ann.path for ann in report.annotations]
    for text in texts:
        found.extend(m.group(1) for m in _CONFIG_PATH_RE.finditer(text or ""))

    seen: set[str] = set()
    paths: list[str] = []
    for raw in found:
        path = raw.strip().removeprefix("./")
        if path and path not in seen and is_editable_path(path):
            seen.add(path)
            paths.append(path)
    return paths[:MAX_CONFIG_CANDIDATES]


def _safe_fetch(fetch_file: FetchFile, path: str) -> Optional[str]:
    try:
        content = fetch_file(path)
    except Exception as exc:
        logger.warning("Could not fetch %s for deterministic fix: %s", path, exc)
        return None
    return content if isinstance(content, str) else None


def _lint_proposal(incident: Incident, report: Report, fetch_file: FetchFile) -> FixProposal:
    return generate_lint_fix(incident, report, lambda path: _safe_fetch(fetch_file, path))


def _revert_proposal(
    incident: Incident, report: Report, fetch_commit: Optional[FetchCommit]
) -> Optional[FixProposal]:
    if fetch_commit is None:
        return None
    commit = fetch_commit()
    if not isinstance(commit, dict):
        return None
    annotation_texts: list[str] = []
    for ann in report.annotations:
        annotation_texts.extend(t for t in (ann.message, ann.title, ann.raw_details) if t)
    return generate_commit_hunk_revert(
        commit,
        diagnosis_texts=_diagnosis_texts(report),
        evidence_texts=annotation_texts + _evidence_texts(report, COMMIT_DIFF_EVIDENCE_KINDS),
        annotations=report.annotations,
        target_branch_base=incident.github_branch or "",
    )


def _memory_proposal(
    incident: Incident, report: Report, fetch_file: FetchFile
) -> Optional[FixProposal]:
    if not is_oom_failure(report):
        return None
    files: dict[str, str] = {}
    for path in candidate_config_paths(report):
        content = _safe_fetch(fetch_file, path)
        if content is not None:
            files[path] = content
    return generate_memory_config_fix(report, files, incident.github_branch or "")


def select_fix_proposal(
    incident: Incident,
    report: Report,
    fetch_file: FetchFile,
    fetch_commit: Optional[FetchCommit] = None,
) -> Optional[FixProposal]:
    """Return the proposal to execute, or None when nothing actionable is available."""
    deterministic_candidates: list[tuple[str, Callable[[], Optional[FixProposal]]]] = [
        ("deterministic_linter", lambda: _lint_proposal(incident, report, fetch_file)),
        ("commit_hunk_revert", lambda: _revert_proposal(incident, report, fetch_commit)),
        ("config_tuning", lambda: _memory_proposal(incident, report, fetch_file)),
    ]
    for name, build in deterministic_candidates:
        try:
            proposal = build()
        except Exception as exc:
            logger.warning(
                "autofix.candidate incident=%s candidate=%s result=error error=%r",
                incident.id,
                name,
                exc,
            )
            continue
        if proposal is None:
            logger.info(
                "autofix.candidate incident=%s candidate=%s result=not_applicable",
                incident.id,
                name,
            )
            continue
        if proposal.status == "proposed":
            logger.info(
                "autofix.chosen incident=%s candidate=%s strategy=%s patches=%d",
                incident.id,
                name,
                proposal.strategy,
                len(proposal.patches),
            )
            return proposal
        logger.info(
            "autofix.candidate incident=%s candidate=%s result=rejected status=%s rationale=%r",
            incident.id,
            name,
            proposal.status,
            proposal.rationale,
        )

    agent_proposal = report.fix_proposal
    if agent_proposal is None:
        logger.info("autofix.candidate incident=%s candidate=agent result=absent", incident.id)
    elif agent_proposal.status == "proposed":
        logger.info(
            "autofix.chosen incident=%s candidate=agent strategy=%s patches=%d",
            incident.id,
            agent_proposal.strategy,
            len(agent_proposal.patches),
        )
        return agent_proposal
    else:
        logger.info(
            "autofix.candidate incident=%s candidate=agent result=rejected status=%s rationale=%r",
            incident.id,
            agent_proposal.status,
            agent_proposal.rationale,
        )
    logger.info("autofix.chosen incident=%s candidate=none", incident.id)
    return None


__all__ = [
    "COMMIT_DIFF_EVIDENCE_KINDS",
    "MAX_CONFIG_CANDIDATES",
    "candidate_config_paths",
    "select_fix_proposal",
]
