import hmac
import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from ..templates.jira_adf import (
    chunk_adf,
    render_jira_ack_adf,
    render_jira_failure_adf,
    report_to_adf,
)
from ..templates.renderer import render_standalone_html
from .base import BaseAdapter, IncidentRef, parse_utc_datetime

logger = logging.getLogger(__name__)


class _ReusableClientWrapper:
    """Wrapper that prevents closing an externally supplied client in a with statement."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class JiraAdapter(BaseAdapter):
    """Adapter for Jira Cloud REST API v3 issues, comments, transitions, attachments."""

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
        auth = None
        if self.config.jira_email and self.config.jira_api_token:
            auth = httpx.BasicAuth(self.config.jira_email, self.config.jira_api_token)
        timeout = getattr(self.config, "adapter_timeout_seconds", 30.0)
        return httpx.Client(auth=auth, timeout=timeout)

    def verify(self, request: Request, payload: bytes) -> bool:
        """Verify inbound webhook request token header using constant-time comparison."""
        expected_token = self.config.jira_webhook_token
        if not expected_token:
            logger.warning("No JIRA_WEBHOOK_TOKEN configured; accepting request.")
            return True

        # Check various common webhook token headers from Jira Automation
        candidate = (
            request.headers.get("X-Webhook-Token")
            or request.headers.get("X-Atlassian-Webhook-Token")
            or request.headers.get("X-Atlassian-Token")
        )
        if not candidate:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                candidate = auth[7:].strip()

        if not candidate:
            logger.warning("Jira webhook missing token header")
            return False

        return hmac.compare_digest(candidate, expected_token)

    def to_incident(self, payload: dict[str, Any]) -> Incident:
        """Normalize Jira Issue payload into Incident contract with UTC timestamps."""
        issue = payload.get("issue")
        if not isinstance(issue, dict):
            raise ValueError(
                "Missing required Jira incident identifier: "
                "'issue.id' or 'issue.key' must be non-empty"
            )

        raw_id = issue.get("id")
        raw_key = issue.get("key")
        clean_id = str(raw_id).strip() if raw_id is not None else ""
        clean_key = str(raw_key).strip() if raw_key is not None else ""

        if not clean_id and not clean_key:
            raise ValueError(
                "Missing required Jira incident identifier: "
                "'issue.id' or 'issue.key' must be non-empty"
            )

        issue_id = clean_id
        issue_key = clean_key or clean_id

        fields = issue.get("fields", {})
        summary = str(fields.get("summary") or "Jira Incident")

        # Description handling: could be string or ADF structure
        raw_desc = fields.get("description")
        description = summary
        if isinstance(raw_desc, str) and raw_desc.strip():
            description = raw_desc.strip()
        elif isinstance(raw_desc, dict):
            # Extract plain text from simple ADF doc if available
            extracted: list[str] = []
            for item in raw_desc.get("content", []):
                for node in item.get("content", []):
                    if node.get("type") == "text" and node.get("text"):
                        extracted.append(node["text"])
            if extracted:
                description = " ".join(extracted)

        # Map priority to P1-P4
        priority_obj = fields.get("priority")
        priority_name = ""
        if isinstance(priority_obj, dict):
            priority_name = str(priority_obj.get("name", "")).lower()
        elif priority_obj:
            priority_name = str(priority_obj).lower()

        severity: Literal["P1", "P2", "P3", "P4"] = "P3"
        if any(p in priority_name for p in ["highest", "blocker", "p1"]):
            severity = "P1"
        elif any(p in priority_name for p in ["high", "critical", "p2"]):
            severity = "P2"
        elif any(p in priority_name for p in ["medium", "major", "p3"]):
            severity = "P3"
        elif any(p in priority_name for p in ["low", "lowest", "minor", "p4"]):
            severity = "P4"

        # Service hint from components
        service_hint = None
        components = fields.get("components", [])
        if components and isinstance(components, list):
            comp = components[0]
            if isinstance(comp, dict):
                service_hint = comp.get("name")
            else:
                service_hint = str(comp)

        # Labels
        labels = fields.get("labels", [])
        if not isinstance(labels, list):
            labels = []

        # UTC-aware opened_at
        created_raw = fields.get("created")
        opened_at = parse_utc_datetime(created_raw)

        # Reporter
        reporter_obj = fields.get("reporter")
        reporter_name = None
        if isinstance(reporter_obj, dict):
            reporter_name = reporter_obj.get("displayName") or reporter_obj.get("name")
        elif reporter_obj:
            reporter_name = str(reporter_obj)

        base_url = self.config.jira_base_url or ""
        external_url = f"{base_url.rstrip('/')}/browse/{issue_key}" if base_url else None

        return Incident(
            id=f"jira-{issue_key}",
            source="jira",
            external_id=issue_id or issue_key,
            external_key=issue_key,
            external_url=external_url,
            title=summary,
            description=description,
            severity=severity,
            service_hint=service_hint,
            labels=labels,
            opened_at=opened_at,
            reporter=reporter_name,
        )

    def post_ack(self, ref: IncidentRef) -> None:
        """Acknowledge investigation with a comment and execute transition to In Progress."""
        base_url = self.config.jira_base_url
        if not base_url:
            logger.warning("JIRA_BASE_URL not set; skipping Jira ack post.")
            return

        with self._get_client() as client:
            # 1. Post acknowledgement comment
            comment_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{ref.external_key}/comment"
            ack_body = {
                "body": render_jira_ack_adf(),
            }

            try:
                resp = client.post(comment_url, json=ack_body)
                resp.raise_for_status()
                logger.info("Successfully posted ack comment to Jira %s", ref.external_key)
            except Exception as exc:
                logger.error("Failed to post ack comment to Jira %s: %s", ref.external_key, exc)
                raise

            # 2. Look up and trigger 'In Progress' transition
            target_transition_name = self.config.jira_in_progress_transition.lower()
            transitions_url = (
                f"{base_url.rstrip('/')}/rest/api/3/issue/{ref.external_key}/transitions"
            )
            try:
                t_resp = client.get(transitions_url)
                t_resp.raise_for_status()
                transitions_data = t_resp.json().get("transitions", [])
                matched_id: Optional[str] = None
                for trans in transitions_data:
                    name = str(trans.get("name", "")).lower()
                    if name == target_transition_name:
                        matched_id = str(trans.get("id"))
                        break

                if matched_id:
                    post_t_resp = client.post(
                        transitions_url,
                        json={"transition": {"id": matched_id}},
                    )
                    post_t_resp.raise_for_status()
                    logger.info(
                        "Successfully transitioned Jira %s to '%s' (ID %s)",
                        ref.external_key,
                        self.config.jira_in_progress_transition,
                        matched_id,
                    )
                else:
                    logger.warning(
                        "Transition '%s' not found for Jira issue %s among %s",
                        self.config.jira_in_progress_transition,
                        ref.external_key,
                        [t.get("name") for t in transitions_data],
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to transition Jira issue %s state: %s",
                    ref.external_key,
                    exc,
                )

    def post_report(self, ref: IncidentRef, report: Report) -> None:
        """Post ADF structured comment (chunked if > 32k) and attach standalone HTML report."""
        base_url = self.config.jira_base_url
        if not base_url:
            logger.warning("JIRA_BASE_URL not set; skipping Jira report post.")
            return

        with self._get_client() as client:
            # 1. Render ADF comment and chunk if needed
            adf_doc = report_to_adf(report, incident_key=ref.external_key)
            chunk_size = getattr(self.config, "jira_chunk_size", 32000)
            chunks = chunk_adf(adf_doc, max_chars=chunk_size)

            comment_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{ref.external_key}/comment"
            for chunk in chunks:
                try:
                    resp = client.post(comment_url, json={"body": chunk})
                    resp.raise_for_status()
                except Exception as exc:
                    logger.error(
                        "Failed to post Jira ADF comment chunk for %s: %s", ref.external_key, exc
                    )
                    raise

            # 2. Attach standalone HTML report via Jira Attachment API
            standalone_html = render_standalone_html(report, incident_ref=ref)
            attach_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{ref.external_key}/attachments"
            headers = {"X-Atlassian-Token": "no-check"}
            files = {
                "file": (
                    f"sre_investigation_{ref.external_key}.html",
                    standalone_html.encode("utf-8"),
                    "text/html",
                )
            }

            try:
                resp = client.post(attach_url, headers=headers, files=files)
                resp.raise_for_status()
                logger.info("Successfully attached HTML report to Jira %s", ref.external_key)
            except Exception as exc:
                logger.warning("Failed to attach HTML report to Jira %s: %s", ref.external_key, exc)

    def post_failure(self, ref: IncidentRef, error: str) -> None:
        """Post failure comment to Jira issue."""
        base_url = self.config.jira_base_url
        if not base_url:
            logger.warning("JIRA_BASE_URL not set; skipping Jira failure post.")
            return

        with self._get_client() as client:
            comment_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{ref.external_key}/comment"
            body = {
                "body": render_jira_failure_adf(error),
            }

            try:
                resp = client.post(comment_url, json=body)
                resp.raise_for_status()
                logger.info("Successfully posted failure comment to Jira %s", ref.external_key)
            except Exception as exc:
                logger.error(
                    "Failed to post Jira failure comment for %s: %s", ref.external_key, exc
                )
                raise
