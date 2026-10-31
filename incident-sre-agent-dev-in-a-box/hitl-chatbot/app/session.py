"""In-memory ConversationStore and Draft Registry."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationMessage:
    """Individual message record in a conversation session."""

    def __init__(self, role: str, content: str, timestamp: Optional[float] = None) -> None:
        self.role = role
        self.content = content
        self.timestamp = timestamp if timestamp is not None else time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }


class ConversationSession:
    """Active conversational context for a user or channel thread."""

    def __init__(self, session_id: str, max_history: int = 20) -> None:
        self.session_id = session_id
        self.max_history = max_history
        self.messages: List[ConversationMessage] = []
        self.pending_drafts: Dict[str, Dict[str, Any]] = {}
        self.last_active_at: float = time.time()

    def add_message(self, role: str, content: str) -> None:
        """Append message and enforce sliding window capacity."""
        self.messages.append(ConversationMessage(role, content))
        if len(self.messages) > self.max_history:
            self.messages = self.messages[-self.max_history :]
        self.last_active_at = time.time()

    def get_history(self) -> List[Dict[str, Any]]:
        """Return history as list of dicts."""
        return [msg.to_dict() for msg in self.messages]

    def save_draft(self, draft_id: str, draft_data: Dict[str, Any]) -> None:
        """Store pending draft in session."""
        self.pending_drafts[draft_id] = draft_data
        self.last_active_at = time.time()

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve pending draft from session."""
        return self.pending_drafts.get(draft_id)

    def remove_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Remove and return pending draft."""
        return self.pending_drafts.pop(draft_id, None)


class ConversationStore:
    """Thread-safe in-memory store for chat sessions and ticket-to-thread mappings."""

    def __init__(self, ttl_seconds: int = 3600, max_history: int = 20) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_history = max_history
        self._sessions: Dict[str, ConversationSession] = {}
        self._ticket_to_thread: Dict[str, str] = {}
        self._global_drafts: Dict[str, Dict[str, Any]] = {}
        self._rca_reports: Dict[str, Dict[str, Any]] = {}
        self._active_tickets: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_or_create_session(self, session_id: str) -> ConversationSession:
        """Retrieve existing session or initialize a new one."""
        with self._lock:
            self._purge_expired_sessions()
            if session_id not in self._sessions:
                self._sessions[session_id] = ConversationSession(
                    session_id=session_id,
                    max_history=self.max_history,
                )
            return self._sessions[session_id]

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Add message to specified session."""
        session = self.get_or_create_session(session_id)
        session.add_message(role, content)

    def save_draft(
        self,
        draft_id: str,
        draft_data: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        """Save draft globally and in session if provided."""
        with self._lock:
            self._global_drafts[draft_id] = draft_data
            if session_id:
                if session_id not in self._sessions:
                    self._sessions[session_id] = ConversationSession(
                        session_id=session_id,
                        max_history=self.max_history,
                    )
                self._sessions[session_id].save_draft(draft_id, draft_data)

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Get draft by draft_id."""
        with self._lock:
            return self._global_drafts.get(draft_id)

    def remove_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Remove draft by draft_id."""
        with self._lock:
            draft = self._global_drafts.pop(draft_id, None)
            for sess in self._sessions.values():
                sess.remove_draft(draft_id)
            return draft

    def record_ticket_thread(self, ticket_key: str, thread_ref: str) -> None:
        """Associate created Jira ticket key with conversation thread reference."""
        with self._lock:
            self._ticket_to_thread[ticket_key] = thread_ref

    def get_ticket_thread(self, ticket_key: str) -> Optional[str]:
        """Look up thread reference for a ticket key."""
        with self._lock:
            return self._ticket_to_thread.get(ticket_key)

    def _purge_expired_sessions(self) -> None:
        """Evict sessions inactive longer than ttl_seconds."""
        now = time.time()
        expired = [
            sid
            for sid, sess in self._sessions.items()
            if now - sess.last_active_at > self.ttl_seconds
        ]
        for sid in expired:
            del self._sessions[sid]

    def record_rca_report(self, ticket_key: str, report: Dict[str, Any]) -> None:
        """Store RCA report for a ticket key."""
        with self._lock:
            self._rca_reports[ticket_key] = report

    def get_rca_report(self, ticket_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored RCA report for a ticket key."""
        with self._lock:
            return self._rca_reports.get(ticket_key)

    def set_active_ticket(
        self,
        thread_id: str,
        ticket_key: str,
        incident_id: Optional[str] = None,
        policy_name: Optional[str] = None,
        ticket_url: Optional[str] = None,
        rca_status: str = "pending",
        rca_content: Optional[Dict[str, Any]] = None,
        rca_attached: bool = False,
    ) -> Dict[str, Any]:
        """Record or update active ticket context for a thread."""
        with self._lock:
            context = {
                "ticket_key": ticket_key,
                "incident_id": incident_id,
                "policy_name": policy_name,
                "rca_status": rca_status,
                "rca_content": rca_content,
                "rca_attached": rca_attached,
                "ticket_url": ticket_url,
                "created_at": time.time(),
            }
            self._active_tickets[thread_id] = context
            self._ticket_to_thread[ticket_key] = thread_id
            return context

    def get_active_ticket(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve active ticket context for a thread if present."""
        with self._lock:
            return self._active_tickets.get(thread_id)

    def update_active_ticket_rca(
        self,
        ticket_key: str,
        rca_content: Dict[str, Any],
        thread_id: Optional[str] = None,
    ) -> None:
        """Update active ticket context with delivered RCA report."""
        with self._lock:
            target_thread = thread_id or self._ticket_to_thread.get(ticket_key)
            if target_thread and target_thread in self._active_tickets:
                ctx = self._active_tickets[target_thread]
                ctx["rca_status"] = "ready"
                ctx["rca_content"] = rca_content
            else:
                for tid, ctx in self._active_tickets.items():
                    if ctx.get("ticket_key") == ticket_key:
                        ctx["rca_status"] = "ready"
                        ctx["rca_content"] = rca_content

    def mark_rca_attached(
        self,
        thread_id: str,
        ticket_key: Optional[str] = None,
    ) -> None:
        """Mark RCA as attached to the active ticket in thread."""
        with self._lock:
            if thread_id in self._active_tickets:
                ctx = self._active_tickets[thread_id]
                ctx["rca_attached"] = True
                ctx["rca_status"] = "attached"
            elif ticket_key:
                for tid, ctx in self._active_tickets.items():
                    if ctx.get("ticket_key") == ticket_key:
                        ctx["rca_attached"] = True
                        ctx["rca_status"] = "attached"

    def clear(self) -> None:
        """Reset all in-memory store state."""
        with self._lock:
            self._sessions.clear()
            self._ticket_to_thread.clear()
            self._global_drafts.clear()
            self._rca_reports.clear()
            self._active_tickets.clear()


# Global in-memory conversation store
_global_store = ConversationStore()


def get_conversation_store() -> ConversationStore:
    """Get the active in-memory conversation store."""
    return _global_store
