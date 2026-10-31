"""Unit tests for Google OIDC token authentication and authorization on /api/* and /internal/*."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import create_mock_oidc_token
from app.config import get_settings
from app.main import app


@pytest.fixture
def client() -> TestClient:
    """FastAPI test client."""
    return TestClient(app)


def test_health_endpoint_is_public_and_unauthenticated(client: TestClient) -> None:
    """GET /health must be accessible without any Authorization header."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_api_chat_unauthenticated_returns_401(client: TestClient) -> None:
    """POST /api/chat without Authorization header returns HTTP 401."""
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 401
    assert "unauthorized" in resp.json()["detail"].lower()


def test_api_chat_approve_unauthenticated_returns_401(client: TestClient) -> None:
    """POST /api/chat/approve without Authorization header returns HTTP 401."""
    resp = client.post("/api/chat/approve", json={"signed_token": "abc.def.ghi"})
    assert resp.status_code == 401
    assert "unauthorized" in resp.json()["detail"].lower()


def test_api_investigations_unauthenticated_returns_401(client: TestClient) -> None:
    """GET /api/investigations/{ticket_key} without Authorization header returns HTTP 401."""
    resp = client.get("/api/investigations/AMJTEST-22")
    assert resp.status_code == 401
    assert "unauthorized" in resp.json()["detail"].lower()


def test_internal_rca_callback_unauthenticated_returns_401(client: TestClient) -> None:
    """POST /internal/rca-callback without Authorization header returns HTTP 401."""
    resp = client.post(
        "/internal/rca-callback",
        json={"ticket_key": "AMJTEST-22", "report": {"summary": "Root cause found"}},
    )
    assert resp.status_code == 401
    assert "unauthorized" in resp.json()["detail"].lower()


def test_malformed_authorization_header_returns_401(client: TestClient) -> None:
    """Authorization header not starting with 'Bearer ' or with empty token returns HTTP 401."""
    resp1 = client.get("/api/investigations/AMJTEST-22", headers={"Authorization": "Basic 12345"})
    assert resp1.status_code == 401

    resp2 = client.get("/api/investigations/AMJTEST-22", headers={"Authorization": "Bearer "})
    assert resp2.status_code == 401


def test_unauthorized_caller_identity_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OIDC token from an unauthorized identity returns HTTP 403 Forbidden."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "local")

    unauthorized_token = create_mock_oidc_token(
        email="unauthorized-intruder@attacker-domain.com",
    )
    headers = {"Authorization": f"Bearer {unauthorized_token}"}

    resp_chat = client.post("/api/chat", json={"message": "hello"}, headers=headers)
    assert resp_chat.status_code == 403
    assert "not authorized" in resp_chat.json()["detail"].lower()

    resp_inv = client.get("/api/investigations/AMJTEST-22", headers=headers)
    assert resp_inv.status_code == 403
    assert "not authorized" in resp_inv.json()["detail"].lower()


def test_authorized_caller_identity_returns_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OIDC token from an authorized service account returns HTTP 200."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "local")

    authorized_token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-corp.iam.gserviceaccount.com",
    )
    headers = {"Authorization": f"Bearer {authorized_token}"}

    resp_inv = client.get("/api/investigations/AMJTEST-22", headers=headers)
    assert resp_inv.status_code == 200
    assert resp_inv.json()["ticket_key"] == "AMJTEST-22"


def test_internal_rca_callback_requires_dib_bridge_sa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /internal/rca-callback requires dib-bridge-sa caller identity."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "local")

    # Non-bridge caller is rejected with 403
    wrong_caller_token = create_mock_oidc_token(
        email="other-sa@test-corp.iam.gserviceaccount.com",
    )
    resp_reject = client.post(
        "/internal/rca-callback",
        json={"ticket_key": "AMJTEST-22", "report": {}},
        headers={"Authorization": f"Bearer {wrong_caller_token}"},
    )
    assert resp_reject.status_code == 403
    assert "dib-bridge-sa" in resp_reject.json()["detail"].lower()

    # Authorized dib-bridge-sa caller succeeds with 200
    bridge_token = create_mock_oidc_token(
        email="dib-bridge-sa@test-corp.iam.gserviceaccount.com",
    )
    resp_ok = client.post(
        "/internal/rca-callback",
        json={"ticket_key": "AMJTEST-22", "report": {"summary": "Resolved"}},
        headers={"Authorization": f"Bearer {bridge_token}"},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["status"] == "received"


def test_internal_rca_callback_audience_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /internal/rca-callback validates token audience when configured."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "expected_audience", "https://chatbot.internal.service")

    # Mismatched audience returns 403
    token_wrong_aud = create_mock_oidc_token(
        email="dib-bridge-sa@test-corp.iam.gserviceaccount.com",
        audience="https://wrong-audience.internal.service",
    )
    resp_aud_err = client.post(
        "/internal/rca-callback",
        json={"ticket_key": "AMJTEST-22", "report": {}},
        headers={"Authorization": f"Bearer {token_wrong_aud}"},
    )
    assert resp_aud_err.status_code == 403
    assert "audience mismatch" in resp_aud_err.json()["detail"].lower()

    # Matching audience succeeds
    token_correct_aud = create_mock_oidc_token(
        email="dib-bridge-sa@test-corp.iam.gserviceaccount.com",
        audience="https://chatbot.internal.service",
    )
    resp_ok = client.post(
        "/internal/rca-callback",
        json={"ticket_key": "AMJTEST-22", "report": {}},
        headers={"Authorization": f"Bearer {token_correct_aud}"},
    )
    assert resp_ok.status_code == 200
