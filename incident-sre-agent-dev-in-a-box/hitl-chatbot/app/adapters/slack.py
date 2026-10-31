"""Slack Bolt adapter for HITL Chatbot.

Provides lazy initialization, async event handling with strict <3-second ack,
threading, Block Kit HITL approval cards with requester-only authorization gate,
and RCA callback delivery to Slack threads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.config import get_settings

if TYPE_CHECKING:
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# Global singletons for lazy-built Slack app and handler
_slack_app: Optional[AsyncApp] = None
_slack_handler: Optional[AsyncSlackRequestHandler] = None


def _strip_mention(text: str) -> str:
    """Remove leading bot mention (<@USERID>) from channel messages."""
    return re.sub(r"^<@[A-Z0-9]+>\s*", "", text or "").strip()


def _parse_args(text: str) -> List[str]:
    """Split command text into arguments."""
    return text.strip().split() if text and text.strip() else []


def _arg(parts: List[str], index: int, default: str = "") -> str:
    """Retrieve an argument by index with default fallback."""
    return parts[index] if index < len(parts) else default


def get_slack_app() -> Optional[AsyncApp]:
    """Return the active AsyncApp instance if initialized."""
    return _slack_app


def get_slack_handler() -> Optional[AsyncSlackRequestHandler]:
    """Return the active AsyncSlackRequestHandler instance if initialized."""
    return _slack_handler


def build_slack_handler() -> Optional[AsyncSlackRequestHandler]:
    """Build and return the AsyncSlackRequestHandler.

    Returns None if SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET are missing or empty.
    """
    global _slack_app, _slack_handler

    settings = get_settings()
    token = (
        settings.slack_bot_token
        or os.environ.get("SLACK_BOT_TOKEN")
        or os.environ.get("HITL_SLACK_BOT_TOKEN")
        or ""
    ).strip()
    signing_secret = (
        settings.slack_signing_secret
        or os.environ.get("SLACK_SIGNING_SECRET")
        or os.environ.get("HITL_SLACK_SIGNING_SECRET")
        or ""
    ).strip()

    if not token or not signing_secret:
        logger.warning(
            "SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET not set — Slack integration disabled."
        )
        _slack_app = None
        _slack_handler = None
        return None

    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.async_app import AsyncApp

    _slack_app = AsyncApp(token=token, signing_secret=signing_secret)
    _register_handlers(_slack_app)
    _slack_handler = AsyncSlackRequestHandler(_slack_app)
    return _slack_handler


def _register_handlers(app: AsyncApp) -> None:
    """Register event, action, and command listeners on the Slack AsyncApp."""

    # ── Channel @-mentions ─────────────────────────────────────────────────
    @app.event("app_mention")
    async def handle_mention(event: Dict[str, Any], client: Any) -> None:
        """Handle app mention event with immediate ack and async worker."""
        # Non-blocking async dispatch ensures <3s Slack acknowledgment
        asyncio.create_task(_process_nl_event(event, client))

    # ── Direct Messages (DMs) ───────────────────────────────────────────────
    @app.event("message")
    async def handle_message(event: Dict[str, Any], client: Any) -> None:
        """Handle direct messages; ignores bots and edits."""
        if event.get("channel_type") != "im":
            return
        if event.get("subtype") or event.get("bot_id"):
            return
        asyncio.create_task(_process_nl_event(event, client))

    # ── Interactive Actions: HITL Ticket Approval ───────────────────────────
    @app.action("hitl_approve_ticket")
    @app.action("hitl_approve_jira")
    async def handle_approve_ticket(
        ack: Any,
        body: Dict[str, Any],
        client: Any,
        respond: Any,
    ) -> None:
        """Process HITL approval button click with requester-only gate."""
        await ack()
        await _process_approval_action(body=body, client=client, respond=respond)

    # ── Interactive Actions: HITL Ticket Cancellation ───────────────────────
    @app.action("hitl_cancel_ticket")
    @app.action("hitl_cancel_jira")
    async def handle_cancel_ticket(
        ack: Any,
        body: Dict[str, Any],
        client: Any,
        respond: Any,
    ) -> None:
        """Process HITL cancel button click."""
        await ack()
        channel_id = body.get("channel", {}).get("id") or body.get("container", {}).get(
            "channel_id"
        )
        message_ts = body.get("container", {}).get("message_ts") or body.get("message", {}).get(
            "ts"
        )
        cancel_msg = "❌ Ticket creation cancelled."
        if channel_id and message_ts:
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=cancel_msg,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": cancel_msg},
                        }
                    ],
                )
                return
            except Exception as exc:
                logger.debug("chat_update failed on cancel, falling back to respond: %s", exc)
        await respond(text=cancel_msg, replace_original=True)


async def _process_nl_event(event: Dict[str, Any], client: Any) -> None:
    """Async background worker for natural language turns."""
    from app.main import runner

    user_id = event.get("user", "")
    raw_text = event.get("text", "")
    text = _strip_mention(raw_text)
    channel_id = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")

    if not text or not channel_id:
        return

    session_id = f"{channel_id}:{thread_ts}"
    author = f"<@{user_id}>" if user_id else None

    try:
        res = await asyncio.to_thread(
            runner.process_turn,
            user_message=text,
            session_id=session_id,
            user_id=user_id,
            author=author,
        )
    except Exception as exc:
        logger.exception("Error processing turn for user %s: %s", user_id, exc)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"⚠️ An error occurred while processing your request: {exc}",
        )
        return

    res_type = res.get("type", "text")

    if res_type == "draft_ticket":
        draft = res.get("draft") or {}
        signed_token = res.get("signed_token") or draft.get("signed_token", "")
        draft_id = draft.get("draft_id", "draft")
        requester_id = draft.get("requester_id") or user_id
        summary = draft.get("summary", "Incident Alert")
        priority = draft.get("priority", "P2")
        project_key = draft.get("project_key", "AMJTEST")
        incident_id = draft.get("incident_id", "")

        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Jira Incident Ticket Preview*\n"
                        f"• *Draft ID:* `{draft_id}`\n"
                        f"• *Project:* `{project_key}` | *Priority:* `{priority}`\n"
                        f"• *Summary:* {summary}\n"
                        f"• *Violation:* `{incident_id}`\n"
                        f"• *Requester:* <@{requester_id}>"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "hitl_jira_gate",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve & Create Ticket"},
                        "style": "primary",
                        "action_id": "hitl_approve_ticket",
                        "value": signed_token,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "hitl_cancel_ticket",
                        "value": draft_id,
                    },
                ],
            },
        ]
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=res.get("text", "Jira ticket draft preview"),
            blocks=blocks,
        )
    elif res_type == "draft_comment":
        draft = res.get("draft") or {}
        signed_token = res.get("signed_token") or draft.get("signed_token", "")
        draft_id = draft.get("draft_id", "comment")
        requester_id = draft.get("requester_id") or user_id
        ticket_key = draft.get("ticket_key", "active ticket")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Jira Comment / RCA Attachment Preview*\n"
                        f"• *Draft ID:* `{draft_id}`\n"
                        f"• *Target Ticket:* `{ticket_key}`\n"
                        f"• *Action:* Attach RCA Dossier\n"
                        f"• *Requester:* <@{requester_id}>"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "hitl_jira_gate",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve & Add Comment"},
                        "style": "primary",
                        "action_id": "hitl_approve_ticket",
                        "value": signed_token,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "hitl_cancel_ticket",
                        "value": draft_id,
                    },
                ],
            },
        ]
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=res.get("text", "Jira comment preview"),
            blocks=blocks,
        )
    else:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=res.get("text", ""),
        )


async def _process_approval_action(
    body: Dict[str, Any],
    client: Any,
    respond: Any,
) -> None:
    """Validate requester authorization and execute approved ticket creation."""
    from app.main import runner
    from app.tokens import verify_signed_token

    settings = get_settings()

    approver_id = body.get("user", {}).get("id") or ""
    channel_id = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    message_ts = body.get("container", {}).get("message_ts") or body.get("message", {}).get("ts")
    thread_ts = (
        body.get("container", {}).get("thread_ts")
        or body.get("message", {}).get("thread_ts")
        or message_ts
    )

    actions = body.get("actions", [])
    signed_token = actions[0].get("value", "") if actions else ""

    if not signed_token:
        await respond("Error: Missing approval token payload.")
        return

    # Inspect token payload to verify requester identity
    _, _, payload = verify_signed_token(
        signed_token,
        settings.hmac_secret_key,
    )
    draft_id = str(payload.get("draft_id", ""))
    stored_draft = runner.store.get_draft(draft_id) if draft_id else None
    requester_id = (stored_draft and stored_draft.get("requester_id")) or payload.get(
        "requester_id"
    )

    # Requester-only authorization gate
    if requester_id and approver_id != requester_id:
        rejection_msg = (
            f"Approval rejected: user '{approver_id}' is not authorized to "
            f"approve draft created by '{requester_id}'"
        )
        logger.warning(
            "Rejecting approval click: approver %s != requester %s",
            approver_id,
            requester_id,
        )
        # Post ephemeral message to rejecting user; keep draft/token unconsumed
        posted_ephemeral = False
        if channel_id and approver_id:
            try:
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=approver_id,
                    text=rejection_msg,
                )
                posted_ephemeral = True
            except Exception as exc:
                logger.debug("chat_postEphemeral failed, trying respond: %s", exc)

        if not posted_ephemeral:
            try:
                await respond(text=rejection_msg, response_type="ephemeral")
            except Exception:
                if channel_id and thread_ts:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=rejection_msg,
                    )
        return

    # Authorized: execute ticket creation flow
    session_id = f"{channel_id}:{thread_ts}" if (channel_id and thread_ts) else "default-session"
    try:
        approval_res = await asyncio.to_thread(
            runner.execute_approval,
            signed_token=signed_token,
            session_id=session_id,
            approver_id=approver_id,
        )
        res_data = approval_res.get("result", {})
        issue_key = (
            res_data.get("issue_key")
            or approval_res.get("ticket_key")
            or (stored_draft and stored_draft.get("ticket_key"))
            or "AMJTEST-XX"
        )
        issue_url = (
            res_data.get("issue_url")
            or res_data.get("ticket_url")
            or approval_res.get("ticket_url")
            or f"https://google-autocloud-team.atlassian.net/browse/{issue_key}"
        )
        draft_action = (
            (stored_draft and stored_draft.get("action"))
            or approval_res.get("action")
            or "create_ticket"
        )

        # Record Jira ticket key to Slack thread reference mapping
        if channel_id and thread_ts:
            runner.store.record_ticket_thread(issue_key, f"{channel_id}:{thread_ts}")

        # If ticket already had RCA completed, deliver RCA report to this Slack thread
        if res_data.get("status") == "already_exists" and channel_id and thread_ts:
            existing_rca = runner.store.get_rca_report(issue_key)
            if existing_rca:
                asyncio.create_task(
                    deliver_rca_to_slack(
                        issue_key,
                        existing_rca,
                        thread_ref=f"{channel_id}:{thread_ts}",
                    )
                )

        if draft_action == "comment":
            confirmation_msg = f"✅ RCA / Comment added to {issue_key}: {issue_url}"
        else:
            confirmation_msg = f"✅ Ticket {issue_key} Created. Investigation running ⏳"

        # Update interactive message to replace buttons with confirmation
        if channel_id and message_ts:
            try:
                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=confirmation_msg,
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": confirmation_msg,
                            },
                        }
                    ],
                )
                return
            except Exception as exc:
                logger.debug("chat_update failed, falling back to respond: %s", exc)

        await respond(text=confirmation_msg, replace_original=True)

    except ValueError as exc:
        err_msg = str(exc)
        logger.warning("Approval execution failed: %s", err_msg)
        if "not authorized" in err_msg.lower() or "rejected" in err_msg.lower():
            if channel_id and approver_id:
                try:
                    await client.chat_postEphemeral(
                        channel=channel_id,
                        user=approver_id,
                        text=err_msg,
                    )
                    return
                except Exception:
                    pass
            await respond(text=err_msg, response_type="ephemeral")
        else:
            await respond(text=f"Error creating ticket: {err_msg}")


async def deliver_rca_to_slack(
    ticket_key: str,
    report: Dict[str, Any],
    thread_ref: Optional[str] = None,
) -> bool:
    """Lookup thread reference and post RCA findings directly into Slack thread."""
    from app.session import get_conversation_store

    store = get_conversation_store()
    ref = thread_ref or store.get_ticket_thread(ticket_key)
    if not ref or ":" not in ref:
        logger.info("No Slack thread reference found for ticket %s", ticket_key)
        return False

    channel_id, thread_ts = ref.split(":", 1)
    if _slack_app is None:
        build_slack_handler()
    if _slack_app is None:
        logger.warning("Slack integration not configured; cannot deliver RCA to Slack")
        return False

    summary = (
        report.get("executive_summary")
        or report.get("summary")
        or f"RCA investigation completed for {ticket_key}."
    )
    root_cause = report.get("root_cause") or report.get("root_cause_analysis") or ""
    report_url = (
        report.get("report_url") or report.get("html_report_url") or report.get("url") or ""
    )

    lines = [
        f"🎯 *Root Cause Analysis (RCA) Completed for {ticket_key}*",
        "",
        f"*Summary:* {summary}",
    ]
    if root_cause:
        lines.append(f"*Root Cause:* {root_cause}")
    if report_url:
        lines.append(f"📊 *Report:* <{report_url}|View Diagnostic Report>")

    text = "\n".join(lines)

    try:
        await _slack_app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
        logger.info("Successfully delivered RCA for %s to Slack thread %s", ticket_key, ref)
        return True
    except Exception as exc:
        logger.exception("Failed to deliver RCA to Slack thread %s: %s", ref, exc)
        return False
