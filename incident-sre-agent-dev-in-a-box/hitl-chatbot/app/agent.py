"""ADK LlmAgent core configuration, tool binding, and intent execution."""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from google.adk.agents import LlmAgent

from app.config import get_settings
from app.session import get_conversation_store
from app.tokens import create_signed_token
from app.tools.alert import get_latest_incident, resolve_alert
from app.tools.jira_create import add_jira_comment, create_jira_issue
from app.tools.jira_draft import draft_jira_ticket
from app.tools.mcp_client import get_recent_deployments, lookup_alert_context

logger = logging.getLogger(__name__)


class ChatbotIntent(str, Enum):
    """Categorized conversational intent for SRE chatbot turns."""

    UPDATE_TICKET = "update_ticket"
    CREATE_TICKET = "create_ticket"
    LOOKUP_DEPLOYMENTS = "lookup_deployments"
    CLARIFICATION = "clarification"
    GENERAL_QUERY = "general_query"


def classify_intent(message: str) -> Tuple[ChatbotIntent, float]:
    """Classify user intent with distinct evaluation paths.

    Evaluation precedence:
      1. UPDATE_TICKET / ADD_COMMENT: Evaluated BEFORE create path.
         Recognizes verbs/phrases: update, comment, attach, add rca, attach rca,
         with your rca, post rca. Never treats an entire sentence as an alert policy name.
      2. LOOKUP_DEPLOYMENTS: Rollout / deployment queries.
      3. CREATE_TICKET: Explicit ticket filing/creation intent.
      4. CLARIFICATION / GENERAL_QUERY: Ambiguous prompts ask for clarification
         rather than greedily defaulting to alert search.
    """
    lowered = message.lower().strip()

    # 1. UPDATE_TICKET / ADD_COMMENT
    update_patterns = [
        r"\bupdate\b",
        r"\bcomment\b",
        r"\battach\b",
        r"\badd\s+rca\b",
        r"\battach\s+rca\b",
        r"\bpost\s+rca\b",
        r"\bappend\s+rca\b",
        r"\bwith\s+(?:your\s+)?rca\b",
    ]
    if any(re.search(pat, lowered) for pat in update_patterns):
        return ChatbotIntent.UPDATE_TICKET, 1.0

    # 2. LOOKUP_DEPLOYMENTS
    deploy_patterns = [
        r"\brollout\b",
        r"\bdeploy(?:ment)?s?\b",
        r"\bwhat\s+changed\b",
        r"\brecent\s+deploy\b",
    ]
    if any(re.search(pat, lowered) for pat in deploy_patterns):
        return ChatbotIntent.LOOKUP_DEPLOYMENTS, 1.0

    # 3. CREATE_TICKET (Explicit ticket filing/creation intent)
    create_patterns = [
        r"\bfile\s+(?:a\s+)?(?:jira\s+)?(?:ticket|issue|incident)\b",
        r"\bcreate\s+(?:a\s+)?(?:jira\s+)?(?:ticket|issue|incident)\b",
        r"\bopen\s+(?:a\s+)?(?:jira\s+)?(?:ticket|issue|incident)\b",
        r"\bfile\s+(?:an?\s+)?alert\b",
        r"\bcreate\s+(?:an?\s+)?alert\b",
        r"\bopen\s+(?:an?\s+)?alert\b",
    ]
    if any(re.search(pat, lowered) for pat in create_patterns):
        return ChatbotIntent.CREATE_TICKET, 1.0

    if re.search(r"\b(ticket|jira|incident)\b", lowered) and re.search(
        r"\b(file|create|open|new)\b", lowered
    ):
        return ChatbotIntent.CREATE_TICKET, 0.9

    # 4. Ambiguous alert queries ask for clarification
    if re.search(r"\balert\b", lowered):
        return ChatbotIntent.CLARIFICATION, 0.5

    return ChatbotIntent.GENERAL_QUERY, 0.5


def load_prompts(prompts_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load externalized instructions and prompt templates from YAML."""
    path = prompts_path or get_settings().prompts_path
    if not path.is_file():
        raise FileNotFoundError(f"Prompts file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        return data or {}


def build_adk_agent(prompts_path: Optional[Path] = None) -> LlmAgent:
    """Instantiate and configure the Google ADK LlmAgent with externalized instruction and tools."""
    settings = get_settings()
    prompt_config = load_prompts(prompts_path)
    system_instruction = prompt_config.get("system_instruction", "")

    tools: List[Any] = [
        resolve_alert,
        get_latest_incident,
        get_recent_deployments,
        lookup_alert_context,
        draft_jira_ticket,
        create_jira_issue,
    ]

    agent = LlmAgent(
        name="hitl_chatbot",
        model=settings.gemini_model,
        instruction=system_instruction,
        tools=tools,
    )
    return agent


class ChatbotAgentRunner:
    """Core execution engine handling multi-turn conversational intent dispatch."""

    def __init__(self, prompts_path: Optional[Path] = None) -> None:
        self.prompts = load_prompts(prompts_path)
        self.store = get_conversation_store()

    def process_turn(
        self,
        user_message: str,
        session_id: str = "default-session",
        target_workload: Optional[str] = None,
        target_cluster: Optional[str] = None,
        user_id: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process an inbound conversation turn and return structured response."""
        self.store.add_message(session_id, "user", user_message)

        # Consult thread-scoped active ticket context on EVERY turn
        active_ticket = self.store.get_active_ticket(session_id)

        intent, confidence = classify_intent(user_message)

        # Structured logging
        logger.info(
            "Turn routing decision: intent=%s, policy_name=%s, incident_id=%s, "
            "ticket_key=%s, thread_id=%s, confidence=%.2f",
            intent.value,
            active_ticket.get("policy_name") if active_ticket else None,
            active_ticket.get("incident_id") if active_ticket else None,
            active_ticket.get("ticket_key") if active_ticket else None,
            session_id,
            confidence,
        )

        # Distinct intent dispatch paths
        if intent == ChatbotIntent.UPDATE_TICKET:
            res = self._handle_update_ticket_intent(
                user_message, session_id, active_ticket, user_id=user_id, author=author
            )
            self.store.add_message(session_id, "assistant", res["text"])
            return res

        elif intent == ChatbotIntent.LOOKUP_DEPLOYMENTS:
            res = self._handle_rollout_query(target_workload, target_cluster)
            self.store.add_message(session_id, "assistant", res["text"])
            return res

        elif intent == ChatbotIntent.CREATE_TICKET:
            res = self._handle_jira_draft_intent(
                user_message,
                session_id,
                active_ticket=active_ticket,
                user_id=user_id,
                author=author,
            )
            self.store.add_message(session_id, "assistant", res["text"])
            return res

        elif intent == ChatbotIntent.CLARIFICATION:
            text = self.prompts.get(
                "clarification_prompt",
                "Did you mean to file a ticket for an alert? "
                "Please specify the exact alert policy name.",
            )
            self.store.add_message(session_id, "assistant", text)
            return {"type": "clarification", "text": text}

        else:
            text = self.prompts.get(
                "ambiguous_prompt_fallback",
                "I can help you inspect recent rollouts, file a ticket for an alert, "
                "or update an active ticket with RCA.",
            )
            self.store.add_message(session_id, "assistant", text)
            return {"type": "info", "text": text}

    def _handle_rollout_query(
        self,
        workload: Optional[str] = None,
        cluster: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute Case A: Call MCP get_recent_deployments and format deployment summary."""
        events_resp = get_recent_deployments(workload=workload, cluster=cluster, limit=5)

        intro = self.prompts.get("case_a_intro", "Here are recent deployment events:")
        notice = events_resp.missing_pipeline_notice or self.prompts.get(
            "missing_pipeline_notice",
            "",
        )

        lines = [intro, ""]
        for ev in events_resp.deployments:
            lines.append(f"• Event: {ev.event_id} ({ev.timestamp})")
            lines.append(
                f"  Workload: {ev.workload or 'unknown'} | Revision: {ev.revision or 'N/A'}"
            )
            lines.append(f"  Tool: {ev.tool or 'N/A'} | Deployer: {ev.deployer or 'N/A'}")
            lines.append(f"  Image: {ev.image or 'N/A'}")
            if ev.change_summary:
                lines.append(f"  Summary: {ev.change_summary}")
            lines.append("")

        if notice:
            lines.append(f"ℹ️ {notice}")

        formatted_text = "\n".join(lines).strip()
        return {
            "type": "deployments",
            "text": formatted_text,
            "data": events_resp.model_dump(),
        }

    def _handle_update_ticket_intent(
        self,
        user_message: str,
        session_id: str,
        active_ticket: Optional[Dict[str, Any]],
        user_id: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle UPDATE_TICKET / ADD_COMMENT flow using active ticket context."""
        if not active_ticket:
            text = self.prompts.get(
                "no_active_ticket_notice",
                "There is no active ticket in this thread. "
                "Please specify which ticket key to update (e.g. AMJTEST-20).",
            )
            return {"type": "info", "text": text}

        ticket_key = active_ticket.get("ticket_key", "UNKNOWN")
        ticket_url = active_ticket.get(
            "ticket_url", f"https://jira.example.internal/browse/{ticket_key}"
        )
        rca_attached = active_ticket.get("rca_attached", False)

        # 1. If RCA is already attached to {ticket_key}, reply immediately:
        if rca_attached:
            tmpl = self.prompts.get(
                "rca_already_attached_notice",
                "RCA is already attached to {ticket_key}: {url}",
            )
            text = tmpl.format(ticket_key=ticket_key, url=ticket_url)
            return {
                "type": "info",
                "text": text,
                "ticket_key": ticket_key,
                "ticket_url": ticket_url,
            }

        # 2. If RCA is available but not yet attached, generate signed approval card
        rca_content = active_ticket.get("rca_content") or self.store.get_rca_report(ticket_key)
        rca_status = active_ticket.get("rca_status", "pending")

        if rca_content or rca_status == "ready":
            draft_id = f"comment-{uuid.uuid4().hex[:8]}"
            incident_id = active_ticket.get("incident_id") or ticket_key

            settings = get_settings()
            signed_token = create_signed_token(
                draft_id=draft_id,
                incident_id=incident_id,
                secret_key=settings.hmac_secret_key,
                ttl_seconds=settings.token_expiry_seconds,
                requester_id=user_id,
            )

            rca_summary = (
                rca_content.get("root_cause")
                if isinstance(rca_content, dict)
                else "Root cause analysis completed by SRE Agent."
            )
            comment_body = (
                f"h2. Root Cause Analysis (RCA)\n"
                f"*Summary:* {rca_summary}\n"
                f"*Investigated Ticket:* {ticket_key}\n"
                f"*Attached By:* {author or user_id or 'SRE Responder'}\n"
            )

            draft_data = {
                "draft_id": draft_id,
                "action": "comment",
                "ticket_key": ticket_key,
                "body": comment_body,
                "incident_id": incident_id,
                "requester_id": user_id,
                "slack_thread": session_id,
                "signed_token": signed_token,
            }
            self.store.save_draft(draft_id, draft_data, session_id=session_id)

            header = self.prompts.get(
                "comment_preview_header", "Jira Comment / RCA Attachment Preview"
            )
            action_notice = self.prompts.get(
                "comment_action_notice",
                "Action Required: Appending a comment to Jira creates an external side-effect. "
                "Please confirm update [Y/n].",
            )

            lines = [
                f"=== {header} ===",
                f"Draft ID:        {draft_id}",
                "Action:          Add Comment (RCA Dossier)",
                f"Target Ticket:   {ticket_key}",
                f"RCA Summary:     {rca_summary}",
            ]
            if user_id:
                lines.append(f"Requester:       {user_id}")
            lines.extend(
                [
                    "",
                    "Signed Approval Token:",
                    f"{signed_token}",
                    "",
                    f"⚠️ {action_notice}",
                ]
            )
            formatted_text = "\n".join(lines)
            return {
                "type": "draft_comment",
                "text": formatted_text,
                "draft": draft_data,
                "signed_token": signed_token,
            }

        # 3. If RCA is not ready yet:
        tmpl = self.prompts.get(
            "rca_pending_notice",
            "Investigation is currently in progress for {ticket_key}. "
            "The RCA report is not yet ready to attach.",
        )
        text = tmpl.format(ticket_key=ticket_key)
        return {"type": "info", "text": text, "ticket_key": ticket_key}

    def _handle_jira_draft_intent(
        self,
        user_message: str,
        session_id: str,
        active_ticket: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute Case B Turn 1: Resolve alert and render ticket draft with signed token."""
        # Extract alert query candidate from message
        alert_query = user_message
        lowered = user_message.lower()
        if "response to" in lowered:
            parts = lowered.split("response to", 1)
            if len(parts) > 1:
                alert_query = parts[1].strip()
        elif "for" in lowered:
            parts = lowered.split("for", 1)
            if len(parts) > 1:
                alert_query = parts[1].strip()

        # Clean query: strip trailing words like "in [project] project"
        alert_query = re.sub(
            r"\bin\s+\[?[a-zA-Z0-9_\-]+\]?\s+project.*$",
            "",
            alert_query,
            flags=re.IGNORECASE,
        ).strip()
        alert_query = re.sub(
            r"\b(latest|active|recent|the|an?)\b",
            "",
            alert_query,
            flags=re.IGNORECASE,
        ).strip()

        # Step 1: Fuzzy resolve alert policy
        policy_match = resolve_alert(alert_query)

        # Stricter alert resolution: if match is below threshold or ambiguous
        if policy_match.match_confidence < 0.5 or not policy_match.policy_id:
            if policy_match.suggested_policy:
                tmpl = self.prompts.get(
                    "clarification_prompt",
                    "Did you mean '{policy}'? Please specify the exact alert policy name.",
                )
                text = tmpl.format(policy=policy_match.suggested_policy)
            else:
                text = (
                    f"Could not resolve an alert policy for '{alert_query}'. "
                    "Please specify the exact alert policy name."
                )
            return {"type": "clarification", "text": text}

        # Step 2: Render draft ticket and obtain HMAC signed token
        overrides: Dict[str, Any] = {}
        if author:
            overrides["author"] = author
        if session_id:
            overrides["slack_thread"] = session_id

        draft = draft_jira_ticket(
            policy_match.policy_id,
            session_id=session_id,
            requester_id=user_id,
            overrides=overrides if overrides else None,
        )

        header = self.prompts.get("draft_ticket_header", "Jira Incident Ticket Preview")
        action_notice = self.prompts.get(
            "action_required_notice",
            "Action Required: Confirm creation [Y/n].",
        )

        lines = [f"=== {header} ==="]

        # Duplicate Guard: warn prominently if thread already has an active ticket
        dup_warn = None
        if active_ticket:
            existing_key = active_ticket.get("ticket_key", "UNKNOWN")
            dup_warn = self.prompts.get(
                "duplicate_ticket_warning",
                "Notice: A ticket ({ticket_key}) is already active in this thread. "
                "Did you mean to update {ticket_key} instead?",
            ).format(ticket_key=existing_key)
            lines.extend([f"⚠️ {dup_warn}", ""])

        lines.extend(
            [
                f"Draft ID:        {draft.draft_id}",
                f"Project:         {draft.project_key} ({draft.issue_type})",
                f"Priority:        {draft.priority}",
                f"Summary:         {draft.summary}",
                f"Alert Policy:    {policy_match.display_name} ({policy_match.policy_id})",
                f"Violation ID:    {draft.incident_id}",
                f"Labels:          {', '.join(draft.labels)}",
            ]
        )
        if draft.requester_id:
            lines.append(f"Requester:       {draft.requester_id}")
        lines.extend(
            [
                "",
                "Signed Approval Token:",
                f"{draft.signed_token}",
                "",
                f"⚠️ {action_notice}",
            ]
        )

        formatted_text = "\n".join(lines)
        draft_dict = draft.model_dump()
        if dup_warn:
            draft_dict["active_ticket_warning"] = dup_warn

        return {
            "type": "draft_ticket",
            "text": formatted_text,
            "draft": draft_dict,
            "signed_token": draft.signed_token,
        }

    def execute_approval(
        self,
        signed_token: str,
        session_id: str = "default-session",
        approver_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate token and execute either ticket creation or comment attachment."""
        # Inspect draft_id from token payload to determine action
        draft_action = "create"
        try:
            padded = signed_token + "=" * (-len(signed_token) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            draft_id = str(payload.get("draft_id"))
            draft = self.store.get_draft(draft_id)
            if draft and draft.get("action") == "comment":
                draft_action = "comment"
        except Exception as exc:
            logger.debug("Could not inspect draft action from token: %s", exc)

        if draft_action == "comment":
            comment_result = add_jira_comment(
                signed_token,
                approver_id=approver_id,
                session_id=session_id,
            )
            header = self.prompts.get(
                "comment_success_header",
                "Jira Issue Successfully Updated with RCA",
            )
            text = (
                f"✅ {header}\n"
                f"Ticket Key: {comment_result.ticket_key}\n"
                f"Comment ID: {comment_result.comment_id}\n"
                f"Status:     {comment_result.status}\n"
                f"Ticket URL: {comment_result.ticket_url}"
            )
            self.store.add_message(session_id, "assistant", text)
            return {
                "type": "comment_created",
                "text": text,
                "result": comment_result.model_dump(),
            }

        creation_result = create_jira_issue(
            signed_token,
            approver_id=approver_id,
            session_id=session_id,
        )
        header = self.prompts.get("creation_success_header", "Jira Issue Successfully Created")

        text = (
            f"✅ {header}\n"
            f"Issue Key:   {creation_result.issue_key}\n"
            f"Issue URL:   {creation_result.issue_url}\n"
            f"Status:      {creation_result.status}\n"
            f"Incident ID: {creation_result.incident_id}"
        )
        self.store.add_message(session_id, "assistant", text)
        return {
            "type": "issue_created",
            "text": text,
            "result": creation_result.model_dump(),
        }
