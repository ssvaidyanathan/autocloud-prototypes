"""Google OIDC authentication middleware for service-to-service calls."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class GoogleAuthMiddleware(BaseHTTPMiddleware):
    """Middleware verifying Google OIDC ID tokens on incoming requests."""

    def __init__(self, app: Any, settings: Optional[Settings] = None) -> None:  # noqa: ANN401
        super().__init__(app)
        self.settings = settings or get_settings()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # 1. Bypass authentication if AUTH_DISABLED is set
        if self.settings.AUTH_DISABLED:
            return await call_next(request)

        # 2. Bypass authentication for unauthenticated health probes
        if request.url.path == "/health":
            return await call_next(request)

        # 3. Extract Bearer token from Authorization header
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized: missing or invalid Bearer token in Authorization header"
                },
            )

        token = auth_header[7:].strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized: empty Bearer token"},
            )

        # 4. Verify Google OIDC ID token
        try:
            claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                token,
                google_requests.Request(),
                audience=self.settings.EXPECTED_AUDIENCE or None,
            )
        except Exception as e:
            logger.warning("Google OIDC token verification failed: %s", e)
            return JSONResponse(
                status_code=401,
                content={"error": f"Unauthorized: invalid ID token ({e})"},
            )

        # 5. Verify caller service account email
        caller_email = claims.get("email")
        if not caller_email:
            return JSONResponse(
                status_code=403,
                content={"error": "Forbidden: token missing email claim"},
            )

        authorized_accounts = self.settings.get_authorized_accounts()
        if not authorized_accounts:
            logger.warning("No authorized service accounts configured; failing closed")
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Forbidden: No authorized service accounts configured",
                    "detail": "No authorized service accounts configured",
                },
            )

        if caller_email not in authorized_accounts:
            logger.warning(
                "Caller %s not in authorized accounts: %s",
                caller_email,
                authorized_accounts,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": (
                        f"Forbidden: caller service account '{caller_email}' is not authorized"
                    ),
                    "detail": f"caller service account '{caller_email}' is not authorized",
                },
            )

        # Attach caller information to request state
        request.state.caller_email = caller_email
        request.state.token_claims = claims

        return await call_next(request)
