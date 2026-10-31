"""Scenario tests for AMJTEST-27: Intent routing, active ticket context, and duplicate guard."""

from __future__ import annotations

import pytest

from app.agent import ChatbotAgentRunner, ChatbotIntent, classify_intent
from app.session import get_conversation_store
from app.tools.alert import resolve_alert


def test_classify_intent_update_ticket_precedence() -> None:
    """UPDATE_TICKET is evaluated before create path and never treats sentence as alert name."""
    queries = [
        "Update the ticket with your RCA",
        "update ticket with rca",
        "Please attach RCA to the ticket",
        "add rca to the issue",
        "comment on the ticket",
        "attach RCA",
    ]
    for q in queries:
        intent, conf = classify_intent(q)
        assert intent == ChatbotIntent.UPDATE_TICKET
        assert conf == 1.0


def test_classify_intent_distinct_paths() -> None:
    """Verify distinct intent paths for create, deployments, and ambiguous queries."""
    # Deployments
    intent, _ = classify_intent("what changed in the latest rollout?")
    assert intent == ChatbotIntent.LOOKUP_DEPLOYMENTS

    # Explicit ticket filing
    intent, _ = classify_intent("file a JIRA ticket for snow-ip alert")
    assert intent == ChatbotIntent.CREATE_TICKET

    intent, _ = classify_intent("create incident ticket in response to alert")
    assert intent == ChatbotIntent.CREATE_TICKET

    # Ambiguous prompt
    intent, _ = classify_intent("help with alert")
    assert intent == ChatbotIntent.CLARIFICATION


def test_ambiguous_phrases_do_not_match_alert_policies() -> None:
    """Ambiguous phrases without matching tokens must not guess or match alert policies."""
    ambiguous_queries = [
        "Update the ticket with your RCA",
        "something is broken in production",
        "general database error",
        "check the system",
    ]
    for q in ambiguous_queries:
        match = resolve_alert(q)
        assert match.match_confidence < 0.5
        assert match.policy_id == ""


def test_amjtest27_update_intent_does_not_create_ticket_or_match_snow_policy() -> None:
    """AMJTEST-27 Scenario:

    In a thread with an existing active ticket, receiving 'Update the ticket with your RCA':
      - MUST NOT create a new ticket.
      - MUST NOT resolve to california-snow-image-update (or any other policy).
      - MUST recognize update intent and attach RCA or report already attached.
    """
    store = get_conversation_store()
    runner = ChatbotAgentRunner()
    thread_id = "thread-amjtest-27"

    # Set up active ticket AMJTEST-27 with RCA ready
    store.set_active_ticket(
        thread_id=thread_id,
        ticket_key="AMJTEST-27",
        incident_id="0.test-inc-12345",
        policy_name="snow-ip-healthcheck-3",
        ticket_url="https://jira.example.internal/browse/AMJTEST-27",
        rca_status="ready",
        rca_content={
            "root_cause": "Replica scale-down event triggered 503 errors.",
            "confidence": 0.85,
        },
        rca_attached=False,
    )

    turn = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=thread_id,
        user_id="U12345",
    )

    # 1. Must NOT be a new ticket draft
    assert turn["type"] == "draft_comment"
    assert "draft_ticket" not in turn["type"]

    # 2. Must NOT match any alert policy name
    assert "california-snow-image-update" not in turn["text"]
    assert "snow-ip-healthcheck-3" not in turn["text"]

    # 3. Must be targeted to AMJTEST-27
    assert "AMJTEST-27" in turn["text"]
    assert "Signed Approval Token:" in turn["text"]
    assert turn.get("signed_token") is not None


def test_amjtest27_rca_already_attached_reports_link() -> None:
    """If RCA is already attached to AMJTEST-27, reply immediately without duplicate draft."""
    store = get_conversation_store()
    runner = ChatbotAgentRunner()
    thread_id = "thread-amjtest-27-attached"

    store.set_active_ticket(
        thread_id=thread_id,
        ticket_key="AMJTEST-27",
        incident_id="0.test-inc-12345",
        policy_name="snow-ip-healthcheck-3",
        ticket_url="https://jira.example.internal/browse/AMJTEST-27",
        rca_status="attached",
        rca_attached=True,
    )

    turn = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=thread_id,
        user_id="U12345",
    )

    assert turn["type"] == "info"
    assert "RCA is already attached to AMJTEST-27" in turn["text"]
    assert "https://jira.example.internal/browse/AMJTEST-27" in turn["text"]
    assert "signed_token" not in turn


def test_amjtest27_no_active_ticket_asks_for_ticket_key() -> None:
    """If thread has NO active ticket, reply asking which ticket key to update."""
    runner = ChatbotAgentRunner()
    thread_id = "thread-empty"

    turn = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=thread_id,
    )

    assert turn["type"] == "info"
    assert "no active ticket in this thread" in turn["text"].lower()


def test_amjtest27_signed_requester_approval_for_comment() -> None:
    """Signed requester-only approval gate works for comment action."""
    store = get_conversation_store()
    runner = ChatbotAgentRunner()
    thread_id = "thread-amjtest-27-auth"

    store.set_active_ticket(
        thread_id=thread_id,
        ticket_key="AMJTEST-27",
        incident_id="0.test-inc-12345",
        policy_name="snow-ip-healthcheck-3",
        ticket_url="https://jira.example.internal/browse/AMJTEST-27",
        rca_status="ready",
        rca_content={"root_cause": "Configuration drift"},
        rca_attached=False,
    )

    # Requester U12345 requests comment draft
    turn_1 = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=thread_id,
        user_id="U12345",
    )
    signed_token = turn_1["signed_token"]

    # Unauthorized approver U99999 is rejected
    with pytest.raises(ValueError, match="not authorized"):
        runner.execute_approval(signed_token, session_id=thread_id, approver_id="U99999")

    # Authorized requester U12345 approves successfully
    res = runner.execute_approval(signed_token, session_id=thread_id, approver_id="U12345")
    assert res["type"] == "comment_created"
    assert "AMJTEST-27" in res["text"]

    # Verify active ticket marked rca_attached = True
    active_ticket = store.get_active_ticket(thread_id)
    assert active_ticket is not None
    assert active_ticket["rca_attached"] is True

    # Subsequent turn recognizes already attached
    turn_subsequent = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=thread_id,
        user_id="U12345",
    )
    assert "RCA is already attached to AMJTEST-27" in turn_subsequent["text"]


def test_duplicate_guard_warns_on_draft_card() -> None:
    """If user requests to create a ticket in a thread with an active ticket, warn on card."""
    store = get_conversation_store()
    runner = ChatbotAgentRunner()
    thread_id = "thread-active-dup-guard"

    store.set_active_ticket(
        thread_id=thread_id,
        ticket_key="AMJTEST-20",
        incident_id="0.test-inc-12345",
        policy_name="snow-ip-healthcheck-3",
        ticket_url="https://jira.example.internal/browse/AMJTEST-20",
    )

    turn = runner.process_turn(
        user_message="file a JIRA ticket in response to the latest ip-health-snow alert",
        session_id=thread_id,
        user_id="U12345",
    )

    assert turn["type"] == "draft_ticket"
    assert "Notice: A ticket (AMJTEST-20) is already active in this thread" in turn["text"]
    assert "Did you mean to update AMJTEST-20 instead?" in turn["text"]


def test_amjtest27_ticket_created_callback_arrives_update_replies_attached() -> None:
    """Regression test:
    ticket created in thread -> callback arrives -> update ticket with RCA
    -> replies RCA already attached to <KEY>.

    Also verifies thread key lookup handling both '{channel}:{thread_ts}' and '{thread_ts}'.
    """
    store = get_conversation_store()
    runner = ChatbotAgentRunner()
    channel = "C012345"
    thread_ts = "1720001122.334455"
    composite_thread_id = f"{channel}:{thread_ts}"

    # 1. Ticket created in thread (composite_thread_id)
    store.set_active_ticket(
        thread_id=composite_thread_id,
        ticket_key="AMJTEST-27",
        incident_id="0.test-inc-12345",
        policy_name="snow-ip-healthcheck-3",
        ticket_url="https://jira.example.internal/browse/AMJTEST-27",
        rca_status="pending",
        rca_attached=False,
    )

    # Verify active ticket is present before callback
    ctx_before = store.get_active_ticket(composite_thread_id)
    assert ctx_before is not None
    assert ctx_before["rca_attached"] is False
    assert ctx_before["rca_status"] == "pending"

    # Also verify lookup via thread_ts alone finds it
    assert store.get_active_ticket(thread_ts) is not None

    # 2. Bridge delivers RCA via callback (using thread_ts or composite)
    rca_report = {
        "root_cause": "Replica scale-down event triggered 503 errors.",
        "confidence": 0.95,
    }
    store.update_active_ticket_rca("AMJTEST-27", rca_report, thread_id=thread_ts)

    # Verify active ticket now has rca_attached = True and rca_status = 'attached'
    ctx_after = store.get_active_ticket(composite_thread_id)
    assert ctx_after is not None
    assert ctx_after["rca_attached"] is True
    assert ctx_after["rca_status"] == "attached"
    assert ctx_after["rca_content"] == rca_report

    # 3. User asks: "Update the ticket with your RCA"
    turn = runner.process_turn(
        user_message="Update the ticket with your RCA",
        session_id=composite_thread_id,
        user_id="U12345",
    )

    # 4. Chatbot replies that RCA is already attached to AMJTEST-27
    assert turn["type"] == "info"
    assert "RCA is already attached to AMJTEST-27" in turn["text"]
    assert "https://jira.example.internal/browse/AMJTEST-27" in turn["text"]
    assert "signed_token" not in turn
