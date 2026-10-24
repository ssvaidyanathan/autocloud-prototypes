"""Choose which FixProposal (if any) the bridge should execute for a completed investigation.

Order of preference:
1. The agent's own proposal (``report.fix_proposal``) when its status is ``proposed``.
2. The deterministic lint fixer (S1) when lint diagnostics are present in the evidence.
3. The deterministic memory-config fixer (S4) for deploy-stage memory exhaustion.

File contents are read through a read-only ``fetch_file(path)`` callback bound to the
failing commit. Fallback fixers never raise: any failure means "no proposal".
"""

import logging
import re
from typing import Callable, Optional

from ..contracts.incident import Incident
from ..contracts.report import FixProposal, Report
from .s1_lint import generate_lint_fix
from .s4_config import generate_memory_config_fix, is_editable_path, is_oom_failure

logger = logging.getLogger(__name__)

FetchFile = Callable[[str], Optional[str]]

# Upper bound on files fetched to look for tunable memory settings.
MAX_CONFIG_CANDIDATES: int = 10
_CONFIG_PATH_RE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.(?:sh|bash|ya?ml|json|toml|tf|tfvars|conf|cfg|ini|properties))\b"
)


def candidate_config_paths(report: Report) -> list[str]:
    """Collect editable configuration file paths referenced in the report, in order."""
    texts: list[str] = [report.summary, report.impact]
    if report.probable_root_cause:
        texts.append(report.probable_root_cause.text)
    texts.extend(report.causal_remediation)
    texts.extend(report.recommended_actions)
    texts.extend(change.description for change in report.recent_changes)
    for items in report.evidence_by_area.values():
        texts.extend(item.excerpt for item in items if item.excerpt)
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
    incident: Incident, report: Report, fetch_file: FetchFile
) -> Optional[FixProposal]:
    """Return the proposal to execute, or None when nothing actionable is available."""
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
            "autofix.candidate incident=%s candidate=agent result=rejected status=%s rationale=%s",
            incident.id,
            agent_proposal.status,
            agent_proposal.rationale,
        )

    fallbacks: list[tuple[str, Callable[[], Optional[FixProposal]]]] = [
        ("deterministic_linter", lambda: _lint_proposal(incident, report, fetch_file)),
        ("config_tuning", lambda: _memory_proposal(incident, report, fetch_file)),
    ]
    for name, build in fallbacks:
        try:
            proposal = build()
        except Exception as exc:
            logger.warning(
                "autofix.candidate incident=%s candidate=%s result=error error=%s",
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
            "autofix.candidate incident=%s candidate=%s result=rejected status=%s rationale=%s",
            incident.id,
            name,
            proposal.status,
            proposal.rationale,
        )
    logger.info("autofix.chosen incident=%s candidate=none", incident.id)
    return None


__all__ = ["MAX_CONFIG_CANDIDATES", "candidate_config_paths", "select_fix_proposal"]
