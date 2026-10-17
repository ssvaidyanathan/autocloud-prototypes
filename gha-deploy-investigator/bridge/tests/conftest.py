from datetime import datetime, timezone
from typing import Generator

import pytest
from bridge.app.config import BridgeConfig
from bridge.app.contracts.findings import EvidenceItem
from bridge.app.contracts.incident import Incident
from bridge.app.contracts.report import ProbableRootCause, Report, TimelineItem
from bridge.tests.fake_github_server import app as fake_github_app
from bridge.tests.fake_github_server import state as github_state
from bridge.tests.fake_ticket_server import app as fake_ticket_app
from bridge.tests.fake_ticket_server import state as ticket_state
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_mock_states() -> Generator[None, None, None]:
    github_state.reset()
    ticket_state.reset()
    yield
    github_state.reset()
    ticket_state.reset()


@pytest.fixture
def fake_github_client() -> Generator[TestClient, None, None]:
    client = TestClient(fake_github_app, base_url="https://api.github.com")
    yield client
    client.close()


@pytest.fixture
def fake_ticket_client() -> Generator[TestClient, None, None]:
    client = TestClient(fake_ticket_app, base_url="https://example.service-now.com")
    yield client
    client.close()


@pytest.fixture
def bridge_settings() -> BridgeConfig:
    return BridgeConfig(
        app_env="test",
        github_webhook_secret="test-webhook-secret-12345",
        github_token="ghp_test_token_xyz",
        github_api_url="https://api.github.com",
        servicenow_instance_url="https://example.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        enable_github_writeback=True,
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
        description=(
            "Workflow 'Sample App CI/CD Pipeline' failed on branch 'main' "
            "(commit e4b2a8d3) triggered by @octocat"
        ),
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
        github_failing_stage="Run Ruff Lint",
        github_actor="octocat",
        github_commit_author="The Octocat",
    )


@pytest.fixture
def sample_report() -> Report:
    return Report(
        summary=(
            "Workflow run for repository 'my-org/sample-app' failed at stage 'Run Ruff Lint'. "
            "Primary failure: app/main.py:42:5: F841"
        ),
        impact="Pipeline delivery blocked on branch 'main'",
        timeline=[
            TimelineItem(
                timestamp=datetime(2026, 10, 16, 21, 0, 0, tzinfo=timezone.utc),
                description="Workflow initiated",
            ),
        ],
        evidence_by_area={
            "logs": [
                EvidenceItem(
                    kind="github_runner_log",
                    excerpt="app/main.py:42:5: F841 Local variable `unused_token` is unused",
                    timestamp=datetime(2026, 10, 16, 21, 0, 30, tzinfo=timezone.utc),
                )
            ]
        },
        probable_root_cause=ProbableRootCause(
            text="Pipeline step 'Run Ruff Lint' failed: Local variable `unused_token` unused",
            confidence=0.92,
        ),
        recommended_actions=[
            "Run local linter (`ruff check .`) to verify and fix syntax/style violations",
        ],
        skills_used=["github-actions-logs-analysis"],
    )
