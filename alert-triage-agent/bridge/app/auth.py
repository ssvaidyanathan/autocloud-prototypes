"""Authentication and authorization dependencies for ata-bridge endpoints."""

import logging
from typing import Any
from fastapi import Depends, HTTPException, Request, status

from .config import BridgeConfig, get_settings

logger = logging.getLogger(__name__)


async def verify_agent_caller(
    request: Request,
    settings: BridgeConfig = Depends(get_settings),
) -> dict[str, Any]:
    """Verify Google-signed ID token from ata-agent-sa.

    Enforces:
    1. Authorization header present and formatted as 'Bearer <token>'.
    2. Google-signed ID token signature and validity.
    3. Audience matches bridge service URLs / base host.
    4. Service account email matches ata-agent-sa or authorized operator SA.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header; Bearer ID token required",
        )

    token = auth_header[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty Bearer token",
        )

    expected_agent_sa = f"{settings.name_prefix}-agent-sa@{settings.project_id}.iam.gserviceaccount.com"

    # In local testing, allow mock agent tokens
    if settings.is_local:
        if token == "unauthorized-sa-token":
            claims = {"email": "attacker@example.com", "aud": "mock-bridge-aud"}
        elif token == "wrong-audience-token":
            claims = {"email": expected_agent_sa, "aud": "https://wrong-service.a.run.app"}
        elif token in ("mock-agent-token", "test-agent-token", "valid-agent-token"):
            claims = {"email": expected_agent_sa, "aud": "mock-bridge-aud"}
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid ID token",
            )
    else:
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            req = google_requests.Request()
            claims = id_token.verify_oauth2_token(token, req)
        except Exception as exc:
            logger.warning("Google ID token verification failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid or expired ID token: {exc}",
            ) from exc

    # Validate audience
    token_aud = str(claims.get("aud", "")).rstrip("/")
    expected_audiences = {
        str(request.base_url).rstrip("/"),
        f"https://{settings.name_prefix}-bridge-4klvlqnmpa-uw.a.run.app",
        f"https://{settings.name_prefix}-bridge-185099261133.{settings.region}.run.app",
    }
    if settings.is_local:
        expected_audiences.add("mock-bridge-aud")

    if token_aud not in expected_audiences:
        logger.warning("ID token audience mismatch: got '%s', expected one of %s", token_aud, expected_audiences)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Audience mismatch: {token_aud}",
        )

    # Validate caller identity
    caller_email = claims.get("email", "")
    allowed_callers = {
        expected_agent_sa,
        f"{settings.name_prefix}-bridge-sa@{settings.project_id}.iam.gserviceaccount.com",
        "scion-fde-ops@deploy-demo-test.iam.gserviceaccount.com",
    }
    if caller_email not in allowed_callers:
        logger.warning("Unauthorized caller email '%s' (expected %s)", caller_email, expected_agent_sa)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden caller identity: {caller_email}",
        )

    return claims
