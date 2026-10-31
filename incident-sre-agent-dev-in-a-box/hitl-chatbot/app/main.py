"""FastAPI application entrypoint for HITL Chatbot."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.agent import ChatbotAgentRunner
from app.auth import verify_api_auth, verify_internal_callback_auth

logger = logging.getLogger(__name__)

app = FastAPI(
    title="HITL SRE Chatbot",
    description="Interactive SRE Chatbot with Human-In-The-Loop confirmation gates",
    version="0.1.0",
)

runner = ChatbotAgentRunner()


class ChatRequest(BaseModel):
    """Inbound chat message request."""

    message: str = Field(description="User message text")
    session_id: Optional[str] = Field(
        default="default-session", description="Conversation session ID"
    )
    user_id: Optional[str] = Field(
        default=None, description="User ID of the user sending the message"
    )


class ChatResponse(BaseModel):
    """Response payload for chat turn."""

    type: str
    text: str
    data: Optional[Dict[str, Any]] = None
    signed_token: Optional[str] = None


class ApprovalRequest(BaseModel):
    """Request to execute approved mutating side-effect using signed token."""

    signed_token: str = Field(description="HMAC-SHA256 signed approval token")
    session_id: Optional[str] = Field(
        default="default-session", description="Conversation session ID"
    )
    approver_id: Optional[str] = Field(default=None, description="User ID of the approving user")


@app.get("/health")
def health_check() -> Dict[str, str]:
    """Container health check endpoint."""
    return {"status": "healthy", "service": "hitl-chatbot", "version": "0.1.0"}


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_auth)])
def handle_chat_message(request: ChatRequest) -> ChatResponse:
    """Process conversation turn and return response or draft ticket with signed token."""
    res = runner.process_turn(
        user_message=request.message,
        session_id=request.session_id or "default-session",
        user_id=request.user_id,
    )
    return ChatResponse(
        type=res.get("type", "text"),
        text=res.get("text", ""),
        data=res.get("draft") or res.get("data"),
        signed_token=res.get("signed_token"),
    )


@app.post("/api/chat/approve", dependencies=[Depends(verify_api_auth)])
def handle_approval(request: ApprovalRequest) -> Dict[str, Any]:
    """Validate signed approval token and execute side-effect (Jira ticket creation)."""
    try:
        res = runner.execute_approval(
            signed_token=request.signed_token,
            session_id=request.session_id or "default-session",
            approver_id=request.approver_id,
        )
        return res
    except ValueError as exc:
        err_msg = str(exc)
        if "not authorized" in err_msg.lower() or "rejected" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=err_msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg,
        ) from exc


@app.api_route("/slack/events", methods=["GET", "POST"])
async def slack_events(request: Request) -> Response:
    """Slack events endpoint (disabled in core; enabled via Slack adapter in PR 5)."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Slack integration is disabled (requires PR 5 Slack integration package)",
    )


class RcaCallbackRequest(BaseModel):
    """Payload for bridge RCA callback."""

    ticket_key: str = Field(description="Jira ticket key")
    thread_ref: Optional[str] = Field(default=None, description="Slack or chat thread reference")
    report: Dict[str, Any] = Field(default_factory=dict, description="Investigation report")


@app.post("/internal/rca-callback", dependencies=[Depends(verify_internal_callback_auth)])
async def handle_rca_callback(request: Request, payload: RcaCallbackRequest) -> Dict[str, Any]:
    """Receive RCA investigation callback from incident bridge and cache in conversation store."""
    caller = getattr(request.state, "caller_email", "unknown")
    logger.info(
        "Received RCA callback for ticket %s from caller %s (thread_ref=%s)",
        payload.ticket_key,
        caller,
        payload.thread_ref,
    )
    runner.store.record_rca_report(payload.ticket_key, payload.report)
    runner.store.update_active_ticket_rca(
        payload.ticket_key, payload.report, thread_id=payload.thread_ref
    )
    return {"status": "received", "ticket_key": payload.ticket_key}


@app.get("/api/investigations/{ticket_key}", dependencies=[Depends(verify_api_auth)])
def get_investigation_status(ticket_key: str) -> Dict[str, Any]:
    """Get investigation status and report for ticket key."""
    report = runner.store.get_rca_report(ticket_key)
    if report:
        return {"status": "completed", "ticket_key": ticket_key, "report": report}
    return {"status": "pending", "ticket_key": ticket_key, "report": None}
