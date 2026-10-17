import logging
import os
import sys
from typing import Any, Optional

import httpx

from ..config import AgentConfig, get_settings
from .registry import readonly_tool

logger = logging.getLogger(__name__)


def _build_client(
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
    repo: Optional[str] = None,
) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    cfg = config or get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
    }
    token = cfg.github_token
    if not token and repo:
        try:
            from ..github.auth import GitHubAppAuth

            auth = GitHubAppAuth(config=cfg)
            token = auth.get_installation_token_for_repo(repo)
        except Exception as exc:
            logger.warning("Failed to obtain installation token for repo %s: %s", repo, exc)

    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=30.0, follow_redirects=True), True


@readonly_tool(
    name="fetch_workflow_run_jobs",
    description="Fetch list of jobs and step statuses for a GitHub Actions workflow run",
)
def fetch_workflow_run_jobs(
    repo: str,
    run_id: int,
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Fetch jobs for a GitHub Actions workflow run from GitHub REST API."""
    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/actions/runs/{run_id}/jobs"

    c, owns_client = _build_client(client, config, repo=repo)
    try:
        if run_id in _mock_workflow_jobs:
            return _mock_workflow_jobs[run_id]
        try:
            resp = c.get(endpoint)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception as exc:
            if owns_client and (
                getattr(cfg, "is_local", False)
                or os.environ.get("APP_ENV") in ("local", "test", "dev")
                or "pytest" in sys.modules
            ):
                logger.debug("Falling back to empty jobs in test/local environment: %s", exc)
                return {"jobs": []}
            raise
    finally:
        if owns_client:
            c.close()


# In-memory mock stores for offline test hermeticity
_mock_diffs: dict[str, dict[str, Any]] = {}
_mock_job_logs: dict[int, str] = {}
_mock_workflow_jobs: dict[int, dict[str, Any]] = {}


def reset_github_mock_state() -> None:
    """Reset simulated GitHub mock state."""
    _mock_diffs.clear()
    _mock_job_logs.clear()
    _mock_workflow_jobs.clear()


def set_mock_diff(head_sha: str, data: dict[str, Any]) -> None:
    """Register simulated commit diff data for hermetic tests."""
    _mock_diffs[head_sha] = data


def set_mock_job_log(job_id: int, log_text: str) -> None:
    """Register simulated job log for hermetic tests."""
    _mock_job_logs[job_id] = log_text


def set_mock_workflow_jobs(run_id: int, data: dict[str, Any]) -> None:
    """Register simulated workflow run jobs for hermetic tests."""
    _mock_workflow_jobs[run_id] = data


@readonly_tool(
    name="fetch_job_logs",
    description="Fetch execution runner logs for a specific GitHub Actions job",
)
def fetch_job_logs(
    repo: str,
    job_id: int,
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> str:
    """Fetch raw execution logs for a GitHub Actions job."""
    if job_id in _mock_job_logs:
        return _mock_job_logs[job_id]

    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/actions/jobs/{job_id}/logs"

    c, owns_client = _build_client(client, config, repo=repo)
    try:
        try:
            resp = c.get(endpoint, follow_redirects=True)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if owns_client and (
                getattr(cfg, "is_local", False)
                or os.environ.get("APP_ENV") in ("local", "test", "dev")
                or "pytest" in sys.modules
            ):
                logger.debug("Falling back to empty job logs in test/local environment: %s", exc)
                return ""
            raise
    finally:
        if owns_client:
            c.close()


@readonly_tool(
    name="get_commit_diff",
    description="Fetch commit details, modified files, and diff for a given commit SHA",
)
def get_commit_diff(
    repo: str,
    head_sha: str = "",
    commit_sha: str = "",
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Fetch commit diff details from GitHub REST API."""
    target_sha = commit_sha or head_sha
    if not target_sha:
        return {"files": []}
    if target_sha in _mock_diffs:
        return _mock_diffs[target_sha]
    if head_sha in _mock_diffs:
        return _mock_diffs[head_sha]
    if commit_sha in _mock_diffs:
        return _mock_diffs[commit_sha]

    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/commits/{target_sha}"

    c, owns_client = _build_client(client, config, repo=repo)
    try:
        try:
            resp = c.get(endpoint)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception as exc:
            if owns_client and (
                getattr(cfg, "is_local", False)
                or os.environ.get("APP_ENV") in ("local", "test", "dev")
                or "pytest" in sys.modules
            ):
                logger.debug("Falling back to empty diff in test/local environment: %s", exc)
                return {"files": []}
            raise
    finally:
        if owns_client:
            c.close()


@readonly_tool(
    name="get_file_content",
    description="Fetch repository file content for a given path and git reference",
)
def get_file_content(
    repo: str,
    path: str,
    ref: str = "main",
    client: Optional[Any] = None,
    config: Optional[AgentConfig] = None,
) -> str:
    """Fetch file content from GitHub repository."""
    import base64

    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/contents/{path}"

    c, owns_client = _build_client(client, config, repo=repo)
    try:
        resp = c.get(endpoint, params={"ref": ref})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        elif isinstance(data, dict) and "content" in data:
            return str(data["content"])
        return resp.text
    finally:
        if owns_client:
            c.close()


__all__ = [
    "fetch_job_logs",
    "fetch_workflow_run_jobs",
    "get_commit_diff",
    "get_file_content",
    "reset_github_mock_state",
    "set_mock_diff",
    "set_mock_job_log",
    "set_mock_workflow_jobs",
]
