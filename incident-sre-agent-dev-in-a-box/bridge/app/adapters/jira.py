import hmac
import logging
import re
from typing import Any, Literal, Optional

import httpx
from fastapi import Request

from ..config import BridgeConfig, get_settings
from ..contracts.incident import Incident
from ..contracts.report import Report
from ..templates.jira_adf import (
    chunk_adf,
    markdown_to_adf,
    render_jira_ack_adf,
    render_jira_failure_adf,
    report_to_adf,
    update_adf_root_causes,
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

        # UTC-aware opened_at with alert_started_at propagation
        alert_started_at = None
        try:
            prop = self.get_issue_property(issue_key, "hitl_metadata")
            if isinstance(prop, dict) and prop.get("alert_started_at"):
                alert_started_at = prop["alert_started_at"]
        except Exception as exc:
            logger.debug("Failed to read hitl_metadata property from %s: %s", issue_key, exc)

        if not alert_started_at and description:
            alert_ts_pattern = (
                r"(?:Started At|Alert Started At|openTime):\s*"
                r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z?)"
            )
            match = re.search(alert_ts_pattern, description)
            if match:
                alert_started_at = match.group(1)

        if alert_started_at:
            try:
                opened_at = parse_utc_datetime(alert_started_at)
            except Exception as exc:
                logger.warning("Failed to parse alert_started_at '%s': %s", alert_started_at, exc)
                created_raw = fields.get("created")
                opened_at = parse_utc_datetime(created_raw)
        else:
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

    def search_issues(
        self,
        jql: str,
        fields: Optional[list[str]] = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Search Jira issues using JQL."""
        base_url = self.config.jira_base_url
        if not base_url:
            logger.warning("JIRA_BASE_URL not set; returning empty search results.")
            return []

        search_fields = fields or ["key", "summary", "labels", "status"]
        payload = {
            "jql": jql,
            "fields": search_fields,
            "maxResults": max_results,
        }

        with self._get_client() as client:
            # 1. Try modern /rest/api/3/search/jql endpoint
            url = f"{base_url.rstrip('/')}/rest/api/3/search/jql"
            try:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    issues = data.get("issues", [])
                    return issues if isinstance(issues, list) else []
            except Exception as exc:
                logger.debug("POST /search/jql failed, attempting fallbacks: %s", exc)

            # 2. Try GET /rest/api/3/search/jql
            try:
                resp = client.get(
                    url,
                    params={
                        "jql": jql,
                        "fields": ",".join(search_fields),
                        "maxResults": max_results,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    issues = data.get("issues", [])
                    return issues if isinstance(issues, list) else []
            except Exception as exc:
                logger.debug("GET /search/jql fallback failed: %s", exc)

            # 3. Fallback to legacy POST /rest/api/3/search
            legacy_url = f"{base_url.rstrip('/')}/rest/api/3/search"
            try:
                resp = client.post(legacy_url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    issues = data.get("issues", [])
                    return issues if isinstance(issues, list) else []
                resp.raise_for_status()
            except Exception as exc:
                logger.error("Jira JQL search failed for '%s': %s", jql, exc)
                raise

        return []

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str | dict[str, Any],
        issue_type: str = "Task",
        priority: str = "P2",
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a new issue in Jira Cloud REST API v3."""
        base_url = self.config.jira_base_url
        if not base_url:
            raise ValueError("JIRA_BASE_URL is required to create a Jira issue")

        priority_map = {
            "P1": "Highest",
            "P2": "High",
            "P3": "Medium",
            "P4": "Low",
            "HIGHEST": "Highest",
            "HIGH": "High",
            "MEDIUM": "Medium",
            "LOW": "Low",
            "LOWEST": "Lowest",
        }
        mapped_priority = priority_map.get(priority.upper(), priority)

        # Convert description to ADF doc if passed as string
        desc_adf: dict[str, Any]
        if isinstance(description, dict) and description.get("type") == "doc":
            desc_adf = description
        elif isinstance(description, str):
            desc_adf = markdown_to_adf(description)
        else:
            desc_adf = markdown_to_adf(str(description) if description is not None else "")

        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "priority": {"name": mapped_priority},
            "labels": labels or ["hitl"],
            "description": desc_adf,
        }

        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue"
            try:
                resp = client.post(url, json={"fields": fields})
                resp.raise_for_status()
                res_data: dict[str, Any] = resp.json()
                logger.info(
                    "Successfully created Jira issue %s in project %s",
                    res_data.get("key"),
                    project_key,
                )
                return res_data
            except Exception as exc:
                logger.error("Failed to create Jira issue: %s", exc)
                raise

    def get_issue(self, issue_key: str, fields: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Retrieve Jira issue details (summary, status, labels, description)."""
        base_url = self.config.jira_base_url
        if not base_url:
            return None

        clean_key = issue_key[5:] if issue_key.lower().startswith("jira-") else issue_key
        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue/{clean_key}"
            try:
                field_list = fields or "summary,status,labels,description"
                resp = client.get(url, params={"fields": field_list})
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                res_issue: dict[str, Any] = resp.json()
                return res_issue
            except Exception as exc:
                logger.debug("Failed to get Jira issue %s: %s", clean_key, exc)
                return None

    def update_issue(self, issue_key: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update fields on an existing Jira issue."""
        base_url = self.config.jira_base_url
        if not base_url:
            raise ValueError("JIRA_BASE_URL is required to update a Jira issue")

        clean_key = issue_key[5:] if issue_key.lower().startswith("jira-") else issue_key
        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue/{clean_key}"
            try:
                resp = client.put(url, json={"fields": fields})
                resp.raise_for_status()
                if resp.status_code == 204 or not resp.text.strip():
                    return {"status": "updated", "key": clean_key}
                res_dict: dict[str, Any] = resp.json()
                return res_dict
            except Exception as exc:
                logger.error("Failed to update Jira issue %s: %s", clean_key, exc)
                raise

    def update_issue_root_causes(self, issue_key: str, root_causes_text: str) -> bool:
        """Find Root Causes section in issue description and update it with RCA findings."""
        base_url = self.config.jira_base_url
        if not base_url:
            return False

        issue = self.get_issue(issue_key, fields="description")
        if not issue:
            logger.warning("Cannot update Root Causes: issue %s not found", issue_key)
            return False

        desc_data = issue.get("fields", {}).get("description")
        if isinstance(desc_data, dict) and desc_data.get("type") == "doc":
            updated = update_adf_root_causes(desc_data, root_causes_text)
            if updated:
                self.update_issue(issue_key, {"description": desc_data})
                logger.info("Updated Root Causes section in Jira issue %s description", issue_key)
                return True
        elif isinstance(desc_data, str) and desc_data.strip():
            doc = markdown_to_adf(desc_data)
            update_adf_root_causes(doc, root_causes_text)
            self.update_issue(issue_key, {"description": doc})
            logger.info("Updated Root Causes section in Jira issue %s description", issue_key)
            return True
        else:
            doc = markdown_to_adf(f"**Root Causes:**\n- {root_causes_text}")
            self.update_issue(issue_key, {"description": doc})
            logger.info("Created Root Causes section in Jira issue %s description", issue_key)
            return True

        return False

    def set_issue_property(self, issue_key: str, property_key: str, value: Any) -> bool:
        """Set custom property JSON on a Jira issue."""
        base_url = self.config.jira_base_url
        if not base_url:
            return False

        clean_key = issue_key[5:] if issue_key.lower().startswith("jira-") else issue_key
        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue/{clean_key}/properties/{property_key}"
            try:
                resp = client.put(url, json=value)
                return resp.status_code in (200, 201)
            except Exception as exc:
                logger.debug(
                    "Failed to set Jira property %s on %s: %s", property_key, clean_key, exc
                )
                return False

    def get_issue_property(self, issue_key: str, property_key: str) -> Optional[Any]:
        """Get custom property JSON from a Jira issue."""
        base_url = self.config.jira_base_url
        if not base_url:
            return None

        clean_key = issue_key[5:] if issue_key.lower().startswith("jira-") else issue_key
        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue/{clean_key}/properties/{property_key}"
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("value")
            except Exception as exc:
                logger.debug(
                    "Failed to get Jira property %s on %s: %s", property_key, clean_key, exc
                )
            return None

    def add_comment(
        self,
        issue_key: str,
        body: str | dict[str, Any],
        properties: Optional[dict[str, Any] | list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Add a comment to an existing Jira issue using REST API v3.

        Args:
            issue_key: Key of the Jira issue (e.g. 'AMJTEST-20' or 'jira-AMJTEST-20').
            body: Comment content, either plain/markdown text or an ADF document dictionary.
            properties: Optional comment properties to associate with the comment.

        Returns:
            Dictionary returned by Jira Cloud REST API v3 containing comment metadata.

        Raises:
            ValueError: If JIRA_BASE_URL is not set or issue_key is empty.
            httpx.HTTPStatusError: On upstream HTTP error (e.g. 404, 400, 5xx).
        """
        base_url = self.config.jira_base_url
        if not base_url:
            raise ValueError("JIRA_BASE_URL is required to add a Jira comment")

        clean_key = issue_key[5:] if issue_key.lower().startswith("jira-") else issue_key
        if not clean_key:
            raise ValueError("issue_key must not be empty")

        if isinstance(body, dict) and body.get("type") == "doc":
            comment_adf = body
        elif isinstance(body, str):
            comment_adf = markdown_to_adf(body)
        else:
            comment_adf = markdown_to_adf(str(body))

        payload: dict[str, Any] = {"body": comment_adf}
        if properties:
            if isinstance(properties, list):
                payload["properties"] = properties
            elif isinstance(properties, dict):
                payload["properties"] = [{"key": k, "value": v} for k, v in properties.items()]

        with self._get_client() as client:
            url = f"{base_url.rstrip('/')}/rest/api/3/issue/{clean_key}/comment"
            try:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                res_data: dict[str, Any] = (
                    resp.json() if resp.text.strip() else {"status": "created", "key": clean_key}
                )
                logger.info(
                    "Successfully added comment to Jira issue %s (comment id: %s)",
                    clean_key,
                    res_data.get("id"),
                )
                return res_data
            except Exception as exc:
                logger.error("Failed to add comment to Jira issue %s: %s", clean_key, exc)
                raise

    # Alias for normalization parity
    from_issue = to_incident
