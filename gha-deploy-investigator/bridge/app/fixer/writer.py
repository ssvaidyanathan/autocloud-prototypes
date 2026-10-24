"""GitHub REST implementation of the auto-fix PR writer.

This module is the ONLY place in the system holding repository write capability.
Investigator specialists remain strictly read-only.
"""

import base64
import logging
from typing import Any, Optional

import httpx

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import FilePatch, FixProposal, PullRequestRef, Report
from ..github.auth import GitHubAppAuth
from .interface import (
    FixPRWriterBase,
    build_autofix_branch_name,
    is_autofix_branch,
    validate_fix_guardrails,
)

logger = logging.getLogger(__name__)


class FixBranchExistsError(RuntimeError):
    """Raised when the autofix branch for an incident already exists (one attempt per run)."""


def _split_repo(repo_full: Optional[str]) -> Optional[tuple[str, str]]:
    if not repo_full or "/" not in repo_full:
        return None
    owner, name = repo_full.split("/", 1)
    if not owner or not name:
        return None
    return owner, name


class FixPRWriter(FixPRWriterBase):
    """Creates autofix branches, commits patches, and opens PRs via the GitHub REST API."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        http_client: Optional[httpx.Client] = None,
        auth: Optional[GitHubAppAuth] = None,
    ) -> None:
        self.config = config or get_settings()
        self._http_client = http_client
        self._auth = auth
        if self._auth is None and self.config.github_app_id and self.config.github_app_private_key:
            self._auth = GitHubAppAuth(
                app_id=self.config.github_app_id,
                private_key_pem=self.config.github_app_private_key,
                config=self.config,
            )

    # ------------------------------------------------------------------ helpers

    @property
    def _api_url(self) -> str:
        return (self.config.github_api_url or "https://api.github.com").rstrip("/")

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            timeout = getattr(self.config, "adapter_timeout_seconds", 30.0)
            self._http_client = httpx.Client(timeout=timeout)
        return self._http_client

    def _headers(self, owner: str, repo: str) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token: Optional[str] = self.config.github_token
        if not token and self._auth is not None:
            token = self._auth.get_installation_token_for_repo(
                f"{owner}/{repo}", client=self._client(), base_url=self._api_url
            )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(
        self, method: str, owner: str, repo: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        url = f"{self._api_url}/repos/{owner}/{repo}{path}"
        return self._client().request(method, url, headers=self._headers(owner, repo), **kwargs)

    def _get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        resp = self._request("GET", owner, repo, f"/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("sha"):
            return str(data["sha"])
        return None

    def _delete_branch(self, owner: str, repo: str, branch: str) -> None:
        try:
            resp = self._request("DELETE", owner, repo, f"/git/refs/heads/{branch}")
            if resp.status_code not in (204, 404):
                logger.warning(
                    "Failed to clean up autofix branch %s (status %d)", branch, resp.status_code
                )
        except Exception as exc:
            logger.warning("Failed to clean up autofix branch %s: %s", branch, exc)

    def find_existing_pull_request(
        self, owner: str, repo: str, head_branch: str
    ) -> Optional[PullRequestRef]:
        """Return the PR already opened from head_branch, if any."""
        resp = self._request(
            "GET",
            owner,
            repo,
            "/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "all"},
        )
        if resp.status_code != 200:
            return None
        pulls = resp.json()
        if not isinstance(pulls, list) or not pulls:
            return None
        return self._to_pr_ref(pulls[0], head_branch)

    @staticmethod
    def _to_pr_ref(data: dict[str, Any], head_branch: str) -> PullRequestRef:
        head = data.get("head") or {}
        return PullRequestRef(
            pr_number=int(data["number"]),
            pr_url=str(data.get("html_url") or data.get("url") or ""),
            branch_name=head_branch,
            head_sha=str(head.get("sha") or ""),
            status="opened",
        )

    # --------------------------------------------------------------- interface

    def create_fix_branch(self, owner: str, repo: str, base_sha: str, run_id: int | str) -> str:
        branch = build_autofix_branch_name(run_id)
        resp = self._request(
            "POST",
            owner,
            repo,
            "/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if resp.status_code == 422:
            # GitHub returns 422 "Reference already exists"; never overwrite a prior attempt.
            raise FixBranchExistsError(f"Autofix branch '{branch}' already exists")
        resp.raise_for_status()
        return branch

    def commit_patch(
        self, owner: str, repo: str, branch: str, patch: FilePatch, message: str
    ) -> str:
        existing_sha = self._get_file_sha(owner, repo, patch.path, branch)

        if patch.action == "delete":
            if existing_sha is None:
                raise ValueError(f"Cannot delete missing file '{patch.path}'")
            resp = self._request(
                "DELETE",
                owner,
                repo,
                f"/contents/{patch.path}",
                json={"message": message, "sha": existing_sha, "branch": branch},
            )
        else:
            if patch.content is None:
                raise ValueError(f"Patch for '{patch.path}' has no full file content")
            if patch.action == "modify" and existing_sha is None:
                raise ValueError(f"Cannot modify missing file '{patch.path}'")
            if patch.action == "create" and existing_sha is not None:
                raise ValueError(f"Cannot create existing file '{patch.path}'")
            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(patch.content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if existing_sha:
                body["sha"] = existing_sha
            resp = self._request("PUT", owner, repo, f"/contents/{patch.path}", json=body)

        resp.raise_for_status()
        commit = resp.json().get("commit") or {}
        return str(commit.get("sha") or "")

    def create_pull_request(
        self, owner: str, repo: str, title: str, body: str, head_branch: str, base_branch: str
    ) -> PullRequestRef:
        resp = self._request(
            "POST",
            owner,
            repo,
            "/pulls",
            json={"title": title, "body": body, "head": head_branch, "base": base_branch},
        )
        resp.raise_for_status()
        return self._to_pr_ref(resp.json(), head_branch)

    def execute_fix_proposal(
        self, incident: Incident, report: Report, proposal: FixProposal
    ) -> Optional[PullRequestRef]:
        if self.config.autofix_enabled is not True:
            logger.info("Auto-fix disabled by kill switch; skipping incident %s", incident.id)
            return None

        if proposal.status != "proposed" or not proposal.patches:
            logger.info(
                "Fix proposal for incident %s not actionable (status=%s, patches=%d)",
                incident.id,
                proposal.status,
                len(proposal.patches),
            )
            return None

        valid, reason = validate_fix_guardrails(proposal)
        if not valid:
            logger.warning("Fix proposal for incident %s rejected: %s", incident.id, reason)
            return None

        if is_autofix_branch(incident.github_branch) or is_autofix_branch(
            proposal.target_branch_base
        ):
            logger.warning(
                "Refusing to auto-fix a failure on autofix branch for incident %s", incident.id
            )
            return None

        repo_parts = _split_repo(incident.github_repo)
        if repo_parts is None or incident.github_run_id is None or not incident.github_head_sha:
            logger.warning(
                "Incident %s lacks repo/run_id/head_sha; cannot open fix PR", incident.id
            )
            return None
        owner, repo = repo_parts
        run_id = incident.github_run_id

        try:
            branch = self.create_fix_branch(owner, repo, incident.github_head_sha, run_id)
        except FixBranchExistsError:
            existing_branch = build_autofix_branch_name(run_id)
            existing = self.find_existing_pull_request(owner, repo, existing_branch)
            logger.info(
                "Autofix already attempted for run %s (existing PR: %s); not retrying",
                run_id,
                existing.pr_url if existing else "none",
            )
            return existing
        except Exception as exc:
            logger.error("Failed to create autofix branch for run %s: %s", run_id, exc)
            return None

        try:
            for patch in proposal.patches:
                self.commit_patch(
                    owner,
                    repo,
                    branch,
                    patch,
                    f"fix: {proposal.strategy} remediation for {patch.path} (run {run_id})",
                )
            pr = self.create_pull_request(
                owner,
                repo,
                title=f"fix: automated remediation for run {run_id}",
                body=self._build_pr_body(incident, report, proposal),
                head_branch=branch,
                base_branch=proposal.target_branch_base,
            )
        except Exception as exc:
            logger.error("Auto-fix for run %s failed; removing partial branch: %s", run_id, exc)
            self._delete_branch(owner, repo, branch)
            return None

        logger.info("Opened auto-fix PR #%d for run %s: %s", pr.pr_number, run_id, pr.pr_url)
        return pr

    @staticmethod
    def _build_pr_body(incident: Incident, report: Report, proposal: FixProposal) -> str:
        root_cause = (
            report.probable_root_cause.text if report.probable_root_cause else report.summary
        )
        files = "\n".join(f"- `{p.path}` ({p.action})" for p in proposal.patches)
        lines = [
            "## Automated Remediation",
            "",
            f"**Incident:** {incident.external_key}",
            f"**Workflow Run:** {incident.external_url or incident.github_run_id}",
            f"**Strategy:** `{proposal.strategy}`",
            "",
            "### Probable Root Cause",
            f"> {root_cause}",
            "",
            "### Rationale",
            proposal.rationale,
            "",
            "### Changed Files",
            files,
            "",
            "---",
            "*Opened automatically by gha-deploy-investigator. Review before merging.*",
        ]
        return "\n".join(lines)


__all__ = ["FixBranchExistsError", "FixPRWriter"]
