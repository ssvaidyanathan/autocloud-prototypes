import base64
import json
import logging
import os
from typing import Any, Optional

from fastapi import HTTPException, Request

from ..config import BridgeConfig, get_settings

logger = logging.getLogger(__name__)


def _is_local_dev_env(config: Optional[BridgeConfig] = None) -> bool:
    """Check if environment or config has explicit local/dev flag set."""
    if config and getattr(config, "is_local", False):
        return True
    for var in ("APP_ENV", "ENVIRONMENT"):
        val = os.environ.get(var, "").strip().lower()
        if val in ("local", "dev", "development", "test", "testing"):
            return True
    return False


def _decode_jwt_unverified(token: str) -> dict[str, Any]:
    """Decode JWT claims without cryptographic verification for claims inspection."""
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
        raise ValueError("JWT payload is not a JSON object")
    return data


def create_mock_oidc_token(audience: str, email: str) -> str:
    """Helper for tests to generate a mock unsigned JWT with given claims."""
    header = {"alg": "none", "typ": "JWT"}
    payload = {"aud": audience, "email": email, "iss": "https://accounts.google.com"}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{h_b64}.{p_b64}.mock_signature"


def verify_oidc_token(
    request: Request,
    config: Optional[BridgeConfig] = None,
) -> dict[str, Any]:
    """Verify OIDC Bearer token in Authorization header."""
    settings = config or get_settings()

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format: expected 'Bearer <token>'",
        )

    token = parts[1].strip()

    # Fast-path for testing/dev environments
    if _is_local_dev_env(settings):
        if token == "mock-oidc-token-for-testing":
            return {
                "email": "test-agent@example.iam.gserviceaccount.com",
                "aud": settings.callback_url or "http://testserver/callback/report",
                "iss": "https://accounts.google.com",
            }
        try:
            return _decode_jwt_unverified(token)
        except Exception:
            return {"email": "test-agent@example.iam.gserviceaccount.com"}

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        audience = settings.callback_url or str(request.url)
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
        return claims
    except Exception as exc:
        logger.warning("OIDC token cryptographic verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=f"Invalid OIDC token: {exc}") from exc
