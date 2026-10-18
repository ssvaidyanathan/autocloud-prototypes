import hashlib
import hmac
import logging
import re
from typing import Any, Literal, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from ..github.auth import GitHubAppAuth
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
        self._auth: Optional[GitHubAppAuth] = None
        if self.config.github_app_id and self.config.github_app_private_key:
            self._auth = GitHubAppAuth(
                app_id=self.config.github_app_id,
                private_key_pem=self.config.github_app_private_key,
                config=self.config,
            )

    def _get_writeback_token(self, repo_str: str) -> Optional[str]:
        if self.config.github_token:
            return self.config.github_token
        if self._auth:
            try:
                with self._get_client() as client:
                    return self._auth.get_installation_token_for_repo(
                        repo_str, client=client, base_url=self.config.github_api_url
                    )
            except Exception as exc:
                logger.error("Failed to mint installation token for repo %s: %s", repo_str, exc)
                raise
        return None

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
        raw_actor = run.get("actor")
        actor = raw_actor.get("login", "unknown") if isinstance(raw_actor, dict) else "unknown"
        head_commit = run.get("head_commit") or payload.get("head_commit") or {}
        if not isinstance(head_commit, dict):
            head_commit = {}
        author_info = head_commit.get("author") or {}
        if not isinstance(author_info, dict):
            author_info = {}
        author = author_info.get("name") or author_info.get("username") or actor

        opened_at = parse_utc_datetime(run.get("updated_at") or run.get("run_started_at"))
        run_attempt = run.get("run_attempt", 1)

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
            github_run_attempt=run_attempt,
            github_actor=actor,
            github_commit_author=author,
        )

    def post_report_link(self, incident: Incident, report: Report, ref: IncidentRef) -> None:
        """Post a Check Run on commit SHA linking to the ServiceNow incident."""
        repo_str = incident.github_repo or incident.service_hint or ""
        if not incident.github_head_sha or not repo_str or "/" not in repo_str:
            logger.warning(
                "Missing or invalid repo/head_sha on incident %s "
                "(repo=%s, sha=%s); skipping Check Run",
                incident.id,
                repo_str,
                incident.github_head_sha,
            )
            return

        owner, repo_name = repo_str.split("/", 1)
        api_url = (self.config.github_api_url or "https://api.github.com").rstrip("/")
        check_run_endpoint = f"{api_url}/repos/{owner}/{repo_name}/check-runs"

        raw_cause = (
            report.probable_root_cause.text if report.probable_root_cause else report.summary
        )
        clean_cause = raw_cause.strip()
        clean_cause = re.sub(r"^```(?:json)?\s*", "", clean_cause, flags=re.IGNORECASE)
        clean_cause = re.sub(r"\s*```$", "", clean_cause).strip()
        if clean_cause.startswith("{") and '"root_cause"' in clean_cause:
            m_rc = re.search(r'"root_cause"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"?', clean_cause)
            if m_rc:
                clean_cause = m_rc.group(1).replace(r"\"", '"').strip()
        clean_cause = re.sub(r"^\{\s*\"root_cause\"\s*:\s*\"?", "", clean_cause)
        clean_cause = re.sub(
            r"\"?\s*,?\s*\"remediation_actions\".*$", "", clean_cause, flags=re.DOTALL
        )
        clean_cause = re.sub(r"\"?\s*\}\s*$", "", clean_cause).strip(' \t\n\r"{}')
        root_cause_text = clean_cause or raw_cause

        confidence_val = (
            report.probable_root_cause.confidence if report.probable_root_cause else 0.90
        )
        confidence_str = f"{int(round(confidence_val * 100))}%"

        sn_link = f"[{ref.external_key}]({ref.external_url or '#'})"
        summary = (
            f"Autocloud investigated pipeline failure.\n\n"
            f"**ServiceNow Incident:** {sn_link}\n\n"
            f"**Confidence:** {confidence_str}\n\n"
            f"**Root Cause:** {root_cause_text}"
        )

        output_data: dict[str, Any] = {
            "title": f"ServiceNow Incident {ref.external_key}",
            "summary": summary,
            "text": report.summary,
        }
        if report.annotations:
            output_data["annotations"] = [
                {
                    "path": re.sub(r"^\.?/", "", a.path),
                    "start_line": a.start_line,
                    "end_line": a.end_line,
                    "annotation_level": a.annotation_level,
                    "message": a.message,
                    "title": a.title or "Diagnostic Finding",
                }
                for a in report.annotations[:50]
            ]

        check_run_payload = {
            "name": "Autocloud / ServiceNow Incident Link",
            "head_sha": incident.github_head_sha,
            "status": "completed",
            "conclusion": "neutral",
            "output": output_data,
        }

        token = self._get_writeback_token(repo_str)
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            with self._get_client() as client:
                resp = client.post(check_run_endpoint, json=check_run_payload, headers=headers)
                resp.raise_for_status()
                logger.info("Successfully posted Check Run for commit %s", incident.github_head_sha)

                # 2. Post PR or Commit Comment with collapsible <details> sections

                remediation_items = (
                    report.causal_remediation
                    if report.causal_remediation
                    else report.recommended_actions
                )
                actions_md = (
                    "\n".join(f"- {act}" for act in remediation_items) or "- Review logs"
                )
                evidence_lines = []
                for area, items in report.evidence_by_area.items():
                    for item in items:
                        kind_label = getattr(item, "kind", area) or area
                        excerpt = getattr(item, "excerpt", "") or str(item)
                        evidence_lines.append(f"**[{kind_label}]**:\n```\n{excerpt[:400]}\n```")
                evidence_md = "\n\n".join(evidence_lines) or report.summary

                failing_stg = (
                    incident.github_failing_stage
                    or report.metadata.get("failing_stage")
                    or "Pipeline"
                )
                sn_target = ref.external_url or "#"
                sn_md = f"[{ref.external_key}]({sn_target})"
                comment_lines = [
                    "### 🚨 Autocloud Deployment Failure Diagnostic Report\n",
                    f"**Stage:** `{failing_stg}` | **Severity:** `{incident.severity}` | "
                    f"**Confidence:** `{confidence_str}` | **ServiceNow Incident:** {sn_md}\n",
                    "<details open>",
                    "<summary><strong>🔍 Probable Root Cause</strong></summary>\n",
                    f"> {root_cause_text}",
                    "</details>\n",
                    "<details>",
                    "<summary><strong>📋 Diagnostic Summary & Evidence</strong></summary>\n",
                    evidence_md,
                    "</details>\n",
                    "<details>",
                    "<summary><strong>🛠️ Recommended Remediation</strong></summary>\n",
                    actions_md,
                    "</details>\n",
                ]
                if report.unrelated_observations:
                    unrelated_md = "\n".join(f"- {obs}" for obs in report.unrelated_observations)
                    comment_lines.extend(
                        [
                            "<details>",
                            "<summary><strong>⚠️ Unrelated Observations</strong></summary>\n",
                            unrelated_md,
                            "</details>\n",
                        ]
                    )
                comment_lines.extend(
                    [
                        "---",
                        f"*Automated diagnostic report generated by Autocloud Deploy Investigator "
                        f"linking to ServiceNow Incident {sn_md}.*",
                    ]
                )
                comment_body = "\n".join(comment_lines)

                if incident.github_pr_number:
                    comment_url = (
                        f"{api_url}/repos/{owner}/{repo_name}/issues/"
                        f"{incident.github_pr_number}/comments"
                    )
                else:
                    comment_url = (
                        f"{api_url}/repos/{owner}/{repo_name}/commits/"
                        f"{incident.github_head_sha}/comments"
                    )

                c_resp = client.post(comment_url, json={"body": comment_body}, headers=headers)
                c_resp.raise_for_status()
                logger.info(
                    "Successfully posted diagnostic comment for %s", incident.github_head_sha
                )
        except httpx.HTTPStatusError as http_err:
            error_body = http_err.response.text
            logger.error(
                "GitHub write-back failed for %s (status %d): %s (response: %s)",
                repo_str,
                http_err.response.status_code,
                http_err,
                error_body,
            )
            raise
        except Exception as exc:
            logger.error("GitHub write-back failed for %s: %s", repo_str, exc)
            raise

    def post_ack(self, ref: IncidentRef) -> None:
        """No-op for GitHub inbound webhook."""
        pass

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """No-op; reverse flow uses post_report_link."""
        pass

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Log failure for GitHub incident."""
        logger.warning("Investigation failed for GitHub incident %s: %s", ref.external_key, error)
