"""Unit tests for Google OIDC authentication middleware."""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient

from app.config import Settings
from app.indexer import DeploymentContextIndexer
from app.server import create_app


def test_auth_disabled_bypass() -> None:
    """Verify requests succeed without token when AUTH_DISABLED is True."""
    settings = Settings(AUTH_DISABLED=True)
    app = create_app(indexer=DeploymentContextIndexer(), settings=settings)

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


def test_auth_health_endpoint_bypass_with_auth_enabled(test_settings: Settings) -> None:
    """Verify /health bypasses authentication even when auth is enabled."""
    app = create_app(indexer=DeploymentContextIndexer(), settings=test_settings)

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


def test_auth_missing_header_returns_401(test_settings: Settings) -> None:
    """Verify missing Authorization header returns 401 Unauthorized."""
    app = create_app(indexer=DeploymentContextIndexer(), settings=test_settings)

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 401
        assert "missing or invalid Bearer token" in resp.json()["error"]


def test_auth_invalid_token_returns_401(test_settings: Settings) -> None:
    """Verify invalid ID token returns 401 Unauthorized."""
    app = create_app(indexer=DeploymentContextIndexer(), settings=test_settings)

    with patch(
        "google.oauth2.id_token.verify_oauth2_token",
        side_effect=ValueError("Token expired"),
    ):
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer bad-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 401
            assert "invalid ID token" in resp.json()["error"]


def test_auth_unauthorized_service_account_returns_403(test_settings: Settings) -> None:
    """Verify token from untrusted service account returns 403 Forbidden."""
    app = create_app(indexer=DeploymentContextIndexer(), settings=test_settings)

    mock_claims = {
        "email": "untrusted-attacker-sa@example-project-id.iam.gserviceaccount.com",
        "email_verified": True,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims):
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer valid-but-unauthorized-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 403
            assert "is not authorized" in resp.json()["error"]


def test_auth_authorized_service_account_accepted(test_settings: Settings) -> None:
    """Verify token from authorized service account passes auth check."""
    app = create_app(indexer=DeploymentContextIndexer(), settings=test_settings)

    mock_claims = {
        "email": "authorized-caller@example-project-id.iam.gserviceaccount.com",
        "email_verified": True,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims):
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-runner", "version": "1.0.0"},
                    },
                },
                headers={
                    "Authorization": "Bearer valid-and-authorized-token",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 200
            assert "serverInfo" in resp.text


def test_auth_unconfigured_authorized_accounts_fails_closed() -> None:
    """Verify auth fails closed with 403 when AUTH_DISABLED is False but no accounts configured."""
    settings = Settings(
        AUTH_DISABLED=False,
        AUTHORIZED_CALLER_SERVICE_ACCOUNTS="",
    )
    app = create_app(indexer=DeploymentContextIndexer(), settings=settings)

    mock_claims = {
        "email": "caller@example-project-id.iam.gserviceaccount.com",
        "email_verified": True,
    }

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=mock_claims):
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 403
            assert "No authorized service accounts configured" in resp.json()["error"]
