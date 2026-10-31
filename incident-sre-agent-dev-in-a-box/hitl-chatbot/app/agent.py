"""ADK LlmAgent core configuration, tool binding, and intent execution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from google.adk.agents import LlmAgent

from app.config import get_settings
from app.session import get_conversation_store
from app.tools.alert import get_latest_incident, resolve_alert
from app.tools.jira_create import create_jira_issue
from app.tools.jira_draft import draft_jira_ticket
from app.tools.mcp_client import get_recent_deployments, lookup_alert_context

logger = logging.getLogger(__name__)


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
        lowered = user_message.lower()

        # Case A: Query latest rollout / deployment changes
        if any(term in lowered for term in ["rollout", "deploy", "what changed"]):
            response_data = self._handle_rollout_query(target_workload, target_cluster)
            self.store.add_message(session_id, "assistant", response_data["text"])
            return response_data

        # Case B: File / create Jira ticket in response to alert
        if any(term in lowered for term in ["jira", "ticket", "file", "create issue", "alert"]):
            response_data = self._handle_jira_draft_intent(
                user_message, session_id, user_id=user_id, author=author
            )
            self.store.add_message(session_id, "assistant", response_data["text"])
            return response_data

        # Fallback general query
        text = (
            "I can help you inspect recent rollouts (Case A) or file JIRA "
            "incident tickets for alerts (Case B)."
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

    def _handle_jira_draft_intent(
        self,
        user_message: str,
        session_id: str,
        user_id: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute Case B Turn 1: Resolve alert and render ticket draft with signed token."""
        # Extract alert query candidate from message
        alert_query = user_message
        if "response to" in user_message.lower():
            parts = user_message.lower().split("response to", 1)
            if len(parts) > 1:
                alert_query = parts[1].strip()

        # Step 1: Fuzzy resolve alert policy
        policy_match = resolve_alert(alert_query)

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

        lines = [
            f"=== {header} ===",
            f"Draft ID:        {draft.draft_id}",
            f"Project:         {draft.project_key} ({draft.issue_type})",
            f"Priority:        {draft.priority}",
            f"Summary:         {draft.summary}",
            f"Alert Policy:    {policy_match.display_name} ({policy_match.policy_id})",
            f"Violation ID:    {draft.incident_id}",
            f"Labels:          {', '.join(draft.labels)}",
        ]
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
        return {
            "type": "draft_ticket",
            "text": formatted_text,
            "draft": draft.model_dump(),
            "signed_token": draft.signed_token,
        }

    def execute_approval(
        self,
        signed_token: str,
        session_id: str = "default-session",
        approver_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute Case B Turn 2: Validate token and invoke create_jira_issue."""
        result = create_jira_issue(
            signed_token,
            approver_id=approver_id,
            session_id=session_id,
        )
        header = self.prompts.get("creation_success_header", "Jira Issue Successfully Created")

        text = (
            f"✅ {header}\n"
            f"Issue Key:   {result.issue_key}\n"
            f"Issue URL:   {result.issue_url}\n"
            f"Status:      {result.status}\n"
            f"Incident ID: {result.incident_id}"
        )
        self.store.add_message(session_id, "assistant", text)
        return {
            "type": "issue_created",
            "text": text,
            "result": result.model_dump(),
        }
