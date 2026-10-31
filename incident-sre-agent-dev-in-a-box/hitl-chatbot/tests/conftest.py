"""Pytest fixtures and environment setup for HITL Chatbot tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator

import pytest

# Ensure hitl-chatbot root is in sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.config import Settings, get_settings  # noqa: E402
from app.session import get_conversation_store  # noqa: E402
from app.tokens import get_replay_registry  # noqa: E402
from app.tools.jira_create import reset_idempotency_cache  # noqa: E402

TEST_ALERT_POLICIES = [
    {
        "name": "projects/test-project/alertPolicies/15010275944346199662",
        "displayName": "Alert Policy for snow-ip-healthcheck-3",
        "conditions": [
            {
                "displayName": "Uptime check failed",
                "conditionThreshold": {
                    "filter": 'metric.label.check_id="snow-ip-healthcheck-3"',
                },
            }
        ],
    }
]

TEST_ALERT_VIOLATIONS = [
    {
        "name": "projects/test-project/alerts/0.test-inc-12345",
        "state": "OPEN",
        "openTime": "2026-10-30T03:00:00Z",
        "policy": {
            "name": "projects/test-project/alertPolicies/15010275944346199662",
            "displayName": "Alert Policy for snow-ip-healthcheck-3",
        },
        "metric": {
            "labels": {
                "check_id": "snow-ip-healthcheck-3",
                "checked_resource_id": "10.0.0.1",
            }
        },
        "resource": {
            "labels": {
                "host": "10.0.0.1",
            }
        },
    }
]


@pytest.fixture(autouse=True)
def clean_environment_and_stores(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Isolate state between test runs and mock Cloud Monitoring alert calls."""
    os.environ["HMAC_SECRET_KEY"] = "test-secret-key-32-bytes-long"
    os.environ["TOKEN_EXPIRY_SECONDS"] = "600"
    os.environ["PROJECT_ID"] = "test-project-dev"

    monkeypatch.setattr(
        "app.tools.alert.fetch_alert_policies",
        lambda pid: list(TEST_ALERT_POLICIES),
    )
    monkeypatch.setattr(
        "app.tools.alert.fetch_policy_alerts",
        lambda pid, pol_id: list(TEST_ALERT_VIOLATIONS),
    )

    get_settings.cache_clear()
    store = get_conversation_store()
    store.clear()

    replay_registry = get_replay_registry()
    replay_registry.reset()

    reset_idempotency_cache()

    yield

    store.clear()
    replay_registry.reset()
    reset_idempotency_cache()
    get_settings.cache_clear()


@pytest.fixture
def test_settings() -> Settings:
    """Provide clean settings instance."""
    return get_settings()
