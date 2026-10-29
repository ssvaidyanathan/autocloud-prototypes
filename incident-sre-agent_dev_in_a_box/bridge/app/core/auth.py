import base64
import hmac
import json
import logging
import os
from typing import Any, Optional

from fastapi import HTTPException, Request

from ..config import BridgeConfig, get_settings

logger = logging.getLogger(__name__)


def _is_local_dev_env(config: Optional[BridgeConfig] = None) -> bool:
    """Check if environment or config has explicit local/dev flag set."""
    if config and getattr(config, "app_env", None):
        val = str(config.app_env).strip().lower()
        if val in ("local", "dev", "development", "test", "testing"):
            return True
        if val in ("prod", "production", "staging"):
            return False
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
    # Add padding if needed
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
    """Verify OIDC Bearer token in Authorization header.

    Verifies:
      1. Presence and format of Authorization: Bearer <token>.
      2. In development / testing:
         - Accepts fallback token 'mock-oidc-token-for-testing' when local/dev env is set.
         - Allows unverified decoding fallback for local mock/synthetic tokens.
      3. In production:
         - Strictly rejects mock tokens with 401.
         - Uses google.oauth2.id_token to verify cryptographic signature against Google certs.
         - Strictly rejects unverified / invalid signature tokens with 401.
    """
    settings = config or get_settings()

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header: expected 'Bearer <token>'",
        )

    token = parts[1].strip()

    # Fast-path for default mock token used by local agent
    if token == "mock-oidc-token-for-testing":
        if _is_local_dev_env(settings):
            logger.info("Accepted mock OIDC token for local testing")
            return {"aud": settings.callback_url, "email": settings.agent_sa, "mock": True}
        logger.error("Mock OIDC token rejected in production environment")
        raise HTTPException(
            status_code=401,
            detail="Mock OIDC token not allowed in production",
        )

    expected_aud = settings.callback_url
    expected_email = settings.agent_sa

    # Attempt Google ID token verification if google auth library is available
    verified_claims: Optional[dict[str, Any]] = None
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        req = google_requests.Request()
        verified_claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, req, audience=expected_aud if expected_aud else None
        )
    except (ImportError, ModuleNotFoundError):
        if _is_local_dev_env(settings):
            logger.warning(
                "google-auth not installed; decoding JWT unverified in local/dev environment"
            )
            try:
                verified_claims = _decode_jwt_unverified(token)
            except Exception as exc:
                raise HTTPException(
                    status_code=401,
                    detail=f"Invalid OIDC token structure: {exc}",
                ) from exc
        else:
            logger.error("google-auth library missing in production environment")
            raise HTTPException(
                status_code=500,
                detail="Authentication provider unavailable",
            )
    except Exception as exc:
        if _is_local_dev_env(settings):
            # In local/test environment, allow unverified decode fallback for mock/synthetic JWTs
            try:
                verified_claims = _decode_jwt_unverified(token)
            except Exception:
                raise HTTPException(
                    status_code=401,
                    detail=f"OIDC token verification failed: {exc}",
                ) from exc
        else:
            logger.warning("OIDC token verification rejected: %s", exc)
            raise HTTPException(
                status_code=401,
                detail=f"OIDC token verification failed: {exc}",
            ) from exc

    if verified_claims is None:
        raise HTTPException(status_code=401, detail="Could not extract claims from OIDC token")

    # Verify audience using constant-time comparison
    if expected_aud:
        token_aud = verified_claims.get("aud")
        if not token_aud or not hmac.compare_digest(str(token_aud), str(expected_aud)):
            logger.warning(
                "OIDC token audience mismatch: expected '%s', got '%s'",
                expected_aud,
                token_aud,
            )
            raise HTTPException(status_code=403, detail="Invalid token audience")

    # Verify caller service account email using constant-time comparison
    if expected_email:
        token_email = verified_claims.get("email")
        if not token_email or not hmac.compare_digest(str(token_email), str(expected_email)):
            logger.warning(
                "OIDC token caller email mismatch: expected '%s', got '%s'",
                expected_email,
                token_email,
            )
            raise HTTPException(status_code=403, detail="Invalid caller service account")

    return verified_claims
