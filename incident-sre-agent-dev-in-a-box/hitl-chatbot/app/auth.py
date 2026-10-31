"""Google OIDC authentication and authorization module for HITL Chatbot."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

from fastapi import HTTPException, Request, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_AUTHORIZED_PREFIXES = {
    "hitl-chatbot-sa",
    "dib-bridge-sa",
    "dib-agent-sa",
    "scion-fde-ops",
}

DEFAULT_BRIDGE_PREFIXES = {
    "dib-bridge-sa",
}


def _is_local_dev_env(settings: Settings) -> bool:
    """Check if environment or settings specify local development/testing."""
    env_str = settings.app_env.strip().lower()
    if env_str in ("local", "dev", "development", "test", "testing"):
        return True
    os_env = os.environ.get("APP_ENV", "").strip().lower()
    if os_env in ("local", "dev", "development", "test", "testing"):
        return True
    return False


def _decode_jwt_unverified(token: str) -> dict[str, Any]:
    """Decode JWT payload without cryptographic verification for local testing."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format: expected 3 dot-separated segments")
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += "=" * (4 - rem)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    data = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JWT payload is not a JSON dictionary")
    return data


def create_mock_oidc_token(email: str, audience: Optional[str] = None) -> str:
    """Generate a mock unsigned JWT for local test execution."""
    header = {"alg": "none", "typ": "JWT"}
    payload: dict[str, Any] = {
        "email": email,
        "iss": "https://accounts.google.com",
    }
    if audience:
        payload["aud"] = audience
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{h_b64}.{p_b64}.mock_signature"


def is_authorized_api_caller(email: str, settings: Settings) -> bool:
    """Check if caller email is authorized to call /api/* endpoints."""
    if not email:
        return False
    configured = settings.get_authorized_caller_accounts()
    if email in configured:
        return True
    prefix = email.split("@")[0]
    if prefix in configured:
        return True
    if prefix in DEFAULT_AUTHORIZED_PREFIXES:
        return True
    return False


def is_authorized_bridge_caller(email: str, settings: Settings) -> bool:
    """Check if caller email is authorized to invoke /internal/rca-callback."""
    if not email:
        return False
    configured = settings.get_bridge_caller_accounts()
    if email in configured:
        return True
    prefix = email.split("@")[0]
    if prefix in configured:
        return True
    if prefix in DEFAULT_BRIDGE_PREFIXES:
        return True
    return False


def verify_oidc_token(
    token: str,
    expected_audience: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Verify Google OIDC ID token against Google certs, with local fallback."""
    cfg = settings or get_settings()

    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: empty Bearer token",
        )

    # Local test fallback path
    if _is_local_dev_env(cfg):
        if token == "mock-oidc-token-for-testing":
            return {
                "email": "hitl-chatbot-sa@test.iam.gserviceaccount.com",
                "aud": expected_audience or "mock-aud",
            }
        try:
            return _decode_jwt_unverified(token)
        except Exception:
            pass  # Fall through to standard verification or error below

    # Production cryptographic verification against Google public keys
    try:
        req = google_requests.Request()
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token,
            req,
            audience=expected_audience or None,
        )
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid token claims format",
            )
        return claims
    except HTTPException:
        raise
    except Exception as exc:
        if _is_local_dev_env(cfg):
            try:
                return _decode_jwt_unverified(token)
            except Exception:
                pass
        logger.warning("OIDC token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unauthorized: invalid ID token ({exc})",
        ) from exc


def _extract_bearer_token(request: Request) -> str:
    """Extract token from Authorization header or raise HTTP 401."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: missing Authorization header",
        )
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid Authorization header format, expected 'Bearer <token>'",
        )
    return parts[1].strip()


def verify_api_auth(request: Request) -> dict[str, Any]:
    """Dependency verifying OIDC authentication and caller authorization for /api/*."""
    settings = get_settings()
    if settings.auth_disabled:
        return {"email": "auth-disabled@local", "mock": True}

    token = _extract_bearer_token(request)
    claims = verify_oidc_token(
        token,
        expected_audience=settings.expected_audience,
        settings=settings,
    )

    caller_email = claims.get("email")
    if not caller_email or not isinstance(caller_email, str):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: token missing email claim",
        )

    if not is_authorized_api_caller(caller_email, settings):
        logger.warning("Caller service account %s not authorized for /api/*", caller_email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: caller service account '{caller_email}' is not authorized",
        )

    request.state.caller_email = caller_email
    request.state.token_claims = claims
    return claims


def verify_internal_callback_auth(request: Request) -> dict[str, Any]:
    """Dependency verifying OIDC authentication from dib-bridge-sa for /internal/rca-callback."""
    settings = get_settings()
    if settings.auth_disabled:
        return {"email": "auth-disabled@local", "mock": True}

    token = _extract_bearer_token(request)
    claims = verify_oidc_token(
        token,
        expected_audience=settings.expected_audience,
        settings=settings,
    )

    if settings.expected_audience:
        token_aud = claims.get("aud")
        if not token_aud or str(token_aud) != settings.expected_audience:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Forbidden: token audience mismatch (expected {settings.expected_audience})"
                ),
            )

    caller_email = claims.get("email")
    if not caller_email or not isinstance(caller_email, str):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: token missing email claim",
        )

    if not is_authorized_bridge_caller(caller_email, settings):
        logger.warning(
            "Caller %s not authorized for /internal/rca-callback (expected dib-bridge-sa)",
            caller_email,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Forbidden: caller service account '{caller_email}' "
                "is not authorized bridge caller (expected dib-bridge-sa)"
            ),
        )

    request.state.caller_email = caller_email
    request.state.token_claims = claims
    logger.info(
        "Internal callback caller verified: email=%s, aud=%s",
        caller_email,
        claims.get("aud"),
    )
    return claims
