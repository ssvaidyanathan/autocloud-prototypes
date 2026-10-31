"""Tests for HMAC-SHA256 token generation, verification, and single-use replay protection."""

from __future__ import annotations

import base64
import json
import time

from app.tokens import (
    consume_signed_token,
    create_signed_token,
    get_replay_registry,
    verify_signed_token,
)


def test_create_and_verify_token_success() -> None:
    secret = "my-super-secret-key-12345"
    token = create_signed_token(
        draft_id="draft-001",
        incident_id="inc-999",
        secret_key=secret,
        ttl_seconds=300,
    )
    assert isinstance(token, str)
    assert len(token) > 20

    is_valid, err, payload = verify_signed_token(token, secret)
    assert is_valid is True
    assert err == ""
    assert payload["draft_id"] == "draft-001"
    assert payload["incident_id"] == "inc-999"
    assert payload["exp"] > int(time.time())


def test_token_tampered_payload_rejected() -> None:
    secret = "my-super-secret-key-12345"
    token = create_signed_token(
        draft_id="draft-001",
        incident_id="inc-999",
        secret_key=secret,
        ttl_seconds=300,
    )

    # Decode, modify incident_id, and re-encode with original signature
    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    payload["incident_id"] = "inc-tampered-666"

    tampered_bytes = json.dumps(payload).encode("utf-8")
    tampered_token = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")

    is_valid, err, _ = verify_signed_token(tampered_token, secret)
    assert is_valid is False
    assert "tampered" in err.lower() or "invalid signature" in err.lower()


def test_token_tampered_signature_rejected() -> None:
    secret = "my-super-secret-key-12345"
    token = create_signed_token(
        draft_id="draft-001",
        incident_id="inc-999",
        secret_key=secret,
        ttl_seconds=300,
    )

    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    payload["sig"] = "deadbeef" * 8

    tampered_bytes = json.dumps(payload).encode("utf-8")
    tampered_token = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")

    is_valid, err, _ = verify_signed_token(tampered_token, secret)
    assert is_valid is False
    assert "tampered" in err.lower() or "invalid signature" in err.lower()


def test_token_expired_rejected() -> None:
    secret = "my-super-secret-key-12345"
    # Create token with negative TTL
    token = create_signed_token(
        draft_id="draft-001",
        incident_id="inc-999",
        secret_key=secret,
        ttl_seconds=-10,
    )

    is_valid, err, _ = verify_signed_token(token, secret)
    assert is_valid is False
    assert "expired" in err.lower()


def test_token_single_use_replay_prevention() -> None:
    secret = "my-super-secret-key-12345"
    registry = get_replay_registry()
    token = create_signed_token(
        draft_id="draft-001",
        incident_id="inc-999",
        secret_key=secret,
        ttl_seconds=300,
    )

    # First consumption succeeds
    is_valid_1, err_1, payload_1 = consume_signed_token(token, secret, registry=registry)
    assert is_valid_1 is True
    assert err_1 == ""
    assert payload_1["draft_id"] == "draft-001"

    # Second consumption fails with replay detection
    is_valid_2, err_2, _ = consume_signed_token(token, secret, registry=registry)
    assert is_valid_2 is False
    assert "replay" in err_2.lower() or "already been used" in err_2.lower()


def test_wrong_secret_key_rejected() -> None:
    secret_a = "mock-secret-alpha"
    secret_b = "mock-secret-bravo"
    token = create_signed_token("draft-001", "inc-001", secret_a, 300)

    is_valid, err, _ = verify_signed_token(token, secret_b)
    assert is_valid is False
    assert "tampered" in err.lower() or "invalid signature" in err.lower()


def test_malformed_token_rejected() -> None:
    is_valid, err, _ = verify_signed_token("not-a-valid-token-string", "secret")
    assert is_valid is False
    assert "malformed" in err.lower() or "missing" in err.lower()


def test_replay_registry_ttl_eviction() -> None:
    """Verify expired token signatures are pruned from TokenReplayRegistry."""
    from app.tokens import TokenReplayRegistry

    reg = TokenReplayRegistry()
    now = int(time.time())

    # Add an expired signature (expired 10 seconds ago)
    reg.mark_used("sig-expired", exp=now - 10)
    # Add a valid signature (expires in 300 seconds)
    reg.mark_used("sig-valid", exp=now + 300)

    # Calling is_used on sig-expired automatically evicts it
    assert not reg.is_used("sig-expired")
    assert reg.is_used("sig-valid")


def test_token_creation_with_requester_id() -> None:
    secret = "my-super-secret-key-12345"
    token = create_signed_token(
        draft_id="draft-002",
        incident_id="inc-100",
        secret_key=secret,
        ttl_seconds=300,
        requester_id="U12345678",
    )
    is_valid, err, payload = verify_signed_token(token, secret, approver_id="U12345678")
    assert is_valid is True
    assert err == ""
    assert payload["requester_id"] == "U12345678"
    assert payload["draft_id"] == "draft-002"


def test_same_user_approval_succeeds() -> None:
    secret = "my-super-secret-key-12345"
    requester = "U_ALICE"
    token = create_signed_token(
        draft_id="draft-003",
        incident_id="inc-101",
        secret_key=secret,
        requester_id=requester,
    )
    is_valid, err, payload = verify_signed_token(token, secret, approver_id=requester)
    assert is_valid is True
    assert err == ""
    assert payload["requester_id"] == requester


def test_different_user_approval_rejected() -> None:
    secret = "my-super-secret-key-12345"
    requester = "U_ALICE"
    approver = "U_BOB"
    token = create_signed_token(
        draft_id="draft-004",
        incident_id="inc-102",
        secret_key=secret,
        requester_id=requester,
    )

    # 1. Different user rejected
    is_valid, err, _ = verify_signed_token(token, secret, approver_id=approver)
    assert is_valid is False
    assert f"user '{approver}' is not authorized" in err
    assert f"draft created by '{requester}'" in err

    # 2. None approver rejected when requester is set
    is_valid_none, err_none, _ = verify_signed_token(token, secret, approver_id=None)
    assert is_valid_none is False
    assert "not authorized" in err_none


def test_tampered_requester_id_rejected_with_signature_mismatch() -> None:
    secret = "my-super-secret-key-12345"
    token = create_signed_token(
        draft_id="draft-005",
        incident_id="inc-103",
        secret_key=secret,
        requester_id="U_ALICE",
    )

    # Decode, modify requester_id, and re-encode without updating sig
    padded = token + "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    payload["requester_id"] = "U_BOB"

    tampered_bytes = json.dumps(payload).encode("utf-8")
    tampered_token = base64.urlsafe_b64encode(tampered_bytes).decode("ascii").rstrip("=")

    # Even if bob tries to approve with modified requester_id, signature check fails
    is_valid, err, _ = verify_signed_token(tampered_token, secret, approver_id="U_BOB")
    assert is_valid is False
    assert "invalid signature" in err.lower() or "tampered" in err.lower()


def test_consume_signed_token_enforces_authorization() -> None:
    secret = "my-super-secret-key-12345"
    registry = get_replay_registry()
    token = create_signed_token(
        draft_id="draft-006",
        incident_id="inc-104",
        secret_key=secret,
        requester_id="U_ALICE",
    )

    # Unauthorized consumption fails and does not consume the token
    is_valid_bad, err_bad, _ = consume_signed_token(
        token, secret, registry=registry, approver_id="U_BOB"
    )
    assert is_valid_bad is False
    assert "not authorized" in err_bad

    # Legitimate requester can still consume the token
    is_valid_good, err_good, _ = consume_signed_token(
        token, secret, registry=registry, approver_id="U_ALICE"
    )
    assert is_valid_good is True
    assert err_good == ""

    # Replay is blocked
    is_valid_replay, err_replay, _ = consume_signed_token(
        token, secret, registry=registry, approver_id="U_ALICE"
    )
    assert is_valid_replay is False
    assert "already been used" in err_replay.lower() or "replay" in err_replay.lower()
