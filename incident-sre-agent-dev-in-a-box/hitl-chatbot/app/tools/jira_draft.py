"""Jira incident ticket draft rendering and signed token generation."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.config import get_settings
from app.session import get_conversation_store
from app.tokens import create_signed_token
from app.tools.alert import get_latest_incident, resolve_alert

logger = logging.getLogger(__name__)


class DraftTicket(BaseModel):
    """Rendered preview of a pending Jira ticket with signed confirmation token."""

    draft_id: str = Field(description="Unique draft identifier")
    signed_token: str = Field(description="HMAC-SHA256 signed single-use approval token")
    project_key: str = Field(default="AMJTEST", description="Jira project key")
    issue_type: str = Field(default="Task", description="Jira issue type")
    summary: str = Field(description="Jira ticket summary/title")
    description: str = Field(description="Formatted ticket description")
    priority: str = Field(default="P2", description="Incident priority")
    labels: List[str] = Field(default_factory=list, description="Associated labels")
    components: List[str] = Field(default_factory=list, description="Target components")
    incident_id: str = Field(description="Underlying alert violation ID")
    policy_id: str = Field(description="Associated alert policy ID")
    requester_id: Optional[str] = Field(default=None, description="User ID of the requester")
    slack_thread: Optional[str] = Field(default=None, description="Slack thread reference")
    author: Optional[str] = Field(default=None, description="Requester author string")
    alert_started_at: Optional[str] = Field(
        default=None, description="Alert violation start timestamp"
    )


def draft_jira_ticket(
    policy_id: str,
    priority: str = "P2",
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    requester_id: Optional[str] = None,
) -> DraftTicket:
    """Render a draft incident ticket and generate an HMAC-SHA256 signed approval token."""
    from datetime import datetime, timezone

    settings = get_settings()
    overrides_dict = overrides or {}

    # Resolve policy details and latest incident violation
    policy_match = resolve_alert(policy_id)
    violation = get_latest_incident(policy_id)

    draft_id = f"draft-{uuid.uuid4().hex[:8]}"
    incident_id = violation.incident_id

    # Compute signed single-use approval token
    signed_token = create_signed_token(
        draft_id=draft_id,
        incident_id=incident_id,
        secret_key=settings.hmac_secret_key,
        ttl_seconds=settings.token_expiry_seconds,
        requester_id=requester_id,
    )

    summary = overrides_dict.get(
        "summary",
        f"[ALERT] Uptime check failed for {policy_match.display_name}",
    )
    ticket_priority = overrides_dict.get("priority", priority)
    labels = ["hitl", f"alert-{incident_id}"]
    if "labels" in overrides_dict:
        labels.extend(overrides_dict["labels"])

    components = overrides_dict.get("components", ["Infrastructure / SRE"])
    target_endpoint = violation.target_endpoint or policy_match.target_endpoint or "unknown"

    author = overrides_dict.get("author") or (
        f"<@{requester_id}>" if requester_id else "SRE Incident Responder"
    )
    slack_thread = overrides_dict.get("slack_thread") or session_id
    alert_started_at = overrides_dict.get("alert_started_at") or violation.started_at

    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    time_str = now_utc.strftime("%H:%M")

    # Render structured Markdown matching the incident write-up template
    description = (
        f"# **{date_str} {summary}**\n\n"
        f"**Date:** {date_str}\n\n"
        f"**Authors:** {author}\n\n"
        f"**Status:** In progress\n\n"
        f"**Statuspage:**\n\n"
        f"- **Reflected On StatusPage?:** (if not, include explanation why)\n"
        f"- **Status/Impact Reported?:** (i.e, Minor, Degraded Performance; "
        f"Major, Partial Outage; Critical, Major Outage)\n\n"
        f"**Summary:**\n"
        f"{violation.summary}\n\n"
        f"**Impact / Observations (1. customer/externally facing impact/observations; "
        f"2. internally facing impact/observations; etc)**\n"
        f"- Service impairment observed on {target_endpoint}\n\n"
        f"**Impacted Customers (how many customers are impacted, "
        f"which regions are involved, etc):**\n"
        f"- todo\n\n"
        f"**Root Causes:**\n"
        f"- todo\n\n"
        f"**Trigger:**\n"
        f"- Alert policy {policy_match.display_name} ({policy_id}) "
        f"violation for {target_endpoint}\n\n"
        f"**Detection:**\n"
        f"- Started At: {alert_started_at}\n"
        f"- Cloud Monitoring alert violation {incident_id} detected at {violation.started_at}. "
        f"Uptime check failed for {target_endpoint}\n\n"
        f"**Initial Response:**\n"
        f"- HITL ticket filed via Slack by {author}\n\n"
        f"**Resolution:**\n"
        f"- todo\n\n"
        f"**Action Items / Jira Tickets:**\n\n"
        f"| | | |\n"
        f"| --- | --- | --- |\n"
        f"| | | **TODO** |\n"
        f"| | | **TODO** |\n\n"
        f"**Timeline:**\n"
        f"- Started At: {alert_started_at}\n"
        f"- {date_str} {time_str} UTC - Alert violation detected on {target_endpoint}\n\n"
        f"**Lessons Learned and Incident Feedback:**\n\n"
        f"- **What went well:** Automated alert fired promptly\n"
        f"- **What went wrong:** todo\n"
        f"- **What should we do next:** todo\n"
        f"- **Where we got lucky:** todo\n\n"
        f"**Supporting information**\n"
        f"- Cloud Monitoring policy: {policy_id}, Incident: {incident_id}, "
        f"Endpoint: {target_endpoint}\n"
    )

    draft = DraftTicket(
        draft_id=draft_id,
        signed_token=signed_token,
        project_key=settings.jira_project_key,
        issue_type=settings.jira_issue_type,
        summary=summary,
        description=description,
        priority=ticket_priority,
        labels=labels,
        components=components,
        incident_id=incident_id,
        policy_id=policy_id,
        requester_id=requester_id,
        slack_thread=slack_thread,
        author=author,
        alert_started_at=alert_started_at,
    )

    # Save draft into in-memory conversation store
    store = get_conversation_store()
    store.save_draft(draft_id, draft.model_dump(), session_id=session_id)

    return draft
