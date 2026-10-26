"""CI feedback listener for auto-fix verification runs.

When CI completes on an ``autofix/*`` branch, this listener records the outcome on
the originating ServiceNow incident (and optionally on GitHub). It never triggers a
new investigation or a new fix attempt (exactly one fix attempt per incident), and
it never closes or resolves the incident: the incident stays In Progress for human
review.
"""

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from ..adapters.github import GitHubAdapter
from ..adapters.servicenow import ServiceNowAdapter
from ..config import BridgeConfig, get_settings
from .interface import AUTOFIX_BRANCH_PREFIX, is_autofix_branch

logger = logging.getLogger(__name__)

SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success"})
FAILURE_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out"})

_RUN_ID_PATTERN = re.compile(rf"^{re.escape(AUTOFIX_BRANCH_PREFIX)}run-(\d+)(?:$|[/-])")


def parse_original_run_id(branch: Optional[str]) -> Optional[str]:
    """Extract the original failing run id from an ``autofix/run-{id}`` branch name."""
    if not branch:
        return None
    match = _RUN_ID_PATTERN.match(branch)
    return match.group(1) if match else None


def build_success_note(branch: str, run_url: str) -> str:
    return (
        f"[Auto-Fix Verification] Auto-fix PR on branch {branch} PASSED CI verification "
        f"({run_url}). Ready for human review and merge."
    )


def build_failure_note(branch: str, run_url: str) -> str:
    return (
        f"[Auto-Fix Verification] Auto-fix PR on branch {branch} FAILED CI verification "
        f"({run_url}). Requires human attention. No further automated fix will be attempted."
    )


def build_inconclusive_note(branch: str, run_url: str, unverified_jobs: list[str]) -> str:
    return (
        f"[Auto-Fix Verification] Auto-fix PR on branch {branch} verification INCONCLUSIVE: "
        f"{', '.join(repr(j) for j in unverified_jobs)} did not run on the fix branch ({run_url}); "
        "requires human validation."
    )


@dataclass
class AutofixRunInfo:
    """Fields extracted from a ``workflow_run`` webhook for an auto-fix branch."""

    run_id: Optional[int]
    run_attempt: int
    action: str
    head_branch: str
    head_sha: str
    conclusion: Optional[str]
    html_url: str
    repo_full_name: str
    original_run_id: Optional[str]
    pr_numbers: list[int] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AutofixRunInfo":
        run = payload.get("workflow_run") or {}
        repo = payload.get("repository") or {}
        repo_full = str(repo.get("full_name") or repo.get("name") or "")
        branch = str(run.get("head_branch") or "")

        pr_numbers: list[int] = []
        pr_urls: list[str] = []
        for pr in run.get("pull_requests") or []:
            if not isinstance(pr, dict):
                continue
            number = pr.get("number")
            if isinstance(number, int):
                pr_numbers.append(number)
                if repo_full:
                    pr_urls.append(f"https://github.com/{repo_full}/pull/{number}")
            elif pr.get("url"):
                pr_urls.append(str(pr["url"]))

        raw_run_id = run.get("id")
        run_attempt = run.get("run_attempt") or 1
        return cls(
            run_id=int(raw_run_id) if raw_run_id is not None else None,
            run_attempt=int(run_attempt),
            action=str(payload.get("action") or ""),
            head_branch=branch,
            head_sha=str(run.get("head_sha") or ""),
            conclusion=run.get("conclusion"),
            html_url=str(run.get("html_url") or ""),
            repo_full_name=repo_full,
            original_run_id=parse_original_run_id(branch),
            pr_numbers=pr_numbers,
            pr_urls=pr_urls,
        )


@dataclass
class CIFeedbackOutcome:
    """Result of processing an auto-fix verification webhook."""

    outcome: str
    branch: str
    conclusion: Optional[str]
    incident_sys_id: Optional[str] = None
    incident_number: Optional[str] = None
    work_note_posted: bool = False
    github_writeback_posted: bool = False
    detail: Optional[str] = None


class CIFeedbackListener:
    """Records auto-fix CI verification results on ServiceNow and GitHub."""

    def __init__(
        self,
        settings: Optional[BridgeConfig] = None,
        servicenow_adapter: Optional[ServiceNowAdapter] = None,
        github_adapter: Optional[GitHubAdapter] = None,
    ) -> None:
        self._settings = settings
        self._servicenow_adapter = servicenow_adapter
        self._github_adapter = github_adapter
        self._processed: set[tuple[str, int, int]] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _servicenow(self, settings: BridgeConfig) -> ServiceNowAdapter:
        return self._servicenow_adapter or ServiceNowAdapter(config=settings)

    def _github(self, settings: BridgeConfig) -> GitHubAdapter:
        return self._github_adapter or GitHubAdapter(config=settings)

    def _mark_processed(self, info: AutofixRunInfo) -> bool:
        """Return False if this run/attempt/conclusion was already processed."""
        if info.run_id is None:
            return True
        key = (info.repo_full_name, info.run_id, info.run_attempt)
        with self._lock:
            if key in self._processed:
                return False
            self._processed.add(key)
            return True

    def _unmark_processed(self, info: AutofixRunInfo) -> None:
        if info.run_id is None:
            return
        with self._lock:
            self._processed.discard((info.repo_full_name, info.run_id, info.run_attempt))

    def resolve_incident(
        self, info: AutofixRunInfo, settings: BridgeConfig
    ) -> Optional[dict[str, Any]]:
        """Find the ServiceNow incident created for the original failing run.

        Incidents are created with ``correlation_id=gdi:{repo}:{run_id}:{run_attempt}``;
        the auto-fix branch encodes the original run id, so the most recent incident
        whose correlation id starts with ``gdi:{repo}:{run_id}:`` is the target.
        """
        if not info.original_run_id or not info.repo_full_name:
            return None
        base_url = (settings.servicenow_instance_url or "").rstrip("/")
        if not base_url:
            logger.warning("ServiceNow instance URL not configured; cannot resolve incident")
            return None

        adapter = self._servicenow(settings)
        endpoint = f"{base_url}/api/now/table/incident"
        prefix = f"gdi:{info.repo_full_name}:{info.original_run_id}:"
        query = f"correlation_idSTARTSWITH{prefix}^ORDERBYDESCsys_created_on"

        def _do_lookup() -> Optional[dict[str, Any]]:
            with adapter._get_client() as client:
                resp = client.get(
                    endpoint,
                    params={"sysparm_query": query, "sysparm_limit": "1"},
                )
                resp.raise_for_status()
                records = resp.json().get("result", [])
                if isinstance(records, dict):
                    records = [records]
                if isinstance(records, list):
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        corr = str(record.get("correlation_id") or "")
                        # Guard against instances that ignore STARTSWITH filters; an empty
                        # correlation id never identifies the original incident.
                        if corr and corr.startswith(prefix):
                            return dict(record)
                return None

        result: Optional[dict[str, Any]] = adapter._execute_with_retry(_do_lookup)
        return result

    def post_work_note(self, sys_id: str, note: str, settings: BridgeConfig) -> None:
        """Append a work note without changing incident state (stays In Progress)."""
        base_url = (settings.servicenow_instance_url or "").rstrip("/")
        adapter = self._servicenow(settings)
        endpoint = f"{base_url}/api/now/table/incident/{sys_id}"

        def _do_patch() -> None:
            with adapter._get_client() as client:
                resp = client.patch(endpoint, json={"work_notes": note})
                resp.raise_for_status()

        adapter._execute_with_retry(_do_patch)

    def post_github_feedback(
        self,
        info: AutofixRunInfo,
        passed: Optional[bool],
        settings: BridgeConfig,
        unverified_jobs: Optional[list[str]] = None,
    ) -> bool:
        """Comment on the auto-fix PR (or head commit) with the verification result."""
        repo_str = info.repo_full_name
        if not repo_str or "/" not in repo_str:
            return False
        owner, repo_name = repo_str.split("/", 1)
        api_url = (settings.github_api_url or "https://api.github.com").rstrip("/")

        if info.pr_numbers:
            comment_url = (
                f"{api_url}/repos/{owner}/{repo_name}/issues/{info.pr_numbers[0]}/comments"
            )
        elif info.head_sha:
            comment_url = f"{api_url}/repos/{owner}/{repo_name}/commits/{info.head_sha}/comments"
        else:
            return False

        if unverified_jobs:
            verdict = "INCONCLUSIVE"
            body = (
                f"### Auto-Fix Verification: {verdict}\n\n"
                f"CI on branch `{info.head_branch}` verification {verdict.lower()} "
                f"([run]({info.html_url or '#'})). Job(s) "
                f"{', '.join(repr(j) for j in unverified_jobs)} "
                "did not run on the fix branch; requires human validation."
            )
        else:
            verdict = "PASSED" if passed else "FAILED"
            follow_up = (
                "Ready for human review and merge."
                if passed
                else "Requires human attention. No further automated fix will be attempted."
            )
            body = (
                f"### Auto-Fix Verification: {verdict}\n\n"
                f"CI on branch `{info.head_branch}` {verdict.lower()} "
                f"([run]({info.html_url or '#'})). {follow_up}"
            )

        adapter = self._github(settings)
        token = adapter._get_writeback_token(repo_str)
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with adapter._get_client() as client:
            resp = client.post(comment_url, json={"body": body}, headers=headers)
            resp.raise_for_status()
        return True

    def find_unverified_jobs(self, info: AutofixRunInfo, settings: BridgeConfig) -> list[str]:
        """Return jobs that failed in the original run but did not succeed in the fix run.

        Job names are discovered dynamically from the original run's failing jobs. A job
        that was skipped, cancelled, or absent on the fix branch is unverified. Returns an
        empty list when the comparison cannot be made (missing ids, API errors).
        """
        if not info.original_run_id or info.run_id is None:
            return []
        github = self._github(settings)
        try:
            original_jobs = github.get_workflow_run_jobs(info.repo_full_name, info.original_run_id)
            fix_jobs = github.get_workflow_run_jobs(info.repo_full_name, info.run_id)
        except Exception as exc:
            logger.warning("Failed to fetch workflow run jobs for verification comparison: %s", exc)
            return []

        failing_original_jobs: list[str] = []
        for job in original_jobs:
            name = job.get("name")
            if name and job.get("conclusion") in FAILURE_CONCLUSIONS:
                if name not in failing_original_jobs:
                    failing_original_jobs.append(str(name))
        if not failing_original_jobs:
            return []

        fix_job_map = {j["name"]: j.get("conclusion") for j in fix_jobs if "name" in j}
        return [
            name
            for name in failing_original_jobs
            if fix_job_map.get(name) not in SUCCESS_CONCLUSIONS
        ]

    # --------------------------------------------------------------- entrypoint

    def handle_autofix_workflow_run(
        self, payload: dict[str, Any], settings: Optional[BridgeConfig] = None
    ) -> CIFeedbackOutcome:
        """Process a ``workflow_run`` event for an ``autofix/*`` branch.

        Never raises for downstream (ServiceNow/GitHub) errors so the webhook can
        acknowledge GitHub with HTTP 200 and avoid redelivery loops.
        """
        cfg = settings or self._settings or get_settings()
        info = AutofixRunInfo.from_payload(payload)
        branch = info.head_branch
        conclusion = info.conclusion

        if not is_autofix_branch(branch):
            return CIFeedbackOutcome("not_autofix_branch", branch, conclusion)

        if info.action != "completed":
            logger.info("Auto-fix run on %s not completed (action=%s)", branch, info.action)
            return CIFeedbackOutcome("pending", branch, conclusion)

        if conclusion in SUCCESS_CONCLUSIONS:
            passed = True
            note = build_success_note(branch, info.html_url)
        elif conclusion in FAILURE_CONCLUSIONS:
            passed = False
            note = build_failure_note(branch, info.html_url)
        else:
            logger.info("Ignoring auto-fix run on %s with conclusion=%s", branch, conclusion)
            return CIFeedbackOutcome("ignored_conclusion", branch, conclusion)

        if not self._mark_processed(info):
            logger.info("Duplicate auto-fix verification event for %s; skipping", branch)
            return CIFeedbackOutcome("duplicate_ignored", branch, conclusion)

        unverified_jobs = self.find_unverified_jobs(info, cfg) if passed else []
        if unverified_jobs:
            logger.warning(
                "Auto-fix verification on %s inconclusive; jobs did not run: %s",
                branch,
                unverified_jobs,
            )
            outcome_kind = "verification_inconclusive"
            note = build_inconclusive_note(branch, info.html_url, unverified_jobs)
        else:
            outcome_kind = "verification_passed" if passed else "verification_failed"
        outcome = CIFeedbackOutcome(outcome_kind, branch, conclusion)

        try:
            incident = self.resolve_incident(info, cfg)
        except Exception as exc:
            logger.error("Failed to resolve ServiceNow incident for %s: %s", branch, exc)
            incident = None
            outcome.detail = f"incident lookup failed: {exc}"

        if incident and incident.get("sys_id"):
            outcome.incident_sys_id = str(incident["sys_id"])
            outcome.incident_number = incident.get("number")
            try:
                self.post_work_note(outcome.incident_sys_id, note, cfg)
                outcome.work_note_posted = True
            except Exception as exc:
                logger.error(
                    "Failed to post auto-fix verification note to %s: %s",
                    outcome.incident_sys_id,
                    exc,
                )
                outcome.detail = f"work note failed: {exc}"
        elif outcome.detail is None:
            logger.warning(
                "No ServiceNow incident found for auto-fix branch %s (repo=%s)",
                branch,
                info.repo_full_name,
            )
            outcome.detail = "incident not found"

        if not outcome.work_note_posted:
            # Allow a GitHub redelivery to retry the ServiceNow update.
            self._unmark_processed(info)

        if cfg.enable_github_writeback:
            try:
                if unverified_jobs:
                    outcome.github_writeback_posted = self.post_github_feedback(
                        info, passed=None, settings=cfg, unverified_jobs=unverified_jobs
                    )
                else:
                    outcome.github_writeback_posted = self.post_github_feedback(info, passed, cfg)
            except Exception as exc:
                logger.warning("Failed to post auto-fix verification to GitHub: %s", exc)

        return outcome
