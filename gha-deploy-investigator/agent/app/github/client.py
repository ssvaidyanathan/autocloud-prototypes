import base64
import logging
from typing import Any, Optional

import httpx
from agent.app.config import AgentConfig, get_settings
from agent.app.github.auth import GitHubAppAuth

logger = logging.getLogger(__name__)


class GitHubRestClient:
    """Read-only GitHub REST client using installation token or static token."""

    def __init__(
        self,
        token: Optional[str] = None,
        auth: Optional[GitHubAppAuth] = None,
        installation_id: Optional[int] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.config = config or get_settings()
        self.base_url = (base_url or self.config.github_api_url or "https://api.github.com").rstrip(
            "/"
        )
        self.token = token or self.config.github_token
        self.auth = auth
        self.installation_id = installation_id
        self.client = client

    def _get_token(self) -> str:
        if self.auth and self.installation_id:
            return self.auth.get_installation_token(
                self.installation_id, client=self.client, base_url=self.base_url
            )
        return self.token or "ghp_test_token"

    def _headers(self) -> dict[str, str]:
        token = self._get_token()
        headers = {
            "Accept": "application/vnd.github+json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_workflow_run_jobs(self, repo: str, run_id: int) -> dict[str, Any]:
        """Fetch workflow run jobs list."""
        url = f"{self.base_url}/repos/{repo}/actions/runs/{run_id}/jobs"
        c = self.client or httpx.Client(timeout=15.0)
        owns_client = self.client is None
        try:
            resp = c.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns_client:
                c.close()

    def get_job_logs(self, repo: str, job_id: int) -> str:
        """Fetch raw job logs."""
        url = f"{self.base_url}/repos/{repo}/actions/jobs/{job_id}/logs"
        c = self.client or httpx.Client(timeout=20.0)
        owns_client = self.client is None
        try:
            resp = c.get(url, headers=self._headers(), follow_redirects=True)
            resp.raise_for_status()
            return resp.text
        finally:
            if owns_client:
                c.close()

    def get_commit_diff(self, repo: str, head_sha: str) -> dict[str, Any]:
        """Fetch commit details and diff."""
        url = f"{self.base_url}/repos/{repo}/commits/{head_sha}"
        c = self.client or httpx.Client(timeout=15.0)
        owns_client = self.client is None
        try:
            resp = c.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns_client:
                c.close()

    def get_file_content(self, repo: str, path: str, ref: str = "main") -> str:
        """Fetch raw file content at specific ref."""
        url = f"{self.base_url}/repos/{repo}/contents/{path}"
        params = {"ref": ref}
        c = self.client or httpx.Client(timeout=15.0)
        owns_client = self.client is None
        try:
            resp = c.get(url, headers=self._headers(), params=params)
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
