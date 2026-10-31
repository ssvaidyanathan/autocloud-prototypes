"""HMAC-SHA256 token generation, verification, and single-use replay protection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class TokenReplayRegistry:
    """Thread-safe in-memory store for tracking consumed token signatures with TTL eviction."""

    def __init__(self) -> None:
        self._used_signatures: Dict[str, int] = {}
        self._lock = threading.Lock()

    def prune_expired(self, now: Optional[int] = None) -> int:
        """Prune expired token signatures. Returns count of pruned entries."""
        current_time = now if now is not None else int(time.time())
        with self._lock:
            expired = [sig for sig, exp in self._used_signatures.items() if exp < current_time]
            for sig in expired:
                del self._used_signatures[sig]
            return len(expired)

    def is_used(self, signature: str) -> bool:
        """Check if a token signature has already been consumed (prunes expired entries first)."""
        self.prune_expired()
        with self._lock:
            return signature in self._used_signatures

    def mark_used(
        self,
        signature: str,
        exp: Optional[int] = None,
        ttl_seconds: int = 86400,
    ) -> bool:
        """Mark a signature as consumed. Returns True if newly marked, False if already consumed."""
        self.prune_expired()
        expiry = exp if exp is not None else (int(time.time()) + ttl_seconds)
        with self._lock:
            if signature in self._used_signatures:
                return False
            self._used_signatures[signature] = expiry
            return True

    def reset(self) -> None:
        """Clear all consumed signatures (primarily for test isolation)."""
        with self._lock:
            self._used_signatures.clear()


# Global in-memory replay registry
_global_replay_registry = TokenReplayRegistry()


def get_replay_registry() -> TokenReplayRegistry:
    """Get the active token replay registry."""
    return _global_replay_registry


def compute_signature(
    draft_id: str,
    incident_id: str,
    exp: int,
    secret_key: str,
    requester_id: Optional[str] = None,
) -> str:
    """Compute HMAC-SHA256 hex digest for token contents."""
    message = f"{draft_id}:{incident_id}:{exp}:{requester_id or ''}".encode("utf-8")
    key = secret_key.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def create_signed_token(
    draft_id: str,
    incident_id: str,
    secret_key: str,
    ttl_seconds: int = 600,
    requester_id: Optional[str] = None,
) -> str:
    """Generate a base64url-encoded signed approval token."""
    exp = int(time.time()) + ttl_seconds
    sig = compute_signature(
        draft_id,
        incident_id,
        exp,
        secret_key,
        requester_id=requester_id,
    )
    payload: Dict[str, Any] = {
        "draft_id": draft_id,
        "incident_id": incident_id,
        "exp": exp,
        "sig": sig,
    }
    if requester_id is not None:
        payload["requester_id"] = requester_id
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(json_bytes).decode("ascii").rstrip("=")


def verify_signed_token(
    token_str: str,
    secret_key: str,
    registry: Optional[TokenReplayRegistry] = None,
    approver_id: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate token format, signature, expiration, replay, and requester authorization."""
    replay_registry = registry or get_replay_registry()

    if not token_str or not isinstance(token_str, str):
        return False, "Token is empty or invalid type", {}

    # Pad base64 string if necessary
    padded = token_str + "=" * (-len(token_str) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        return False, f"Malformed token encoding: {exc}", {}

    draft_id = payload.get("draft_id")
    incident_id = payload.get("incident_id")
    exp = payload.get("exp")
    sig = payload.get("sig")
    requester_id = payload.get("requester_id")

    if not draft_id or not incident_id or not exp or not sig:
        return False, "Token payload missing required fields", {}

    if not isinstance(exp, int):
        return False, "Expiration timestamp must be an integer", {}

    # Check expiration
    now = int(time.time())
    if exp < now:
        return False, f"Token expired at {exp} (current time {now})", payload

    # Verify HMAC signature
    expected_sig = compute_signature(
        str(draft_id),
        str(incident_id),
        exp,
        secret_key,
        requester_id=requester_id,
    )
    if not hmac.compare_digest(str(sig), expected_sig):
        return False, "Invalid signature: token has been tampered with", payload

    # Check single-use replay
    if replay_registry.is_used(str(sig)):
        return False, "Token has already been used (single-use replay detected)", payload

    # Enforce requester identity binding
    if requester_id is not None:
        if approver_id is None or not hmac.compare_digest(str(approver_id), str(requester_id)):
            err_msg = (
                f"Approval rejected: user '{approver_id}' is not authorized to "
                f"approve draft created by '{requester_id}'"
            )
            return False, err_msg, payload

    return True, "", payload


def consume_signed_token(
    token_str: str,
    secret_key: str,
    registry: Optional[TokenReplayRegistry] = None,
    approver_id: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Verify and atomically mark the token as consumed."""
    replay_registry = registry or get_replay_registry()
    is_valid, error_msg, payload = verify_signed_token(
        token_str, secret_key, replay_registry, approver_id=approver_id
    )
    if not is_valid:
        return False, error_msg, payload

    sig = str(payload["sig"])
    exp = payload.get("exp")
    exp_int = exp if isinstance(exp, int) else None
    if not replay_registry.mark_used(sig, exp=exp_int):
        return False, "Token has already been used (single-use replay detected)", payload

    return True, "", payload
