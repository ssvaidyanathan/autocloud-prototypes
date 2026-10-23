"""Unit tests for Bridge /callback/report deduplication and TTL expiration."""

import concurrent.futures
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.contracts.report import ProbableRootCause, Report
from bridge.app.core.auth import create_mock_oidc_token
from bridge.app.core.dedup import CallbackDedupRegistry, clear, get_dedup_registry, reset
from bridge.app.main import app
from fastapi.testclient import TestClient


def _sample_report() -> Report:
    return Report(
        summary="Service degradation observed in shop-api.",
        impact="Moderate user impact.",
        probable_root_cause=ProbableRootCause(
            text="Elevated request latency due to connection bottleneck.",
            confidence=0.85,
        ),
        timeline=[],
        evidence_by_area={},
        recent_changes=[],
        recommended_actions=["Review connection pooling configuration."],
        specialists_status=[],
        skills_used=[],
        queries_appendix={},
        metadata={},
    )


@pytest.fixture(autouse=True)
def reset_dedup() -> Generator[None, None, None]:
    """Ensure deduplication registry is reset before and after each test."""
    clear()
    yield
    clear()


@pytest.fixture
def bridge_client() -> Generator[TestClient, None, None]:
    test_config = BridgeConfig(
        app_env="local",
        callback_url="https://bridge.example.com/callback/report",
        agent_sa="agent-sa@test-project.iam.gserviceaccount.com",
    )
    app.dependency_overrides[get_settings] = lambda: test_config
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_callback_dedup_duplicate_report_dropped(
    bridge_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending duplicate callback reports returns HTTP 200 duplicate_ignored without posting."""
    mock_post_report = MagicMock()
    monkeypatch.setattr(ServiceNowAdapter, "post_report", mock_post_report)

    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )
    headers = {"Authorization": f"Bearer {auth_token}"}

    payload = {
        "investigation_id": "inv-dedup-101",
        "incident_id": "inc-dedup-101",
        "incident_source": "servicenow",
        "incident_external_id": "SNOW-101",
        "incident_external_key": "INC00101",
        "status": "completed",
        "report": _sample_report().model_dump(mode="json"),
    }

    # 1. First callback delivery: accepted and processed
    resp1 = bridge_client.post("/callback/report", json=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json() == {"status": "ok"}
    assert mock_post_report.call_count == 1

    # 2. Second callback delivery (identical incident_id and status): dropped as duplicate
    resp2 = bridge_client.post("/callback/report", json=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json() == {
        "status": "duplicate_ignored",
        "incident_id": "inc-dedup-101",
    }
    # post_report must NOT be invoked again
    assert mock_post_report.call_count == 1


def test_callback_dedup_duplicate_failure_dropped(
    bridge_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sending duplicate failed callbacks returns duplicate_ignored and drops duplicate post."""
    mock_post_failure = MagicMock()
    monkeypatch.setattr(ServiceNowAdapter, "post_failure", mock_post_failure)

    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )
    headers = {"Authorization": f"Bearer {auth_token}"}

    payload = {
        "investigation_id": "inv-dedup-102",
        "incident_id": "inc-dedup-102",
        "incident_source": "servicenow",
        "incident_external_id": "SNOW-102",
        "incident_external_key": "INC00102",
        "status": "failed",
        "error": "Investigation timed out",
    }

    # 1. First failure delivery: accepted and processed
    resp1 = bridge_client.post("/callback/report", json=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json() == {"status": "ok"}
    assert mock_post_failure.call_count == 1

    # 2. Duplicate failure delivery: dropped as duplicate
    resp2 = bridge_client.post("/callback/report", json=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json() == {
        "status": "duplicate_ignored",
        "incident_id": "inc-dedup-102",
    }
    assert mock_post_failure.call_count == 1


def test_callback_dedup_retry_after_post_failure_redelivers(
    bridge_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ticket write-back must not mark the report as seen, so a retry re-posts it."""
    mock_post_report = MagicMock(side_effect=[RuntimeError("ticket write failed"), None])
    monkeypatch.setattr(ServiceNowAdapter, "post_report", mock_post_report)

    auth_token = create_mock_oidc_token(
        audience="https://bridge.example.com/callback/report",
        email="agent-sa@test-project.iam.gserviceaccount.com",
    )
    headers = {"Authorization": f"Bearer {auth_token}"}

    payload = {
        "investigation_id": "inv-dedup-103",
        "incident_id": "inc-dedup-103",
        "incident_source": "servicenow",
        "incident_external_id": "SNOW-103",
        "incident_external_key": "INC00103",
        "status": "completed",
        "report": _sample_report().model_dump(mode="json"),
    }

    # 1. First delivery: ticket write-back raises, bridge returns 500
    client = TestClient(app, raise_server_exceptions=False)
    resp1 = client.post("/callback/report", json=payload, headers=headers)
    assert resp1.status_code == 500
    assert mock_post_report.call_count == 1

    # 2. Agent retry: must re-attempt delivery rather than be dropped as a duplicate
    resp2 = bridge_client.post("/callback/report", json=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "ok"}
    assert mock_post_report.call_count == 2


def test_callback_dedup_ttl_expiration() -> None:
    """Entries expire after TTL and can be processed again."""
    registry = CallbackDedupRegistry(ttl_seconds=10.0)

    current_time = 1000.0

    def mock_time() -> float:
        return current_time

    with patch("bridge.app.core.dedup.time.monotonic", side_effect=mock_time):
        # Initial check
        assert registry.check_and_mark("inc-200", "completed") is True
        # Immediate duplicate
        assert registry.check_and_mark("inc-200", "completed") is False

        # Advance time by 5 seconds (within TTL)
        current_time = 1005.0
        assert registry.check_and_mark("inc-200", "completed") is False

        # Advance time past TTL (11 seconds)
        current_time = 1011.1
        assert registry.check_and_mark("inc-200", "completed") is True


def test_callback_dedup_clear_and_reset_helpers() -> None:
    """clear() and reset() purge cached deduplication entries."""
    registry = get_dedup_registry()

    assert registry.check_and_mark("inc-300", "completed") is True
    assert registry.check_and_mark("inc-300", "completed") is False

    # Calling clear() allows key to be recorded again
    registry.clear()
    assert registry.check_and_mark("inc-300", "completed") is True
    assert registry.check_and_mark("inc-300", "completed") is False

    # Calling reset() alias
    reset()
    assert registry.check_and_mark("inc-300", "completed") is True


def test_callback_dedup_thread_safety() -> None:
    """Concurrent check_and_mark calls from multiple threads permit exactly one winner."""
    registry = CallbackDedupRegistry(ttl_seconds=60.0)
    num_threads = 20
    results = []

    def attempt_mark() -> bool:
        return registry.check_and_mark("inc-concurrent", "completed")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(attempt_mark) for _ in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly one thread must return True, all other threads False
    assert results.count(True) == 1
    assert results.count(False) == num_threads - 1
