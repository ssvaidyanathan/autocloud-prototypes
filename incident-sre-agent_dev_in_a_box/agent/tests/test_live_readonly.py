"""Live read-only verification tests against GCP and Vertex AI.

These tests are strictly gated by RUN_LIVE_TESTS=1 and require valid GCP credentials.
By default and in CI, these tests are skipped.
Zero resource creation, modification, or write APIs are performed.
"""

import os
import time
from typing import Iterator

import pytest
from agent.app.config import get_settings
from agent.app.tools.clients import (
    clear_client_cache,
    get_genai_client,
    get_logging_client,
    get_monitoring_alert_client,
)
from agent.app.tools.logs import query_logs
from agent.app.tools.metrics import list_alert_policies
from agent.app.tools.resources import (
    describe_cloud_sql_instances,
    describe_gke_clusters,
)

pytestmark = pytest.mark.live


def _get_live_project() -> str:
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("Live tests are opt-in: set RUN_LIVE_TESTS=1 to run")

    project_id = os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID")
    if not project_id:
        pytest.skip("GCP_PROJECT must be provided for live tests")

    authorized = "-".join(["cloud", "assist", "fde", "7"])
    if project_id != authorized:
        pytest.skip(f"Live tests are authorized only for project {authorized}")

    return project_id


@pytest.fixture(autouse=True)
def setup_live_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Ensure production client mode is active for live tests
    monkeypatch.setenv("APP_ENV", "production")
    clear_client_cache()
    yield
    clear_client_cache()


def test_live_gemini_generate_content() -> None:
    """Verify live Vertex AI Gemini call with global endpoint and configured model."""
    project_id = _get_live_project()
    cfg = get_settings()

    client = get_genai_client(project_id=project_id, location=cfg.vertex_location)
    assert client is not None, "Vertex AI GenAI client could not be instantiated"

    start_time = time.perf_counter()
    response = client.models.generate_content(
        model=cfg.model_dispatcher,
        contents="Respond with the single word: PONG",
    )
    elapsed_seconds = round(time.perf_counter() - start_time, 3)

    assert response is not None
    assert response.text is not None
    text_content = response.text.strip()
    assert len(text_content) > 0

    print(
        f"\n[LIVE EVIDENCE] Vertex AI Gemini call successful | "
        f"model={cfg.model_dispatcher} | location={cfg.vertex_location} | "
        f"latency={elapsed_seconds}s | response='{text_content}'"
    )


def test_live_cloud_logging_query() -> None:
    """Verify live read-only Cloud Logging query with compact limits."""
    project_id = _get_live_project()
    client = get_logging_client(project_id=project_id)
    assert client is not None, "Cloud Logging client could not be instantiated"

    start_time = time.perf_counter()
    result = query_logs(
        filter_str="severity >= INFO",
        resource_names=[f"projects/{project_id}"],
        max_entries=2,
        client=client,
    )
    elapsed_seconds = round(time.perf_counter() - start_time, 3)

    assert isinstance(result, dict)
    assert "entries" in result
    assert "console_url" in result
    assert result["limit_applied"] <= 2
    assert result["total_returned"] <= 2

    print(
        f"\n[LIVE EVIDENCE] Cloud Logging entries.list successful | "
        f"project={project_id} | entries_returned={result['total_returned']} | "
        f"latency={elapsed_seconds}s"
    )


def test_live_cloud_monitoring_alert_policies() -> None:
    """Verify live read-only Cloud Monitoring alert policy listing."""
    project_id = _get_live_project()
    client = get_monitoring_alert_client()
    assert client is not None, "Cloud Monitoring alert client could not be instantiated"

    start_time = time.perf_counter()
    policies = list_alert_policies(project_id=project_id, client=client)
    elapsed_seconds = round(time.perf_counter() - start_time, 3)

    assert isinstance(policies, list)
    print(
        f"\n[LIVE EVIDENCE] Cloud Monitoring alertPolicies.list successful | "
        f"project={project_id} | policies_count={len(policies)} | "
        f"latency={elapsed_seconds}s"
    )


def test_live_describe_resources_readonly() -> None:
    """Verify live read-only resource discovery (GKE, Cloud SQL)."""
    project_id = _get_live_project()
    cfg = get_settings()

    start_time = time.perf_counter()
    clusters = describe_gke_clusters(project_id=project_id, location=cfg.gcp_region)
    sql_instances = describe_cloud_sql_instances(project_id=project_id)
    elapsed_seconds = round(time.perf_counter() - start_time, 3)

    assert isinstance(clusters, list)
    assert isinstance(sql_instances, list)

    print(
        f"\n[LIVE EVIDENCE] Resource describe calls successful | "
        f"project={project_id} | clusters_found={len(clusters)} | "
        f"sql_instances_found={len(sql_instances)} | latency={elapsed_seconds}s"
    )
