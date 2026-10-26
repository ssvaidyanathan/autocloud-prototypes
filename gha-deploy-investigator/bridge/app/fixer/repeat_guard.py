"""Repeat-fix guard: stop automated remediation when a merged auto-fix itself fails.

A failing run on the repository's default branch whose head commit is the merge of an
auto-fix pull request (or a commit authored by the configured auto-fix bot account) means
the previous automated remediation did not work. Generating another fix would start a
retry loop, so the bridge skips fix selection and hands the incident to a human.

Detection signals, checked in order (the first that matches wins):

1. ``merge_message``: the commit message is a GitHub merge message for a branch carrying
   the auto-fix prefix (``Merge pull request #N from <owner>/<prefix>...`` or
   ``Merge branch '<prefix>...'``).
2. ``associated_pull_request``: ``GET /commits/{sha}/pulls`` lists a merged pull request
   whose head branch carries the auto-fix prefix. This covers squash and rebase merges,
   whose commit message does not name the branch.
3. ``bot_author``: the commit's author or committer login equals ``autofix_bot_login``.

The auto-fix branch prefix is :data:`~.interface.AUTOFIX_BRANCH_PREFIX`, the same value
used to name fix branches. The default branch comes from ``GET /repos/{owner}/{repo}``.

Lookup errors never propagate. Each failed lookup is logged and the guard decides from the
signals it could read. If the default branch cannot be read, the guard cannot tell whether
the run is on it and does not apply; the normal auto-fix path then runs (fail open). Runs
on auto-fix branches never reach this code: the webhook routes them to the CI listener.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..contracts.incident import Incident
from .interface import AUTOFIX_BRANCH_PREFIX, FixPRWriterBase, normalize_branch_ref

logger = logging.getLogger(__name__)

OUTCOME = "repeat_autofix_detected"
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
_MERGE_BRANCH_RE = re.compile(r"^Merge branch '([^']+)'")


@dataclass(frozen=True)
class RepeatAutofixDetection:
    """Facts about a failing default-branch commit that came from an auto-fix."""

    commit_sha: str
    default_branch: str
    run_id: str
    signal: str
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    bot_login: Optional[str] = None


def _branch_from_merge_message(message: str, prefix: str) -> Optional[tuple[Optional[int], str]]:
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    match = _MERGE_PR_RE.match(first_line)
    if match:
        # GitHub writes "<owner>/<branch>"; the branch itself may contain slashes.
        _, _, branch = match.group(2).partition("/")
        if branch.startswith(prefix):
            return int(match.group(1)), branch
        return None
    match = _MERGE_BRANCH_RE.match(first_line)
    if match and match.group(1).startswith(prefix):
        return None, match.group(1)
    return None


def _autofix_pull(pulls: list[dict[str, Any]], prefix: str) -> Optional[tuple[int, str]]:
    for pull in pulls:
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        ref = head.get("ref") if isinstance(head, dict) else None
        number = pull.get("number")
        if (
            isinstance(ref, str)
            and ref.startswith(prefix)
            and pull.get("merged_at")
            and isinstance(number, int)
        ):
            return number, ref
    return None


def _logins(commit: dict[str, Any]) -> list[str]:
    logins: list[str] = []
    for role in ("author", "committer"):
        account = commit.get(role)
        if isinstance(account, dict) and isinstance(account.get("login"), str):
            logins.append(account["login"])
    return logins


def detect_repeat_autofix(
    incident: Incident,
    default_branch: Optional[str],
    commit: Optional[dict[str, Any]],
    pulls: list[dict[str, Any]],
    bot_login: Optional[str],
    branch_prefix: str = AUTOFIX_BRANCH_PREFIX,
) -> Optional[RepeatAutofixDetection]:
    """Return the detection when the failing commit came from an auto-fix, else None."""
    sha = incident.github_head_sha or ""
    if not sha or not default_branch:
        return None
    if normalize_branch_ref(incident.github_branch) != default_branch:
        return None
    run_id = str(
        incident.github_run_id if incident.github_run_id is not None else incident.external_id
    )
    base = {"commit_sha": sha, "default_branch": default_branch, "run_id": run_id}

    commit = commit if isinstance(commit, dict) else {}
    details = commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
    message = details.get("message") if isinstance(details, dict) else None
    if isinstance(message, str):
        merged = _branch_from_merge_message(message, branch_prefix)
        if merged is not None:
            pr_number, branch = merged
            if pr_number is None:
                from_pull = _autofix_pull(pulls, branch_prefix)
                if from_pull is not None and from_pull[1] == branch:
                    pr_number = from_pull[0]
            return RepeatAutofixDetection(
                **base, signal="merge_message", pr_number=pr_number, branch=branch
            )

    from_pull = _autofix_pull(pulls, branch_prefix)
    if from_pull is not None:
        return RepeatAutofixDetection(
            **base, signal="associated_pull_request", pr_number=from_pull[0], branch=from_pull[1]
        )

    wanted = (bot_login or "").strip().lower()
    if wanted:
        for login in _logins(commit):
            if login.lower() == wanted:
                return RepeatAutofixDetection(**base, signal="bot_author", bot_login=login)
    return None


def format_repeat_guard_work_note(detection: RepeatAutofixDetection) -> str:
    """Facts-only ServiceNow work note for a detected repeat auto-fix failure.

    With a known PR number and branch this is the approved template verbatim. Without a PR
    number the note names the branch; for a bot-authored commit with no auto-fix branch it
    names the bot account. The remaining sentences are unchanged.
    """
    sha = detection.commit_sha[:7]
    if detection.pr_number is not None and detection.branch:
        origin = f"is a merge of auto-fix PR #{detection.pr_number} (branch {detection.branch})"
    elif detection.branch:
        origin = f"is a merge of auto-fix branch {detection.branch} (no associated PR number found)"
    else:
        origin = f"was authored by the auto-fix account {detection.bot_login}"
    return (
        f"[Auto-Fix Repeat Failure Guard] Failing commit {sha} on '{detection.default_branch}' "
        f"{origin}. The previous automated remediation was ineffective and the workflow run "
        f"{detection.run_id} failed. Halting automated fix attempts to prevent retry loops. "
        "Requires human attention."
    )


def _lookup(incident_id: str, name: str, call: Any, default: Any) -> Any:
    try:
        return call()
    except Exception as exc:
        logger.warning(
            "autofix.repeat_guard incident=%s lookup=%s result=error error=%r",
            incident_id,
            name,
            exc,
        )
        return default


def check_repeat_autofix(
    incident: Incident, writer: FixPRWriterBase, bot_login: Optional[str]
) -> Optional[RepeatAutofixDetection]:
    """Read commit metadata through ``writer`` and run :func:`detect_repeat_autofix`.

    Never raises; see the module docstring for the lookup-error behaviour.
    """
    owner, _, repo = (incident.github_repo or "").partition("/")
    sha = incident.github_head_sha
    if not owner or not repo or not sha:
        logger.info(
            "autofix.repeat_guard incident=%s result=skipped reason=no_repo_or_sha", incident.id
        )
        return None

    default_branch = _lookup(
        incident.id, "default_branch", lambda: writer.fetch_default_branch(owner, repo), None
    )
    if not isinstance(default_branch, str) or not default_branch:
        logger.info(
            "autofix.repeat_guard incident=%s result=not_applied reason=default_branch_unknown",
            incident.id,
        )
        return None
    if normalize_branch_ref(incident.github_branch) != default_branch:
        logger.info(
            "autofix.repeat_guard incident=%s result=not_applied reason=not_default_branch "
            "branch=%s default_branch=%s",
            incident.id,
            incident.github_branch,
            default_branch,
        )
        return None

    commit = _lookup(incident.id, "commit", lambda: writer.fetch_commit(owner, repo, sha), None)
    pulls = _lookup(
        incident.id, "commit_pulls", lambda: writer.fetch_commit_pulls(owner, repo, sha), []
    )
    detection = detect_repeat_autofix(
        incident,
        default_branch,
        commit if isinstance(commit, dict) else None,
        [p for p in pulls if isinstance(p, dict)] if isinstance(pulls, list) else [],
        bot_login,
    )
    if detection is None:
        logger.info("autofix.repeat_guard incident=%s result=clear", incident.id)
    else:
        logger.warning(
            "autofix.repeat_guard incident=%s result=%s signal=%s sha=%s pr_number=%s branch=%s",
            incident.id,
            OUTCOME,
            detection.signal,
            detection.commit_sha,
            detection.pr_number,
            detection.branch,
        )
    return detection


__all__ = [
    "OUTCOME",
    "RepeatAutofixDetection",
    "check_repeat_autofix",
    "detect_repeat_autofix",
    "format_repeat_guard_work_note",
]
