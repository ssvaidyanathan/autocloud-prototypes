"""Tests for in-memory ConversationStore and Draft Registry."""

from __future__ import annotations

import time

from app.session import ConversationStore


def test_add_message_and_sliding_window() -> None:
    store = ConversationStore(max_history=3)
    session_id = "sess-window-test"

    store.add_message(session_id, "user", "msg 1")
    store.add_message(session_id, "assistant", "msg 2")
    store.add_message(session_id, "user", "msg 3")
    store.add_message(session_id, "assistant", "msg 4")

    session = store.get_or_create_session(session_id)
    history = session.get_history()

    assert len(history) == 3
    assert history[0]["content"] == "msg 2"
    assert history[1]["content"] == "msg 3"
    assert history[2]["content"] == "msg 4"


def test_pending_draft_storage_and_removal() -> None:
    store = ConversationStore()
    draft_id = "draft-xyz-123"
    draft_payload = {"summary": "Test Incident", "priority": "P2"}

    store.save_draft(draft_id, draft_payload, session_id="sess-draft")
    assert store.get_draft(draft_id) == draft_payload

    session = store.get_or_create_session("sess-draft")
    assert session.get_draft(draft_id) == draft_payload

    removed = store.remove_draft(draft_id)
    assert removed == draft_payload
    assert store.get_draft(draft_id) is None
    assert session.get_draft(draft_id) is None


def test_ticket_to_thread_mapping() -> None:
    store = ConversationStore()
    ticket_key = "AMJTEST-100"
    thread_ref = "C08ABCDEF:1710000000.123456"

    store.record_ticket_thread(ticket_key, thread_ref)
    assert store.get_ticket_thread(ticket_key) == thread_ref
    assert store.get_ticket_thread("NONEXISTENT") is None


def test_session_expiry_purge() -> None:
    store = ConversationStore(ttl_seconds=1)
    session_id = "sess-ephemeral"

    store.add_message(session_id, "user", "hello")
    sess = store.get_or_create_session(session_id)
    # Artificially age the session
    sess.last_active_at = time.time() - 10

    # Next access triggers purge
    store.get_or_create_session("sess-fresh")
    assert session_id not in store._sessions
