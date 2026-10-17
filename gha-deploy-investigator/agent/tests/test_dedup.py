import threading
import time
from unittest.mock import MagicMock

from agent.app.contracts.report import Report
from agent.app.contracts.request import InvestigationRequest
from agent.app.contracts.result import InvestigationResult
from agent.app.main import (
    InvestigationDeduplicator,
    app,
    set_deduplicator,
    set_dispatcher,
)
from starlette.testclient import TestClient


def _make_dummy_result(key: str) -> InvestigationResult:
    return InvestigationResult(
        investigation_id=f"inv-{key}",
        incident_id=f"inc-{key}",
        incident_source="github",
        incident_external_id=key,
        incident_external_key=f"repo#{key}",
        status="completed",
        report=Report(summary=f"Report for {key}"),
    )


def test_deduplicator_returns_cached_result() -> None:
    dedup = InvestigationDeduplicator(ttl_seconds=300.0)
    mock_compute = MagicMock(side_effect=lambda: _make_dummy_result("run-1"))

    res1 = dedup.deduplicate("github:123:1", mock_compute)
    res2 = dedup.deduplicate("github:123:1", mock_compute)

    assert res1.investigation_id == "inv-run-1"
    assert res2.investigation_id == "inv-run-1"
    assert mock_compute.call_count == 1


def test_deduplicator_concurrent_inflight_waits() -> None:
    dedup = InvestigationDeduplicator(ttl_seconds=300.0)
    call_count = 0
    lock = threading.Lock()

    def slow_compute() -> InvestigationResult:
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.1)
        return _make_dummy_result("concurrent")

    results: list[InvestigationResult] = []

    def worker() -> None:
        r = dedup.deduplicate("github:999:1", slow_compute)
        results.append(r)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)

    t1.start()
    time.sleep(0.01)  # Ensure t1 registers in-flight first
    t2.start()

    t1.join()
    t2.join()

    assert len(results) == 2
    assert results[0].investigation_id == "inv-concurrent"
    assert results[1].investigation_id == "inv-concurrent"
    assert call_count == 1


def test_deduplicator_ttl_expiry() -> None:
    dedup = InvestigationDeduplicator(ttl_seconds=0.05)
    mock_compute = MagicMock(side_effect=lambda: _make_dummy_result("expired"))

    res1 = dedup.deduplicate("github:123:1", mock_compute)
    assert res1.investigation_id == "inv-expired"
    assert mock_compute.call_count == 1

    time.sleep(0.06)

    res2 = dedup.deduplicate("github:123:1", mock_compute)
    assert res2.investigation_id == "inv-expired"
    assert mock_compute.call_count == 2


def test_deduplicator_error_propagation_does_not_cache_error() -> None:
    dedup = InvestigationDeduplicator(ttl_seconds=300.0)

    def failing_compute() -> InvestigationResult:
        raise ValueError("Synthesis engine failure")

    try:
        dedup.deduplicate("github:fail:1", failing_compute)
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert "Synthesis engine failure" in str(exc)

    # After failure, retry should run compute again rather than returning stale cached error
    retry_mock = MagicMock(side_effect=lambda: _make_dummy_result("retry"))
    res = dedup.deduplicate("github:fail:1", retry_mock)
    assert res.investigation_id == "inv-retry"
    assert retry_mock.call_count == 1


def test_investigate_endpoint_deduplication(sample_request: InvestigationRequest) -> None:
    client = TestClient(app)
    mock_dispatcher = MagicMock()
    mock_dispatcher.investigate.side_effect = lambda req: _make_dummy_result(
        req.incident.external_id
    )

    test_dedup = InvestigationDeduplicator(ttl_seconds=300.0)
    set_deduplicator(test_dedup)
    set_dispatcher(mock_dispatcher)

    try:
        request_data = sample_request.model_dump(mode="json")

        # First delivery
        resp1 = client.post("/investigate", json=request_data)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["incident_external_id"] == sample_request.incident.external_id

        # Second duplicate delivery 10 seconds later
        resp2 = client.post("/investigate", json=request_data)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["incident_external_id"] == sample_request.incident.external_id

        # Dispatcher multi-agent investigation was only invoked once!
        assert mock_dispatcher.investigate.call_count == 1
    finally:
        set_deduplicator(None)
        set_dispatcher(None)


def test_investigate_endpoint_different_attempts_not_deduplicated(
    sample_request: InvestigationRequest,
) -> None:
    client = TestClient(app)
    mock_dispatcher = MagicMock()
    mock_dispatcher.investigate.side_effect = lambda req: _make_dummy_result(
        f"{req.incident.external_id}-attempt-{req.incident.github_run_attempt}"
    )

    test_dedup = InvestigationDeduplicator(ttl_seconds=300.0)
    set_deduplicator(test_dedup)
    set_dispatcher(mock_dispatcher)

    try:
        req_attempt1 = sample_request.model_dump(mode="json")
        req_attempt1["incident"]["github_run_attempt"] = 1
        resp1 = client.post("/investigate", json=req_attempt1)
        assert resp1.status_code == 200
        assert (
            resp1.json()["investigation_id"]
            == f"inv-{sample_request.incident.external_id}-attempt-1"
        )

        req_attempt2 = sample_request.model_dump(mode="json")
        req_attempt2["incident"]["github_run_attempt"] = 2
        resp2 = client.post("/investigate", json=req_attempt2)
        assert resp2.status_code == 200
        assert (
            resp2.json()["investigation_id"]
            == f"inv-{sample_request.incident.external_id}-attempt-2"
        )

        # Dispatcher was invoked for each distinct run attempt
        assert mock_dispatcher.investigate.call_count == 2
    finally:
        set_deduplicator(None)
        set_dispatcher(None)
