"""FastMCP Calculator Server implementation for Ambient Agent Registry Demo.

Provides verified arithmetic operations (add, multiply, power) over JSON-RPC
Streamable HTTP (/mcp) and container health check endpoint (/health).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

if TYPE_CHECKING:
    from fastmcp.server.http import StarletteWithLifespan

# Default service prefix ambient-
DEFAULT_SERVICE_NAME = os.getenv("SERVICE_NAME", "ambient-calculator-service")

# Initialize FastMCP service
mcp = FastMCP(
    name=DEFAULT_SERVICE_NAME,
    instructions="Provides verified arithmetic operations (add, multiply, power)",
)


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers with precision arithmetic.

    Args:
        a: First number.
        b: Second number.

    Returns:
        Sum of a and b.
    """
    return float(a + b)


@mcp.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two numbers with precision arithmetic.

    Args:
        a: First number.
        b: Second number.

    Returns:
        Product of a and b.
    """
    return float(a * b)


@mcp.tool()
def power(base: float, exponent: float) -> float:
    """Raise base to the power of exponent.

    Args:
        base: Base number.
        exponent: Exponent power.

    Returns:
        Base raised to the exponent power.
    """
    if base == 0 and exponent < 0:
        raise ValueError("Division by zero: zero cannot be raised to a negative exponent")
    if base < 0 and not exponent.is_integer():
        raise ValueError("Fractional exponent of a negative base produces a complex number")
    return float(base**exponent)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for Cloud Run container probes."""
    return JSONResponse({"status": "healthy"})


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing Bearer token and optional Google OIDC validation."""

    def __init__(
        self,
        app: ASGIApp,
        enforce_auth: bool = False,
        verify_oidc: bool = False,
        expected_audience: str | None = None,
        allowed_service_accounts: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.enforce_auth = enforce_auth
        self.verify_oidc = verify_oidc
        self.expected_audience = expected_audience
        self.allowed_service_accounts = allowed_service_accounts
        if self.verify_oidc:
            from google.auth.transport.requests import Request as GoogleAuthRequest

            self._auth_request: Any = GoogleAuthRequest()
        else:
            self._auth_request = None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Exempt health probes and CORS preflight OPTIONS requests from authentication
        if (
            not self.enforce_auth
            or request.url.path.rstrip("/") == "/health"
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                {
                    "error": "Unauthorized",
                    "detail": "Missing or invalid Authorization header",
                },
                status_code=401,
            )

        token = auth_header[7:].strip()
        if not token:
            return JSONResponse(
                {
                    "error": "Unauthorized",
                    "detail": "Empty Bearer token provided",
                },
                status_code=401,
            )

        if self.verify_oidc:
            try:
                from google.oauth2 import id_token

                claims: dict[str, Any] = id_token.verify_oauth2_token(
                    token,
                    self._auth_request,
                    audience=self.expected_audience,
                )
                if self.allowed_service_accounts:
                    caller_email = claims.get("email")
                    if caller_email not in self.allowed_service_accounts:
                        return JSONResponse(
                            {
                                "error": "Forbidden",
                                "detail": f"Caller email '{caller_email}' not authorized",
                            },
                            status_code=403,
                        )
            except Exception as e:
                return JSONResponse(
                    {
                        "error": "Unauthorized",
                        "detail": f"Token verification failed: {e}",
                    },
                    status_code=401,
                )

        return await call_next(request)


def create_app(
    enforce_auth: bool | None = None,
    verify_oidc: bool | None = None,
    expected_audience: str | None = None,
    allowed_service_accounts: list[str] | None = None,
) -> StarletteWithLifespan:
    """Create and configure the FastMCP Starlette ASGI application.

    Args:
        enforce_auth: Whether to enforce authentication on protected endpoints.
        verify_oidc: Whether to verify Google OIDC tokens using Google certs.
        expected_audience: Expected audience claim for OIDC verification.
        allowed_service_accounts: List of authorized service account emails.

    Returns:
        Configured Starlette application with FastMCP routes and middleware.
    """
    if enforce_auth is None:
        enforce_auth = os.getenv("ENFORCE_AUTH", "false").lower() in ("true", "1", "yes")
    if verify_oidc is None:
        verify_oidc = os.getenv("VERIFY_OIDC", "false").lower() in ("true", "1", "yes")
    if expected_audience is None:
        expected_audience = os.getenv("EXPECTED_AUDIENCE")
    if allowed_service_accounts is None:
        sa_env = os.getenv("ALLOWED_SERVICE_ACCOUNTS", "").strip()
        allowed_service_accounts = [s.strip() for s in sa_env.split(",") if s.strip()] or None

    middleware = [
        Middleware(
            AuthMiddleware,
            enforce_auth=enforce_auth,
            verify_oidc=verify_oidc,
            expected_audience=expected_audience,
            allowed_service_accounts=allowed_service_accounts,
        )
    ]

    return mcp.http_app(
        stateless_http=True,
        json_response=True,
        middleware=middleware,
    )


# Default application instance for ASGI servers (e.g. uvicorn)
app = create_app()
