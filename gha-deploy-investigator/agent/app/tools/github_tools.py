import logging
from typing import Any, Optional

import httpx

from ..config import AgentConfig, get_settings
from .registry import readonly_tool

logger = logging.getLogger(__name__)


def _build_client(
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    cfg = config or get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
    }
    if cfg.github_token:
        headers["Authorization"] = f"Bearer {cfg.github_token}"
    return httpx.Client(headers=headers, timeout=30.0), True


@readonly_tool(
    name="fetch_workflow_run_jobs",
    description="Fetch list of jobs and step statuses for a GitHub Actions workflow run",
)
def fetch_workflow_run_jobs(
    repo: str,
    run_id: int,
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Fetch jobs for a GitHub Actions workflow run from GitHub REST API."""
    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/actions/runs/{run_id}/jobs"

    c, owns_client = _build_client(client, config)
    try:
        resp = c.get(endpoint)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    finally:
        if owns_client:
            c.close()


@readonly_tool(
    name="fetch_job_logs",
    description="Fetch execution runner logs for a specific GitHub Actions job",
)
def fetch_job_logs(
    repo: str,
    job_id: int,
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> str:
    """Fetch raw execution logs for a GitHub Actions job."""
    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/actions/jobs/{job_id}/logs"

    c, owns_client = _build_client(client, config)
    try:
        resp = c.get(endpoint)
        resp.raise_for_status()
        return resp.text
    finally:
        if owns_client:
            c.close()


@readonly_tool(
    name="get_commit_diff",
    description="Fetch commit details, modified files, and diff for a given commit SHA",
)
def get_commit_diff(
    repo: str,
    head_sha: str,
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> dict[str, Any]:
    """Fetch commit diff details from GitHub REST API."""
    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/commits/{head_sha}"

    c, owns_client = _build_client(client, config)
    try:
        resp = c.get(endpoint)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
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
    client: Optional[httpx.Client] = None,
    config: Optional[AgentConfig] = None,
) -> str:
    """Fetch file content from GitHub repository."""
    import base64

    cfg = config or get_settings()
    api_url = cfg.github_api_url.rstrip("/")
    endpoint = f"{api_url}/repos/{repo}/contents/{path}"

    c, owns_client = _build_client(client, config)
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
]
