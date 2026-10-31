"""Unit tests for Bridge HITL endpoints, idempotency, ticket templates, and RCA forwarding."""

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from bridge.app.adapters.jira import JiraAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.core.auth import create_mock_oidc_token
from bridge.app.core.metadata import get_ticket_registry, reset_ticket_registry
from bridge.app.main import app
from bridge.app.templates.ticket_template import (
    get_ticket_template_content,
    render_ticket_description,
)
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_registry() -> None:
    reset_ticket_registry()


@pytest.fixture
def hitl_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_ENV", "local")
    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        jira_base_url="https://jira.example.internal",
        jira_email="sre@example.internal",
        jira_api_token="test-token",
        agent_sa="agent-sa@test-project.iam.gserviceaccount.com",
        hitl_caller_sa="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        callback_url="https://bridge.example.internal/callback/report",
    )
    app.dependency_overrides[get_settings] = lambda: test_config
    client = TestClient(app)
    return client


def test_healthz_endpoint(hitl_test_client: TestClient) -> None:
    """GET /healthz returns healthy status."""
    resp = hitl_test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_ticket_template_defaults_and_custom(tmp_path: Path) -> None:
    """Verify generic ticket template contains required sections and custom path override."""
    default_content = get_ticket_template_content()
    assert "# **" in default_content
    assert "**Date:**" in default_content
    assert "**Authors:**" in default_content
    assert "**Status:**" in default_content
    assert "**Statuspage:**" in default_content
    assert "**Summary:**" in default_content
    assert "**Impact / Observations" in default_content
    assert "**Impacted Customers" in default_content
    assert "**Root Causes:**" in default_content
    assert "**Trigger:**" in default_content
    assert "**Detection:**" in default_content
    assert "**Initial Response:**" in default_content
    assert "**Resolution:**" in default_content
    assert "**Action Items / Jira Tickets:**" in default_content
    assert "**Timeline:**" in default_content
    assert "**Lessons Learned and Incident Feedback:**" in default_content
    assert "**Supporting information" in default_content

    rendered = render_ticket_description(
        {
            "title": "High Latency in Checkout",
            "date": "2026-10-30",
            "author": "hitl-user-1",
            "summary": "Service latency spiked past threshold.",
        }
    )
    assert "High Latency in Checkout" in rendered
    assert "2026-10-30" in rendered
    assert "hitl-user-1" in rendered

    # Custom template via file path
    custom_file = tmp_path / "custom.md"
    custom_file.write_text("CUSTOM TICKET: {{ title }} ({{ date }})", encoding="utf-8")
    custom_rendered = render_ticket_description(
        {"title": "Custom Test", "date": "2026-10-30"},
        template_path=str(custom_file),
    )
    assert custom_rendered == "CUSTOM TICKET: Custom Test (2026-10-30)"


def test_create_jira_issue_auth_missing(hitl_test_client: TestClient) -> None:
    """POST /api/jira/issue without auth header returns 401."""
    payload = {
        "summary": "Alert failure",
        "labels": ["hitl", "alert-123"],
    }
    resp = hitl_test_client.post("/api/jira/issue", json=payload)
    assert resp.status_code == 401
    assert "Missing Authorization header" in resp.json()["detail"]


def test_create_jira_issue_validation_error(hitl_test_client: TestClient) -> None:
    """POST /api/jira/issue with missing required summary returns 422."""
    headers = {"Authorization": "Bearer mock-oidc-token-for-testing"}
    resp = hitl_test_client.post("/api/jira/issue", json={}, headers=headers)
    assert resp.status_code == 422


def test_create_jira_issue_idempotency_duplicate(
    hitl_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/jira/issue returns already_exists if ticket with alert label exists."""
    headers = {"Authorization": "Bearer mock-oidc-token-for-testing"}
    payload = {
        "project_key": "AMJTEST",
        "summary": "High CPU utilization",
        "priority": "P1",
        "labels": ["hitl", "alert-violation-999"],
        "callback_url": "https://chatbot.example.internal/callback",
        "slack_thread": "1730000000.000100",
    }

    mock_search = MagicMock(
        return_value=[{"key": "AMJTEST-15", "fields": {"summary": "Existing issue"}}]
    )
    monkeypatch.setattr(JiraAdapter, "search_issues", mock_search)

    resp = hitl_test_client.post("/api/jira/issue", json=payload, headers=headers)
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "already_exists"
    assert res_json["issue_key"] == "AMJTEST-15"
    assert "AMJTEST-15" in res_json["issue_url"]

    # Verify search was executed with expected JQL
    mock_search.assert_called_once()
    assert 'labels = "alert-violation-999"' in mock_search.call_args[0][0]

    # Verify metadata registry updated
    reg = get_ticket_registry()
    cached = reg.get("AMJTEST-15")
    assert cached is not None
    assert cached.callback_url == "https://chatbot.example.internal/callback"
    assert cached.slack_thread == "1730000000.000100"


def test_create_jira_issue_success(
    hitl_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/jira/issue creates new issue in Jira and caches metadata."""
    headers = {"Authorization": "Bearer mock-oidc-token-for-testing"}
    payload = {
        "project": "AMJTEST",
        "summary": "Database connectivity loss",
        "description": "DB connection timeout after 3 retries",
        "priority": "P2",
        "labels": ["hitl", "alert-violation-888"],
        "callback_url": "https://chatbot.example.internal/rca",
        "slack_thread": "1730000000.000200",
        "alert_started_at": "2026-10-30T20:02:58Z",
    }

    mock_search = MagicMock(return_value=[])
    mock_create = MagicMock(return_value={"key": "AMJTEST-22", "id": "10022"})
    mock_prop = MagicMock(return_value=True)

    monkeypatch.setattr(JiraAdapter, "search_issues", mock_search)
    monkeypatch.setattr(JiraAdapter, "create_issue", mock_create)
    monkeypatch.setattr(JiraAdapter, "set_issue_property", mock_prop)

    resp = hitl_test_client.post("/api/jira/issue", json=payload, headers=headers)
    assert resp.status_code == 200
    res_json = resp.json()
    assert res_json["status"] == "created"
    assert res_json["issue_key"] == "AMJTEST-22"
    assert res_json["issue_url"] == "https://jira.example.internal/browse/AMJTEST-22"

    mock_create.assert_called_once()
    assert mock_create.call_args[1]["project_key"] == "AMJTEST"
    assert mock_create.call_args[1]["priority"] == "P2"

    mock_prop.assert_called_once()
    assert mock_prop.call_args[0][0] == "AMJTEST-22"
    assert mock_prop.call_args[0][1] == "hitl_metadata"
    assert mock_prop.call_args[0][2]["alert_started_at"] == "2026-10-30T20:02:58Z"

    reg = get_ticket_registry()
    cached = reg.get("AMJTEST-22")
    assert cached is not None
    assert cached.status == "created"
    assert cached.callback_url == "https://chatbot.example.internal/rca"
    assert cached.slack_thread == "1730000000.000200"
    assert cached.alert_started_at == "2026-10-30T20:02:58Z"


def test_get_investigation_from_cache(hitl_test_client: TestClient) -> None:
    """GET /api/investigations/{external_key} returns status from cache."""
    reg = get_ticket_registry()
    reg.set(
        issue_key="AMJTEST-30",
        summary="Memory leak investigation",
        status="investigating",
    )

    resp = hitl_test_client.get("/api/investigations/AMJTEST-30")
    assert resp.status_code == 200
    data = resp.json()
    assert data["external_key"] == "AMJTEST-30"
    assert data["status"] == "investigating"
    assert data["summary"] == "Memory leak investigation"


def test_get_investigation_from_jira_fallback(
    hitl_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/investigations/{external_key} falls back to Jira when not in cache."""
    mock_get = MagicMock(
        return_value={
            "fields": {
                "summary": "External Jira ticket",
                "status": {"name": "In Progress"},
            }
        }
    )
    monkeypatch.setattr(JiraAdapter, "get_issue", mock_get)

    resp = hitl_test_client.get("/api/investigations/AMJTEST-50")
    assert resp.status_code == 200
    data = resp.json()
    assert data["external_key"] == "AMJTEST-50"
    assert data["status"] == "In Progress"
    assert data["summary"] == "External Jira ticket"


def test_get_investigation_not_found(
    hitl_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/investigations/{external_key} returns 404 when not in cache or Jira."""
    mock_get = MagicMock(return_value=None)
    monkeypatch.setattr(JiraAdapter, "get_issue", mock_get)

    resp = hitl_test_client.get("/api/investigations/UNKNOWN-999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_rca_forwarding_in_callback_report(
    hitl_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle_callback_report posts comments to Jira and forwards RCA to cached callback_url."""
    # 1. Register ticket in cache with callback_url
    reg = get_ticket_registry()
    reg.set(
        issue_key="AMJTEST-42",
        summary="Service crash",
        status="investigating",
        callback_url="https://hitl-chatbot.example.internal/internal/rca-callback",
        slack_thread="1730000000.000300",
    )

    # 2. Mock Jira adapter post_report
    mock_post_report = MagicMock()
    monkeypatch.setattr(JiraAdapter, "post_report", mock_post_report)

    # 3. Mock outbound HTTP client for RCA callback
    mock_client = MagicMock()
    mock_resp = MagicMock(is_error=False, status_code=200)
    mock_client.post.return_value = mock_resp
    mock_client.__enter__.return_value = mock_client

    with patch("bridge.app.main.httpx.Client", return_value=mock_client):
        token = create_mock_oidc_token(
            audience="https://bridge.example.internal/callback/report",
            email="agent-sa@test-project.iam.gserviceaccount.com",
        )
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "investigation_id": "inv-rca-test-1",
            "incident_id": "inc-rca-test-1",
            "incident_source": "jira",
            "incident_external_id": "AMJTEST-42",
            "incident_external_key": "AMJTEST-42",
            "status": "completed",
            "report": {
                "summary": "Out of memory due to cache growth",
                "impact": "500 errors on /api/items",
                "timeline": [],
                "evidence_by_area": {},
            },
        }

        resp = hitl_test_client.post("/callback/report", json=payload, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify Jira report posted
        mock_post_report.assert_called_once()

        # Verify RCA forwarded to callback_url with OIDC token
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        call_json = mock_client.post.call_args[1]["json"]
        call_headers = mock_client.post.call_args[1]["headers"]

        assert call_url == "https://hitl-chatbot.example.internal/internal/rca-callback"
        assert call_json["ticket_key"] == "AMJTEST-42"
        assert call_json["thread_ref"] == "1730000000.000300"
        assert call_json["report"]["summary"] == "Out of memory due to cache growth"
        assert "Authorization" in call_headers
        assert "Bearer " in call_headers["Authorization"]

        # Verify cache status updated
        cached = reg.get("AMJTEST-42")
        assert cached is not None
        assert cached.status == "completed"
        assert cached.summary == "Out of memory due to cache growth"


def test_create_jira_issue_custom_template_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/jira/issue renders description using settings.ticket_template_path."""
    custom_tmpl = tmp_path / "custom_tmpl.md"
    custom_tmpl.write_text(
        "CUSTOM TEMPLATE: Title={{ title }} Summary={{ summary }}",
        encoding="utf-8",
    )

    test_config = BridgeConfig(
        app_env="local",
        gcp_project="test-project",
        jira_base_url="https://jira.example.internal",
        jira_email="sre@example.internal",
        jira_api_token="test-token",
        agent_sa="agent-sa@test-project.iam.gserviceaccount.com",
        hitl_caller_sa="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        ticket_template_path=str(custom_tmpl),
    )
    app.dependency_overrides[get_settings] = lambda: test_config
    client = TestClient(app)

    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "project_key": "AMJTEST",
        "summary": "Out of memory spike",
        "labels": ["hitl"],
    }

    mock_search = MagicMock(return_value=[])
    mock_create = MagicMock(return_value={"key": "AMJTEST-99", "id": "10099"})
    mock_prop = MagicMock(return_value=True)

    monkeypatch.setattr(JiraAdapter, "search_issues", mock_search)
    monkeypatch.setattr(JiraAdapter, "create_issue", mock_create)
    monkeypatch.setattr(JiraAdapter, "set_issue_property", mock_prop)

    resp = client.post("/api/jira/issue", json=payload, headers=headers)
    assert resp.status_code == 200
    mock_create.assert_called_once()
    passed_desc = mock_create.call_args[1]["description"]
    assert "CUSTOM TEMPLATE: Title=Out of memory spike Summary=Out of memory spike" in passed_desc


def test_ticket_template_gcs_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify GCS template resolution fetches and caches template text."""
    from bridge.app.templates.ticket_template import (
        clear_template_cache,
        get_ticket_template_content,
        render_ticket_description,
    )

    clear_template_cache()
    gcs_uri = "gs://test-bucket/templates/custom_incident.md"
    mock_gcs_content = "# **GCS Template: {{ title }}**\n\n**Summary:**\n{{ summary }}"

    # Mock _load_gcs_template
    monkeypatch.setattr(
        "bridge.app.templates.ticket_template._load_gcs_template",
        lambda uri: mock_gcs_content,
    )

    content = get_ticket_template_content(gcs_uri)
    assert content == mock_gcs_content

    # Second fetch should hit in-memory cache
    content_cached = get_ticket_template_content(gcs_uri)
    assert content_cached == mock_gcs_content

    rendered = render_ticket_description(
        {"title": "GCS Incident Title", "summary": "Detailed summary"},
        template_path=gcs_uri,
    )
    assert "GCS Template: GCS Incident Title" in rendered
    assert "Detailed summary" in rendered
    clear_template_cache()


def test_add_jira_comment_auth_missing(hitl_test_client: TestClient) -> None:
    """POST /api/jira/issue/{key}/comment without auth header returns 401."""
    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json={"body": "RCA report attached."},
    )
    assert resp.status_code == 401
    assert "Missing Authorization header" in resp.json()["detail"]


def test_add_jira_comment_unauthorized_sa(hitl_test_client: TestClient) -> None:
    """POST /api/jira/issue/{key}/comment with unauthorized caller SA returns 403."""
    token = create_mock_oidc_token(
        email="unauthorized-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json={"body": "RCA report attached."},
        headers=headers,
    )
    assert resp.status_code == 403
    assert "Invalid caller service account" in resp.json()["detail"]


def test_add_jira_comment_empty_body(hitl_test_client: TestClient) -> None:
    """POST /api/jira/issue/{key}/comment with empty body returns 400."""
    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json={"body": "   "},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "Comment body must not be empty" in resp.json()["detail"]


def test_add_jira_comment_issue_not_found_404(
    hitl_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/jira/issue/{key}/comment returns 404 when Jira issue is not found."""
    import httpx

    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}

    def mock_add_comment(*args: Any, **kwargs: Any) -> Any:
        req = httpx.Request("POST", "https://jira.example.internal/rest/api/3/issue/NONEXIST-99/comment")
        resp = httpx.Response(404, request=req, text="Issue Does Not Exist")
        raise httpx.HTTPStatusError("Issue not found", request=req, response=resp)

    monkeypatch.setattr(JiraAdapter, "add_comment", mock_add_comment)

    resp = hitl_test_client.post(
        "/api/jira/issue/NONEXIST-99/comment",
        json={"body": "RCA comment"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert "Jira issue not found: NONEXIST-99" in resp.json()["detail"]


def test_add_jira_comment_bad_input_400(
    hitl_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/jira/issue/{key}/comment returns 400 when upstream Jira returns 400."""
    import httpx

    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}

    def mock_add_comment(*args: Any, **kwargs: Any) -> Any:
        req = httpx.Request("POST", "https://jira.example.internal/rest/api/3/issue/AMJTEST-20/comment")
        resp = httpx.Response(400, request=req, text="Invalid ADF content")
        raise httpx.HTTPStatusError("Bad request", request=req, response=resp)

    monkeypatch.setattr(JiraAdapter, "add_comment", mock_add_comment)

    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json={"body": "Invalid comment"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "Bad request to Jira API" in resp.json()["detail"]


def test_add_jira_comment_upstream_failure_502(
    hitl_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/jira/issue/{key}/comment returns 502 when upstream Jira fails."""
    import httpx

    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}

    def mock_add_comment(*args: Any, **kwargs: Any) -> Any:
        req = httpx.Request("POST", "https://jira.example.internal/rest/api/3/issue/AMJTEST-20/comment")
        resp = httpx.Response(500, request=req, text="Internal Server Error")
        raise httpx.HTTPStatusError("Server error", request=req, response=resp)

    monkeypatch.setattr(JiraAdapter, "add_comment", mock_add_comment)

    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json={"body": "RCA comment"},
        headers=headers,
    )
    assert resp.status_code == 502
    assert "Upstream Jira failure" in resp.json()["detail"]


def test_add_jira_comment_success(
    hitl_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/jira/issue/{key}/comment appends comment successfully."""
    token = create_mock_oidc_token(
        email="hitl-chatbot-sa@test-project.iam.gserviceaccount.com",
        audience="https://bridge.example.internal/callback/report",
    )
    headers = {"Authorization": f"Bearer {token}"}

    mock_add = MagicMock(return_value={"id": "20042", "self": "https://jira.example.internal/comment/20042"})
    monkeypatch.setattr(JiraAdapter, "add_comment", mock_add)

    payload = {
        "body": "## RCA Findings\n- Replica scale down caused 503 errors.",
        "properties": {"source": "hitl-chatbot"},
    }
    resp = hitl_test_client.post(
        "/api/jira/issue/AMJTEST-20/comment",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "created"
    assert data["issue_key"] == "AMJTEST-20"
    assert data["comment_id"] == "20042"
    mock_add.assert_called_once_with(
        issue_key="AMJTEST-20",
        body="## RCA Findings\n- Replica scale down caused 503 errors.",
        properties={"source": "hitl-chatbot"},
    )


def test_jira_adapter_add_comment_adf_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """JiraAdapter.add_comment converts markdown body to ADF and sets properties."""
    import httpx

    config = BridgeConfig(
        jira_base_url="https://jira.example.internal",
        jira_email="test@example.com",
        jira_api_token="token",
    )

    posted_json: dict[str, Any] = {}

    def mock_post(url: str, json: Optional[dict[str, Any]] = None, **kwargs: Any) -> httpx.Response:
        nonlocal posted_json
        posted_json = json or {}
        req = httpx.Request("POST", url)
        return httpx.Response(
            201,
            request=req,
            json={"id": "30001", "created": "2026-10-31T01:00:00Z"},
        )

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post = mock_post
    adapter = JiraAdapter(config=config, http_client=mock_client)

    res = adapter.add_comment(
        issue_key="AMJTEST-20",
        body="# Heading\n- Item 1",
        properties={"hitl": "true"},
    )
    assert res["id"] == "30001"
    assert "body" in posted_json
    assert posted_json["body"]["type"] == "doc"
    assert "properties" in posted_json
    assert posted_json["properties"] == [{"key": "hitl", "value": "true"}]
