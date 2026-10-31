"""Comprehensive unit tests for Slack Bolt adapter and HITL gate."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.adapters.slack import (
    _arg,
    _parse_args,
    _process_approval_action,
    _process_nl_event,
    _strip_mention,
    build_slack_handler,
    deliver_rca_to_slack,
)
from app.config import get_settings
from app.main import app, runner
from app.tokens import create_signed_token, get_replay_registry


def _make_slack_signature(secret: str, body: bytes, timestamp: str) -> str:
    """Generate Slack HMAC-SHA256 signature."""
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode("utf-8")
    hex_digest = hmac.new(secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return f"v0={hex_digest}"


# ---------------------------------------------------------------------------
# 1. Helper Function Tests
# ---------------------------------------------------------------------------


def test_strip_mention() -> None:
    assert _strip_mention("<@U12345678> hello there") == "hello there"
    assert _strip_mention("<@W01ABCDEF>    what changed in rollout? ") == "what changed in rollout?"
    assert _strip_mention("no mention here") == "no mention here"
    assert _strip_mention("") == ""


def test_parse_args_and_arg() -> None:
    tokens = _parse_args("  rollout   california-cluster   service-1  ")
    assert tokens == ["rollout", "california-cluster", "service-1"]
    assert _arg(tokens, 0) == "rollout"
    assert _arg(tokens, 1) == "california-cluster"
    assert _arg(tokens, 5, default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# 2. Lazy Initialization & Missing Secrets
# ---------------------------------------------------------------------------


def test_build_slack_handler_missing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", None)
    monkeypatch.setattr(settings, "slack_signing_secret", None)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("HITL_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HITL_SLACK_SIGNING_SECRET", raising=False)

    handler = build_slack_handler()
    assert handler is None

    client = TestClient(app)
    resp = client.post("/slack/events", json={"type": "url_verification"})
    assert resp.status_code == 503
    assert "Slack integration is disabled" in resp.json()["detail"]


def test_build_slack_handler_partial_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-dummy-token")
    monkeypatch.setattr(settings, "slack_signing_secret", None)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("HITL_SLACK_SIGNING_SECRET", raising=False)

    handler = build_slack_handler()
    assert handler is None


# ---------------------------------------------------------------------------
# 3. Signature Verification Tests
# ---------------------------------------------------------------------------


def test_signature_verification_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "mock-test-signing-secret"
    token = "xoxb-test-valid-bot-token"

    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", token)
    monkeypatch.setattr(settings, "slack_signing_secret", secret)
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", secret)

    build_slack_handler()
    client = TestClient(app)

    payload = {"type": "url_verification", "challenge": "challenge-token-xyz"}
    body_bytes = json.dumps(payload).encode("utf-8")
    now_ts = str(int(time.time()))

    # A: Valid signature -> HTTP 200 with challenge
    valid_sig = _make_slack_signature(secret, body_bytes, now_ts)
    resp_valid = client.post(
        "/slack/events",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": now_ts,
            "X-Slack-Signature": valid_sig,
        },
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json() == {"challenge": "challenge-token-xyz"}

    # B: Invalid signature -> HTTP 401
    invalid_sig = "v0=invalid0000000000000000000000000000000000000000000000000000000000"
    resp_invalid = client.post(
        "/slack/events",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": now_ts,
            "X-Slack-Signature": invalid_sig,
        },
    )
    assert resp_invalid.status_code in (400, 401)


# ---------------------------------------------------------------------------
# 4. App Mention & Threaded Reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_mention_threaded_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = AsyncMock()

    event = {
        "type": "app_mention",
        "user": "U_ALICE",
        "text": "<@U_BOT> what changed in the latest rollout?",
        "ts": "1710000000.123456",
        "channel": "C_TEST_CHANNEL",
    }

    # Simulate async worker processing the event
    await _process_nl_event(event=event, client=mock_client)

    # Verify chat_postMessage was invoked with originating thread_ts
    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C_TEST_CHANNEL"
    assert call_kwargs["thread_ts"] == "1710000000.123456"
    assert (
        "Event:" in call_kwargs["text"]
        or "Workload:" in call_kwargs["text"]
        or "Revision:" in call_kwargs["text"]
    )


@pytest.mark.asyncio
async def test_app_mention_draft_ticket_renders_block_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = AsyncMock()

    # Pre-mock alert and violation resolution to avoid live monitoring calls
    from app.tools.alert import AlertPolicyMatch, IncidentViolation

    monkeypatch.setattr(
        "app.tools.jira_draft.resolve_alert",
        lambda _: AlertPolicyMatch(
            policy_id="policy-123",
            display_name="Alert Policy for healthcheck",
            target_endpoint="10.0.0.1:8080",
        ),
    )
    monkeypatch.setattr(
        "app.tools.jira_draft.get_latest_incident",
        lambda _: IncidentViolation(
            incident_id="violation-999",
            policy_id="policy-123",
            policy_name="Alert Policy for healthcheck",
            started_at="2026-10-30T10:00:00Z",
            state="OPEN",
            summary="Uptime check failed",
        ),
    )

    event = {
        "type": "app_mention",
        "user": "U_REQUESTER_1",
        "text": "<@U_BOT> file a JIRA ticket in response to healthcheck alert",
        "ts": "1710000000.999999",
        "channel": "C_OPS_CHANNEL",
    }

    await _process_nl_event(event=event, client=mock_client)

    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C_OPS_CHANNEL"
    assert call_kwargs["thread_ts"] == "1710000000.999999"

    # Verify Block Kit actions contain approve and cancel buttons
    blocks = call_kwargs.get("blocks", [])
    assert len(blocks) >= 2
    actions_block = [b for b in blocks if b.get("type") == "actions"][0]
    action_ids = [elem["action_id"] for elem in actions_block["elements"]]
    assert "hitl_approve_ticket" in action_ids
    assert "hitl_cancel_ticket" in action_ids


# ---------------------------------------------------------------------------
# 5. Requester-Only Authorization Gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requester_only_authorization_gate_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    get_replay_registry().reset()
    settings = get_settings()

    requester_user = "U_ORIGINAL_REQUESTER"
    attacker_user = "U_UNAUTHORIZED_PEER"

    # 1. Create a signed token bound to requester_user
    signed_token = create_signed_token(
        draft_id="draft-auth-test-1",
        incident_id="violation-auth-1",
        secret_key=settings.hmac_secret_key,
        requester_id=requester_user,
    )

    # Save draft into store
    runner.store.save_draft(
        "draft-auth-test-1",
        {
            "draft_id": "draft-auth-test-1",
            "incident_id": "violation-auth-1",
            "requester_id": requester_user,
            "project_key": "AMJTEST",
            "summary": "Test incident draft",
        },
    )

    mock_client = AsyncMock()
    mock_respond = AsyncMock()

    body = {
        "type": "block_actions",
        "user": {"id": attacker_user},
        "channel": {"id": "C_TEST"},
        "container": {
            "channel_id": "C_TEST",
            "message_ts": "1710000005.000000",
            "thread_ts": "1710000000.123456",
        },
        "actions": [{"action_id": "hitl_approve_ticket", "value": signed_token}],
    }

    # 2. Attacker attempts to click [Approve]
    await _process_approval_action(body=body, client=mock_client, respond=mock_respond)

    # Verify rejection message was sent (ephemeral or respond)
    expected_err_snippet = (
        f"Approval rejected: user '{attacker_user}' is not authorized to "
        f"approve draft created by '{requester_user}'"
    )
    if mock_client.chat_postEphemeral.called:
        ephemeral_text = mock_client.chat_postEphemeral.call_args.kwargs["text"]
        assert expected_err_snippet in ephemeral_text
    else:
        assert mock_respond.called
        respond_text = mock_respond.call_args.kwargs.get("text") or mock_respond.call_args.args[0]
        assert expected_err_snippet in respond_text

    # Verify draft remains UNCONSUMED in conversation store
    assert runner.store.get_draft("draft-auth-test-1") is not None


@pytest.mark.asyncio
async def test_requester_only_authorization_gate_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    get_replay_registry().reset()
    settings = get_settings()

    requester_user = "U_ORIGINAL_REQUESTER"

    # 1. Create token bound to requester_user
    signed_token = create_signed_token(
        draft_id="draft-auth-test-2",
        incident_id="violation-auth-2",
        secret_key=settings.hmac_secret_key,
        requester_id=requester_user,
    )

    runner.store.save_draft(
        "draft-auth-test-2",
        {
            "draft_id": "draft-auth-test-2",
            "incident_id": "violation-auth-2",
            "requester_id": requester_user,
            "project_key": "AMJTEST",
            "summary": "Test incident draft",
        },
    )

    mock_client = AsyncMock()
    mock_respond = AsyncMock()

    body = {
        "type": "block_actions",
        "user": {"id": requester_user},
        "channel": {"id": "C_TEST"},
        "container": {
            "channel_id": "C_TEST",
            "message_ts": "1710000005.000000",
            "thread_ts": "1710000000.123456",
        },
        "actions": [{"action_id": "hitl_approve_ticket", "value": signed_token}],
    }

    # 2. Original requester clicks [Approve]
    await _process_approval_action(body=body, client=mock_client, respond=mock_respond)

    # 3. Verify chat_update replaced message with confirmation
    mock_client.chat_update.assert_called_once()
    update_kwargs = mock_client.chat_update.call_args.kwargs
    assert "✅ Ticket AMJTEST-" in update_kwargs["text"]
    assert "Investigation running ⏳" in update_kwargs["text"]

    # 4. Verify thread mapping was recorded in store
    assert runner.store.get_ticket_thread("AMJTEST-20") == "C_TEST:1710000000.123456"


# ---------------------------------------------------------------------------
# 6. RCA Callback Delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_rca_to_slack_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-dummy")
    monkeypatch.setattr(settings, "slack_signing_secret", "dummy-secret")

    build_slack_handler()
    from app.adapters import slack as slack_mod

    assert slack_mod._slack_app is not None
    mock_post_message = AsyncMock()
    monkeypatch.setattr(slack_mod._slack_app.client, "chat_postMessage", mock_post_message)

    runner.store.record_ticket_thread("AMJTEST-88", "C_INCIDENTS:1710000099.555555")

    report_payload = {
        "executive_summary": "Pod failover cascade resolved by node restart",
        "root_cause": "Node OOM killed worker processes",
        "report_url": "https://storage.googleapis.com/reports/AMJTEST-88.html",
    }

    success = await deliver_rca_to_slack("AMJTEST-88", report_payload)
    assert success is True

    mock_post_message.assert_called_once()
    kwargs = mock_post_message.call_args.kwargs
    assert kwargs["channel"] == "C_INCIDENTS"
    assert kwargs["thread_ts"] == "1710000099.555555"
    assert "Root Cause Analysis (RCA) Completed for AMJTEST-88" in kwargs["text"]
    assert "Node OOM killed worker processes" in kwargs["text"]
    assert "https://storage.googleapis.com/reports/AMJTEST-88.html" in kwargs["text"]
