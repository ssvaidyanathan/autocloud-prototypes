"""GitHub REST implementation of the auto-fix PR writer.

This module is the ONLY place in the system holding repository write capability.
Investigator specialists remain strictly read-only.
"""

import base64
import difflib
import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import FilePatch, FixProposal, PullRequestRef, Report
from ..github.auth import GitHubAppAuth
from .diff_apply import apply_unified_diff
from .interface import (
    MAX_LINE_DELTA,
    FixPRWriterBase,
    build_autofix_branch_name,
    is_autofix_branch,
    normalize_patch_path,
    validate_fix_guardrails,
)

logger = logging.getLogger(__name__)


class FixBranchExistsError(RuntimeError):
    """Raised when the autofix branch for an incident already exists (one attempt per run)."""


def committed_line_delta(before: str, after: str) -> int:
    """Count lines added plus removed between two file versions."""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    return sum(
        (i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )


def resolve_patch_content(patch: FilePatch, base_content: Optional[str]) -> str:
    """Return the exact file content a patch writes, given the current base content.

    The ``diff`` is authoritative whenever present: a ``content`` that disagrees with
    ``apply(base, diff)`` is rejected so the guardrail-measured diff is what gets written.
    """
    base = base_content or ""
    if patch.diff:
        applied = apply_unified_diff(base, patch.diff)
        if patch.content is not None and patch.content != applied:
            raise ValueError(
                f"Patch for '{patch.path}' carries content that does not match its diff"
            )
        return applied
    if patch.content is not None:
        return patch.content
    raise ValueError(f"Patch for '{patch.path}' has neither content nor diff")


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

    def _get_file(
        self, owner: str, repo: str, encoded_path: str, ref: str
    ) -> Optional[dict[str, Any]]:
        resp = self._request("GET", owner, repo, f"/contents/{encoded_path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Path '{encoded_path}' is not a file")
        return data

    @staticmethod
    def _decode_content(data: dict[str, Any], path: str) -> str:
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise ValueError(f"Contents API did not return base64 content for '{path}'")
        return base64.b64decode(data["content"]).decode("utf-8")

    def fetch_file_content(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        clean_path, _ = normalize_patch_path(path)
        if clean_path is None:
            return None
        data = self._get_file(owner, repo, quote(clean_path, safe="/"), ref)
        if data is None:
            return None
        return self._decode_content(data, clean_path)

    def _resolve_patch(
        self, owner: str, repo: str, ref: str, patch: FilePatch
    ) -> tuple[str, str, Optional[str], str, int]:
        """Resolve a patch against ``ref``.

        Returns ``(clean_path, encoded_path, existing_sha, final_content, line_delta)``.
        """
        clean_path, reason = normalize_patch_path(patch.path)
        if clean_path is None:
            raise ValueError(f"Invalid patch path '{patch.path}': {reason}")
        encoded_path = quote(clean_path, safe="/")
        existing = self._get_file(owner, repo, encoded_path, ref)
        if patch.action == "modify" and existing is None:
            raise ValueError(f"Cannot modify missing file '{patch.path}'")
        if patch.action == "create" and existing is not None:
            raise ValueError(f"Cannot create existing file '{patch.path}'")
        base_content = self._decode_content(existing, clean_path) if existing else ""
        final_content = resolve_patch_content(patch, base_content)
        existing_sha = str(existing["sha"]) if existing and existing.get("sha") else None
        delta = committed_line_delta(base_content, final_content)
        return clean_path, encoded_path, existing_sha, final_content, delta

    def resolve_proposal(
        self, owner: str, repo: str, ref: str, proposal: FixProposal
    ) -> list[FilePatch]:
        """Resolve every patch to full content at ``ref`` and enforce the committed delta.

        Raises ``ValueError`` if any patch does not apply or the total number of lines
        actually written exceeds ``MAX_LINE_DELTA``.
        """
        resolved: list[FilePatch] = []
        total_delta = 0
        for patch in proposal.patches:
            if patch.action == "delete":
                raise ValueError(f"Delete patches are not permitted: '{patch.path}'")
            clean_path, _, _, final_content, delta = self._resolve_patch(owner, repo, ref, patch)
            total_delta += delta
            resolved.append(FilePatch(path=clean_path, action=patch.action, content=final_content))
        if total_delta > MAX_LINE_DELTA:
            raise ValueError(
                f"Committed change ({total_delta} lines) exceeds maximum allowed ({MAX_LINE_DELTA})"
            )
        return resolved

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
        if patch.action == "delete":
            clean_path, reason = normalize_patch_path(patch.path)
            if clean_path is None:
                raise ValueError(f"Invalid patch path '{patch.path}': {reason}")
            encoded_path = quote(clean_path, safe="/")
            existing = self._get_file(owner, repo, encoded_path, branch)
            if existing is None or not existing.get("sha"):
                raise ValueError(f"Cannot delete missing file '{patch.path}'")
            resp = self._request(
                "DELETE",
                owner,
                repo,
                f"/contents/{encoded_path}",
                json={"message": message, "sha": str(existing["sha"]), "branch": branch},
            )
        else:
            _, encoded_path, existing_sha, final_content, delta = self._resolve_patch(
                owner, repo, branch, patch
            )
            if delta > MAX_LINE_DELTA:
                raise ValueError(
                    f"Committed change to '{patch.path}' ({delta} lines) exceeds maximum "
                    f"allowed ({MAX_LINE_DELTA})"
                )
            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(final_content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if existing_sha:
                body["sha"] = existing_sha
            resp = self._request("PUT", owner, repo, f"/contents/{encoded_path}", json=body)

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
            logger.info("autofix.writer incident=%s outcome=skipped reason=disabled", incident.id)
            return None

        if proposal.status != "proposed" or not proposal.patches:
            logger.info(
                "autofix.writer incident=%s outcome=skipped reason=not_actionable "
                "status=%s patches=%d",
                incident.id,
                proposal.status,
                len(proposal.patches),
            )
            return None

        valid, reason = validate_fix_guardrails(proposal)
        if not valid:
            logger.warning(
                "autofix.guardrail incident=%s result=rejected reason=%r", incident.id, reason
            )
            return None
        logger.info("autofix.guardrail incident=%s result=passed", incident.id)

        if is_autofix_branch(incident.github_branch) or is_autofix_branch(
            proposal.target_branch_base
        ):
            logger.warning(
                "autofix.guardrail incident=%s result=rejected reason=autofix_branch_loop",
                incident.id,
            )
            return None

        repo_parts = _split_repo(incident.github_repo)
        if repo_parts is None or incident.github_run_id is None or not incident.github_head_sha:
            logger.warning(
                "autofix.writer incident=%s outcome=skipped reason=missing_repo_run_or_sha",
                incident.id,
            )
            return None
        owner, repo = repo_parts
        run_id = incident.github_run_id

        # Measure what will actually be written (R-2) before any branch exists.
        try:
            resolved_patches = self.resolve_proposal(
                owner, repo, incident.github_head_sha, proposal
            )
        except Exception as exc:
            logger.warning(
                "autofix.guardrail incident=%s result=rejected stage=resolve reason=%r",
                incident.id,
                exc,
            )
            return None
        logger.info(
            "autofix.guardrail incident=%s result=passed stage=resolve files=%d",
            incident.id,
            len(resolved_patches),
        )

        try:
            branch = self.create_fix_branch(owner, repo, incident.github_head_sha, run_id)
        except FixBranchExistsError:
            existing_branch = build_autofix_branch_name(run_id)
            existing = self.find_existing_pull_request(owner, repo, existing_branch)
            logger.info(
                "autofix.branch incident=%s run=%s outcome=exists branch=%s existing_pr=%s",
                incident.id,
                run_id,
                existing_branch,
                existing.pr_url if existing else "none",
            )
            return existing
        except Exception as exc:
            logger.error(
                "autofix.branch incident=%s run=%s outcome=error error=%r", incident.id, run_id, exc
            )
            return None
        logger.info(
            "autofix.branch incident=%s run=%s outcome=created branch=%s",
            incident.id,
            run_id,
            branch,
        )

        try:
            for patch in resolved_patches:
                commit_sha = self.commit_patch(
                    owner,
                    repo,
                    branch,
                    patch,
                    f"fix: {proposal.strategy} remediation for {patch.path} (run {run_id})",
                )
                logger.info(
                    "autofix.commit incident=%s branch=%s path=%s sha=%s",
                    incident.id,
                    branch,
                    patch.path,
                    commit_sha,
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
            logger.error(
                "autofix.pr incident=%s run=%s outcome=error branch=%s error=%r "
                "action=delete_partial_branch",
                incident.id,
                run_id,
                branch,
                exc,
            )
            self._delete_branch(owner, repo, branch)
            return None

        logger.info(
            "autofix.pr incident=%s run=%s outcome=opened pr_number=%d pr_url=%s branch=%s",
            incident.id,
            run_id,
            pr.pr_number,
            pr.pr_url,
            branch,
        )
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


__all__ = [
    "FixBranchExistsError",
    "FixPRWriter",
    "committed_line_delta",
    "resolve_patch_content",
]
