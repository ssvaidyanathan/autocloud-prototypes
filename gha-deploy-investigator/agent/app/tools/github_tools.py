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


__all__ = [
    "fetch_job_logs",
    "fetch_workflow_run_jobs",
]
