"""Thread-safe in-memory metadata registry for HITL tickets and investigations."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class TicketMetadata(BaseModel):
    """Metadata associated with an incident ticket and its background investigation."""

    issue_key: str
    summary: str
    status: str = "created"  # created, investigating, completed, failed
    callback_url: Optional[str] = None
    slack_thread: Optional[str] = None
    alert_started_at: Optional[str] = None
    report: Optional[dict[str, Any]] = None
    created_at: float = Field(default_factory=time.monotonic)


class TicketMetadataRegistry:
    """Thread-safe registry for caching ticket metadata across HITL lifecycle events."""

    def __init__(self, ttl_seconds: float = 86400.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: Dict[str, TicketMetadata] = {}

    def _normalize_key(self, key: str) -> str:
        k = key.strip()
        return k

    def _evict_expired(self, current_time: float) -> None:
        expired = [
            k
            for k, meta in self._entries.items()
            if current_time - meta.created_at > self.ttl_seconds
        ]
        for k in expired:
            del self._entries[k]

    def set(
        self,
        issue_key: str,
        summary: str,
        status: str = "created",
        callback_url: Optional[str] = None,
        slack_thread: Optional[str] = None,
        alert_started_at: Optional[str] = None,
        report: Optional[dict[str, Any]] = None,
    ) -> TicketMetadata:
        norm_key = self._normalize_key(issue_key)
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            entry = TicketMetadata(
                issue_key=norm_key,
                summary=summary,
                status=status,
                callback_url=callback_url,
                slack_thread=slack_thread,
                alert_started_at=alert_started_at,
                report=report,
                created_at=now,
            )
            self._entries[norm_key] = entry
            # Also register alias if formatted as jira-KEY or vice-versa
            if norm_key.lower().startswith("jira-"):
                self._entries[norm_key[5:]] = entry
            else:
                self._entries[f"jira-{norm_key}"] = entry
            return entry

    def get(self, issue_key: str) -> Optional[TicketMetadata]:
        norm_key = self._normalize_key(issue_key)
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            if norm_key in self._entries:
                return self._entries[norm_key]
            if norm_key.lower().startswith("jira-"):
                return self._entries.get(norm_key[5:])
            return self._entries.get(f"jira-{norm_key}")

    def update_status(
        self,
        issue_key: str,
        status: str,
        summary: Optional[str] = None,
        report: Optional[dict[str, Any]] = None,
    ) -> Optional[TicketMetadata]:
        entry = self.get(issue_key)
        if entry is None:
            return None
        with self._lock:
            entry.status = status
            if summary:
                entry.summary = summary
            if report is not None:
                entry.report = report
        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def reset(self) -> None:
        self.clear()


_default_ticket_registry = TicketMetadataRegistry()


def get_ticket_registry() -> TicketMetadataRegistry:
    """Get singleton ticket metadata registry."""
    return _default_ticket_registry


def reset_ticket_registry() -> None:
    """Reset singleton ticket metadata registry."""
    _default_ticket_registry.reset()
