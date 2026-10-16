import hashlib
import hmac
import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from .base import BaseAdapter, IncidentRef, parse_utc_datetime

logger = logging.getLogger(__name__)


class _ReusableClientWrapper:
    """Wrapper that prevents closing an externally supplied client in a context manager."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class GitHubAdapter(BaseAdapter):
    """Adapter for GitHub Actions webhooks and Check Run write-back."""

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.config = config or get_settings()
        self._http_client = http_client

    def _get_client(self) -> Any:
        if self._http_client is not None:
            return _ReusableClientWrapper(self._http_client)
        headers = {
            "Accept": "application/vnd.github+json",
        }
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        timeout = getattr(self.config, "adapter_timeout_seconds", 30.0)
        return httpx.Client(headers=headers, timeout=timeout)

    def verify(self, request: Request, payload: bytes) -> bool:
        """Verify inbound webhook HMAC SHA-256 signature."""
        secret = self.config.github_webhook_secret
        if not secret:
            if self.config.is_local:
                logger.warning(
                    "No GITHUB_WEBHOOK_SECRET configured in local environment; accepting."
                )
                return True
            logger.warning("No GITHUB_WEBHOOK_SECRET configured; rejecting request.")
            return False

        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature or not signature.startswith("sha256="):
            logger.warning("Missing or malformed X-Hub-Signature-256 header")
            return False

        computed = (
            "sha256="
            + hmac.new(
                secret.encode("utf-8"),
                payload,
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(signature, computed)

    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Parse workflow_run event payload into canonical Incident contract."""
        run = payload.get("workflow_run", {})
        repo = payload.get("repository", {})
        action = payload.get("action", "")
        conclusion = run.get("conclusion", "")

        if action != "completed" or conclusion not in ("failure", "timed_out"):
            raise ValueError(
                f"Ignoring non-failure event: action={action}, conclusion={conclusion}"
            )

        run_id = run.get("id")
        if not run_id:
            raise ValueError("Missing workflow_run.id in webhook payload")

        repo_full = repo.get("full_name") or repo.get("name", "unknown")
        repo_name = repo.get("name", "unknown")
        workflow_name = run.get("name", "CI/CD Pipeline")
        branch = run.get("head_branch", "main")
        head_sha = run.get("head_sha", "")
        prs = run.get("pull_requests", [])
        pr_number = prs[0].get("number") if prs and isinstance(prs[0], dict) else None
        actor = run.get("actor", {}).get("login", "github-actions")
        opened_at = parse_utc_datetime(run.get("updated_at") or run.get("run_started_at"))

        severity: Literal["P1", "P2", "P3", "P4"] = (
            "P2" if branch in ("main", "master", "release") else "P3"
        )

        return Incident(
            id=f"gha-{run_id}",
            source="github",
            external_id=str(run_id),
            external_key=f"{repo_full}#{run_id}",
            external_url=run.get("html_url"),
            title=f"[CI/CD Failure] {repo_name}: {workflow_name}",
            description=(
                f"Workflow '{workflow_name}' failed on branch '{branch}' "
                f"(commit {head_sha[:8] if head_sha else 'unknown'}) triggered by @{actor}"
            ),
            severity=severity,
            service_hint=repo_name,
            labels=["ci-cd", "github-actions", conclusion],
            opened_at=opened_at,
            reporter=actor,
            github_repo=repo_full,
            github_run_id=run_id,
            github_head_sha=head_sha,
            github_branch=branch,
            github_pr_number=pr_number,
            github_failing_stage=None,
        )

    def post_report_link(self, incident: Incident, report: Report, ref: IncidentRef) -> None:
        """Post a Check Run on commit SHA linking to the ServiceNow incident."""
        if not incident.github_head_sha:
            logger.warning("No github_head_sha on incident %s; skipping check run", incident.id)
            return

        repo_str = incident.github_repo or incident.service_hint or ""
        if "/" in repo_str:
            owner, repo_name = repo_str.split("/", 1)
        else:
            owner, repo_name = "unknown", repo_str

        api_url = (self.config.github_api_url or "https://api.github.com").rstrip("/")
        endpoint = f"{api_url}/repos/{owner}/{repo_name}/check-runs"

        root_cause_text = (
            report.probable_root_cause.text if report.probable_root_cause else report.summary
        )
        sn_link = f"[{ref.external_key}]({ref.external_url or '#'})"
        summary = (
            f"Autocloud investigated pipeline failure.\n\n"
            f"**ServiceNow Incident:** {sn_link}\n\n"
            f"**Root Cause:** {root_cause_text}"
        )

        check_run_payload = {
            "name": "Autocloud / ServiceNow Incident Link",
            "head_sha": incident.github_head_sha,
            "status": "completed",
            "conclusion": "neutral",
            "output": {
                "title": f"ServiceNow Incident {ref.external_key}",
                "summary": summary,
                "text": report.summary,
            },
        }

        headers = {"Accept": "application/vnd.github+json"}
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"

        with self._get_client() as client:
            resp = client.post(endpoint, json=check_run_payload, headers=headers)
            resp.raise_for_status()
            logger.info("Successfully posted Check Run for commit %s", incident.github_head_sha)

    def post_ack(self, ref: IncidentRef) -> None:
        """No-op for GitHub inbound webhook."""
        pass

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """No-op; reverse flow uses post_report_link."""
        pass

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Log failure for GitHub incident."""
        logger.warning("Investigation failed for GitHub incident %s: %s", ref.external_key, error)
