"""Thread-safe in-memory TTL deduplication registry for bridge callbacks."""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple


class CallbackDedupRegistry:
    """Thread-safe in-memory TTL deduplication registry for incident callback reports.

    Tracks processed (incident_id, status) tuples to prevent duplicate ticket
    writebacks upon agent retries or redundant delivery.
    """

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str], float] = {}

    def _evict_expired(self, current_time: float) -> None:
        """Evict expired entries. Must be called while holding _lock."""
        expired = [
            k
            for k, timestamp in self._entries.items()
            if current_time - timestamp > self.ttl_seconds
        ]
        for k in expired:
            del self._entries[k]

    def check_and_mark(self, incident_id: str, status: str) -> bool:
        """Check if (incident_id, status) has been processed.

        Returns True if new and recorded, or False if duplicate.
        """
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            key = (incident_id, status)
            if key in self._entries:
                return False
            self._entries[key] = now
            return True

    def release(self, incident_id: str, status: str) -> None:
        """Forget (incident_id, status) so a retry after a failed delivery is processed."""
        with self._lock:
            self._entries.pop((incident_id, status), None)

    def clear(self) -> None:
        """Clear all entries (e.g. for testing)."""
        with self._lock:
            self._entries.clear()

    def reset(self) -> None:
        """Alias for clear()."""
        self.clear()


# Default singleton registry
_default_registry = CallbackDedupRegistry()


def get_dedup_registry() -> CallbackDedupRegistry:
    """Get the global callback deduplication registry."""
    return _default_registry


def clear() -> None:
    """Clear the default global deduplication registry."""
    _default_registry.clear()


def reset() -> None:
    """Alias for clear()."""
    _default_registry.reset()
