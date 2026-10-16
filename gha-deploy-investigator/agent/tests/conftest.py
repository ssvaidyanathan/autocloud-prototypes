from datetime import datetime, timezone
from typing import Generator

import pytest
from agent.app.config import AgentConfig
from agent.app.contracts.incident import Incident
from agent.app.contracts.request import InvestigationRequest
from bridge.tests.fake_github_server import app as fake_github_app
from bridge.tests.fake_github_server import state as github_state
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_github_state() -> Generator[None, None, None]:
    github_state.reset()
    yield
    github_state.reset()


@pytest.fixture
def fake_github_client() -> Generator[TestClient, None, None]:
    client = TestClient(fake_github_app, base_url="https://api.github.com")
    yield client
    client.close()


@pytest.fixture
def agent_settings() -> AgentConfig:
    return AgentConfig(
        app_env="test",
        github_api_url="https://api.github.com",
        github_token="ghp_test_token_123",
    )


@pytest.fixture
def sample_incident() -> Incident:
    return Incident(
        id="gha-1849201948",
        source="github",
        external_id="1849201948",
        external_key="my-org/sample-app#1849201948",
        external_url="https://github.com/my-org/sample-app/actions/runs/1849201948",
        title="[CI/CD Failure] sample-app: Sample App CI/CD Pipeline",
        description="Workflow failed on main branch",
        severity="P2",
        service_hint="sample-app",
        labels=["ci-cd", "github-actions", "failure"],
        opened_at=datetime(2026, 10, 16, 21, 0, 0, tzinfo=timezone.utc),
        reporter="octocat",
        github_repo="my-org/sample-app",
        github_run_id=1849201948,
        github_head_sha="e4b2a8d39c1234567890abcdef1234567890abcd",
        github_branch="main",
        github_pr_number=42,
    )


@pytest.fixture
def sample_request(sample_incident: Incident) -> InvestigationRequest:
    return InvestigationRequest(
        incident=sample_incident,
        callback_url="http://testbridge.local/callback/report",
        requested_at=datetime(2026, 10, 16, 21, 0, 0, tzinfo=timezone.utc),
    )
