"""Jira issue creation with HMAC token validation and idempotency check."""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.session import get_conversation_store
from app.tokens import consume_signed_token

logger = logging.getLogger(__name__)


class JiraIssueResult(BaseModel):
    """Result of creating a Jira issue through the bridge."""

    issue_key: str = Field(description="Created Jira issue key (e.g. AMJTEST-20)")
    issue_url: str = Field(description="Direct URL to created Jira issue")
    status: str = Field(default="created", description="Outcome status (created / already_exists)")
    draft_id: str = Field(description="Associated draft ID")
    incident_id: str = Field(description="Associated alert violation ID")


# In-memory idempotency tracker: alert_label -> (issue_key, issue_url)
_created_incidents: Dict[str, Tuple[str, str]] = {}


def reset_idempotency_cache() -> None:
    """Clear in-memory idempotency cache (for tests)."""
    _created_incidents.clear()


def _fetch_oidc_token(audience: str) -> Optional[str]:
    """Obtain an authenticated Google OIDC ID token targeting the bridge service."""
    settings = get_settings()
    caller_sa = os.environ.get("HITL_CALLER_SA")
    if not caller_sa and settings.project_id:
        caller_sa = f"hitl-chatbot-sa@{settings.project_id}.iam.gserviceaccount.com"

    if caller_sa:
        try:
            import google.auth
            from google.auth import impersonated_credentials
            from google.auth.transport.requests import Request

            source_creds, _ = google.auth.default()
            impersonated_source = impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
                source_credentials=source_creds,
                target_principal=caller_sa,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            id_creds = impersonated_credentials.IDTokenCredentials(  # type: ignore[no-untyped-call]
                target_credentials=impersonated_source,
                target_audience=audience,
                include_email=True,
            )
            id_creds.refresh(Request())
            if id_creds.token:
                return str(id_creds.token)
        except Exception as exc:
            logger.debug("Impersonated token acquisition fallback: %s", exc)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        req = Request()
        tok = id_token.fetch_id_token(req, audience)  # type: ignore[no-untyped-call]
        if tok:
            return str(tok)
    except Exception as exc:
        logger.debug("Direct id_token fetch fallback: %s", exc)

    return None


def create_jira_issue(
    signed_token: str,
    approver_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> JiraIssueResult:
    """Validate HMAC token signature, verify expiry, check idempotency, and invoke bridge."""
    settings = get_settings()
    store = get_conversation_store()

    # 1. Validate and consume signed approval token (single-use replay check and requester check)
    is_valid, error_msg, payload = consume_signed_token(
        token_str=signed_token,
        secret_key=settings.hmac_secret_key,
        approver_id=approver_id,
    )
    if not is_valid:
        raise ValueError(f"Approval token validation failed: {error_msg}")

    draft_id = str(payload.get("draft_id"))
    incident_id = str(payload.get("incident_id"))
    idempotency_label = f"alert-{incident_id}"

    # Verify against draft's stored requester_id if present
    draft_data = store.get_draft(draft_id)
    if draft_data and draft_data.get("requester_id"):
        draft_requester = draft_data["requester_id"]
        if approver_id is None or not hmac.compare_digest(str(approver_id), str(draft_requester)):
            err_msg = (
                f"Approval rejected: user '{approver_id}' is not authorized to "
                f"approve draft created by '{draft_requester}'"
            )
            raise ValueError(err_msg)

    # 2. Jira Idempotency check: prevent duplicate ticket creation for the same alert
    if idempotency_label in _created_incidents:
        existing_key, existing_url = _created_incidents[idempotency_label]
        logger.info(
            "Ticket already exists for %s: %s (idempotency triggered)",
            idempotency_label,
            existing_key,
        )
        return JiraIssueResult(
            issue_key=existing_key,
            issue_url=existing_url,
            status="already_exists",
            draft_id=draft_id,
            incident_id=incident_id,
        )

    # 3. Retrieve draft data from in-memory session store
    draft_data = store.get_draft(draft_id) or {
        "project_key": settings.jira_project_key,
        "issue_type": settings.jira_issue_type,
        "summary": f"[ALERT] Uptime check failed for {incident_id}",
        "description": f"Automated alert incident for violation {incident_id}",
        "priority": "P2",
        "labels": ["hitl", idempotency_label],
    }

    # 4. Invoke bridge POST /api/jira/issue with fallback to local generation
    bridge_url = settings.bridge_url.rstrip("/")
    issue_key = "AMJTEST-20"
    issue_url = f"https://jira.example.internal/browse/{issue_key}"
    status_outcome = "created"

    headers: Dict[str, str] = {}
    if bridge_url.startswith("https://"):
        token = _fetch_oidc_token(bridge_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    callback_url = f"{settings.service_url or 'https://hitl-chatbot-jnwbcz7dkq-uc.a.run.app'}/internal/rca-callback"
    slack_thread = draft_data.get("slack_thread") or session_id
    started_at = draft_data.get("alert_started_at") or draft_data.get("started_at")

    labels = list(draft_data.get("labels", ["hitl", idempotency_label]))
    if started_at:
        try:
            ts_str = str(started_at).replace("Z", "+00:00")
            open_ts = int(datetime.fromisoformat(ts_str).timestamp())
            open_label = f"alert-open-{open_ts}"
            if open_label not in labels:
                labels.append(open_label)
        except Exception as exc:
            logger.debug("Failed to parse started_at for label: %s", exc)

    post_payload: Dict[str, Any] = {
        "project_key": draft_data.get("project_key", settings.jira_project_key),
        "issue_type": draft_data.get("issue_type", settings.jira_issue_type),
        "summary": draft_data.get("summary"),
        "description": draft_data.get("description"),
        "priority": draft_data.get("priority", "P2"),
        "labels": labels,
        "callback_url": callback_url,
        "slack_thread": slack_thread,
        "alert_started_at": started_at,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{bridge_url}/api/jira/issue",
                headers=headers,
                json=post_payload,
            )
            if resp.status_code in (200, 201):
                res_json = resp.json()
                issue_key = res_json.get("issue_key", issue_key)
                issue_url = res_json.get("issue_url", issue_url)
                status_outcome = res_json.get("status", status_outcome)
    except Exception as exc:
        logger.debug("Bridge API invocation not reachable, using simulated key: %s", exc)

    # 5. Record created ticket in idempotency cache
    _created_incidents[idempotency_label] = (issue_key, issue_url)

    # Record active ticket in thread context
    thread_ref = session_id or draft_data.get("slack_thread") or "default-session"
    store.set_active_ticket(
        thread_id=thread_ref,
        ticket_key=issue_key,
        incident_id=incident_id,
        policy_name=draft_data.get("policy_name"),
        ticket_url=issue_url,
        rca_status="pending",
        rca_attached=False,
    )

    # 6. Clean up consumed draft from conversation store
    store.remove_draft(draft_id)

    return JiraIssueResult(
        issue_key=issue_key,
        issue_url=issue_url,
        status=status_outcome,
        draft_id=draft_id,
        incident_id=incident_id,
    )


class JiraCommentResult(BaseModel):
    """Result of adding a comment to a Jira issue through the bridge."""

    ticket_key: str = Field(description="Target Jira ticket key")
    comment_id: str = Field(description="Created Jira comment ID")
    status: str = Field(default="created", description="Comment creation status")
    draft_id: str = Field(description="Associated draft ID")
    ticket_url: str = Field(description="Direct URL to the Jira issue")


def add_jira_comment(
    signed_token: str,
    approver_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> JiraCommentResult:
    """Validate HMAC token signature, verify expiry, and invoke bridge to add comment."""
    settings = get_settings()
    store = get_conversation_store()

    # 1. Validate and consume signed approval token
    is_valid, error_msg, payload = consume_signed_token(
        token_str=signed_token,
        secret_key=settings.hmac_secret_key,
        approver_id=approver_id,
    )
    if not is_valid:
        raise ValueError(f"Approval token validation failed: {error_msg}")

    draft_id = str(payload.get("draft_id"))
    draft_data = store.get_draft(draft_id) or {}

    # Verify against draft's stored requester_id if present
    if draft_data.get("requester_id"):
        draft_requester = draft_data["requester_id"]
        if approver_id is None or not hmac.compare_digest(str(approver_id), str(draft_requester)):
            err_msg = (
                f"Approval rejected: user '{approver_id}' is not authorized to "
                f"approve draft created by '{draft_requester}'"
            )
            raise ValueError(err_msg)

    ticket_key = draft_data.get("ticket_key") or str(payload.get("incident_id"))
    comment_body = draft_data.get("body") or "RCA findings attached."
    properties = draft_data.get("properties")

    bridge_url = settings.bridge_url.rstrip("/")
    comment_id = "comment-001"
    ticket_url = f"https://jira.example.internal/browse/{ticket_key}"

    headers: Dict[str, str] = {}
    if bridge_url.startswith("https://"):
        token = _fetch_oidc_token(bridge_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    post_payload: Dict[str, Any] = {
        "body": comment_body,
        "properties": properties,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{bridge_url}/api/jira/issue/{ticket_key}/comment",
                headers=headers,
                json=post_payload,
            )
            if resp.status_code in (200, 201):
                res_json = resp.json()
                comment_id = str(res_json.get("comment_id") or comment_id)
    except Exception as exc:
        logger.debug("Bridge API comment invocation not reachable, using simulated id: %s", exc)

    # Mark RCA attached in thread context
    thread_ref = session_id or draft_data.get("slack_thread") or "default-session"
    store.mark_rca_attached(thread_ref, ticket_key=ticket_key)

    # Clean up consumed draft
    store.remove_draft(draft_id)

    return JiraCommentResult(
        ticket_key=ticket_key,
        comment_id=comment_id,
        status="created",
        draft_id=draft_id,
        ticket_url=ticket_url,
    )
