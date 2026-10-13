"""Hermetic unit tests for Two-Pass Agentic pattern in remaining specialists."""

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist
from agent.app.tools.changes import query_audit_logs
from agent.app.tools.logs import query_logs, summarize_error_groups
from agent.app.tools.metrics import compare_to_baseline, query_time_series
from agent.app.triage import InvestigationPlan
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _make_incident(service_hint: str = "shop-api") -> Incident:
    return Incident(
        id="inc-test-agentic-01",
        source="generic",
        external_id="EXT-201",
        external_key="INC-201",
        title="Spike in errors and latency on shop-api",
        description="Checkout endpoint is degraded",
        severity="P1",
        service_hint=service_hint,
        opened_at=datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc),
    )


def _make_plan(service: str = "shop-api") -> InvestigationPlan:
    t = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    return InvestigationPlan(
        affected_service=service,
        symptoms=["5xx error spike", "Latency elevation"],
        initial_hypotheses=["Recent deployment", "High latency"],
        selected_specialists=["metrics", "changes", "resources"],
        start_time=datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc),
        end_time=t,
    )


# ---------------------------------------------------------------------------
# MetricsSpecialist Agentic Tests
# ---------------------------------------------------------------------------


class MetricsSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for Metrics."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "cloud-monitoring-metrics"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="query_time_series",
                            args={
                                "service": "shop-api",
                                "lookback_minutes": 60,
                            },
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "Metrics Pass 1 identified 5xx error rate surge.", '
                '"confidence": 0.92, '
                '"anomalies": ["5xx error rate elevated by +450% over baseline"], '
                '"evidence": [{"kind": "metric_anomaly", '
                '"excerpt": "5xx surge to 12.5 req/s", '
                '"timestamp": "2026-10-12T09:45:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


def _make_fake_metrics_client() -> MagicMock:
    client = MagicMock()
    inc = MagicMock()
    inc.incident_id = "alert-999"
    inc.policy_name = "High Error Rate"
    inc.summary = "5xx error rate above threshold"
    client.list_incidents.return_value = [inc]
    client.list_alert_policies.return_value = []

    ts_5xx = MagicMock()
    pt_5xx = MagicMock()
    pt_5xx.value.double_value = 10.0
    ts_5xx.points = [pt_5xx]

    ts_base = MagicMock()
    pt_base = MagicMock()
    pt_base.value.double_value = 1.0
    ts_base.points = [pt_base]

    ts_lat = MagicMock()
    pt_lat = MagicMock()
    pt_lat.value.double_value = 120.0
    ts_lat.points = [pt_lat]

    client.list_time_series.return_value = [ts_5xx]
    return client


@pytest.mark.asyncio
async def test_metrics_two_pass_success_with_stubbed_llm() -> None:
    client = _make_fake_metrics_client()
    specialist = MetricsSpecialist(
        project_id="test-proj",
        model=MetricsSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    plan = _make_plan("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=start,
        end_time=now,
        plan=plan,
    )

    assert "Metrics Pass 1 identified 5xx error rate surge" in findings.summary
    assert findings.confidence == 0.92
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "cloud-monitoring-metrics" in findings.skills_used

    excerpts = [e.excerpt for e in findings.evidence]
    assert any("5xx surge to 12.5 req/s" in e for e in excerpts)
    assert any("High Error Rate" in e for e in excerpts)


@pytest.mark.asyncio
async def test_metrics_timeout_fallback() -> None:
    class SlowLlm(BaseLlm):
        model: str = "stub"

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            await asyncio.sleep(0.5)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="{}")]))

    client = _make_fake_metrics_client()
    specialist = MetricsSpecialist(
        project_id="test-proj",
        model=SlowLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
        timeout_seconds=0.05,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


@pytest.mark.asyncio
async def test_metrics_offline_fallback() -> None:
    client = _make_fake_metrics_client()
    specialist = MetricsSpecialist(
        project_id="test-proj",
        model="",
        client=client,
    )
    if specialist.llm_agent is not None:
        specialist.llm_agent.model = ""

    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


def test_metrics_sync_wrapper() -> None:
    client = _make_fake_metrics_client()
    specialist = MetricsSpecialist(
        project_id="test-proj",
        model=MetricsSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = specialist.run_investigation(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert isinstance(findings, Findings)
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "cloud-monitoring-metrics" in findings.skills_used


# ---------------------------------------------------------------------------
# ChangesSpecialist Agentic Tests
# ---------------------------------------------------------------------------


class ChangesSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for Changes."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "change-correlation"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="query_audit_logs",
                            args={"project_id": "test-proj", "service_name": "shop-api"},
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "Changes Pass 1 correlated bad deployment with error onset.", '
                '"confidence": 0.90, '
                '"anomalies": ["Revision deployment shop-api-v2-bad at 09:50 UTC"], '
                '"evidence": [{"kind": "revision", '
                '"excerpt": "shop-api-v2-bad deployed by developer@example.com", '
                '"timestamp": "2026-10-12T09:50:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


def _make_fake_changes_client() -> MagicMock:
    client = MagicMock()
    # Mock audit log
    audit_entry = MagicMock()
    audit_entry.timestamp = datetime(2026, 10, 12, 9, 50, 0, tzinfo=timezone.utc)
    audit_entry.proto_payload = {
        "methodName": "google.cloud.run.v1.Services.ReplaceService",
        "authenticationInfo": {"principalEmail": "ci-deployer@example.com"},
        "resourceName": "services/shop-api",
        "status": {"code": 0},
    }
    client.list_entries.return_value = [audit_entry]

    # Mock revision
    rev = MagicMock()
    rev.name = "shop-api-v2"
    rev.create_time = datetime(2026, 10, 12, 9, 50, 0, tzinfo=timezone.utc)
    rev.traffic_percent = 100
    rev.template.containers = [MagicMock(image="gcr.io/test-proj/shop-api:v2")]
    client.list_revisions.return_value = [rev]

    # Mock build
    client.list_builds.return_value = MagicMock(builds=[])
    return client


@pytest.mark.asyncio
async def test_changes_two_pass_success_with_stubbed_llm() -> None:
    client = _make_fake_changes_client()
    specialist = ChangesSpecialist(
        project_id="test-proj",
        model=ChangesSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    plan = _make_plan("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=start,
        end_time=now,
        plan=plan,
    )

    assert "Changes Pass 1 correlated bad deployment" in findings.summary
    assert findings.confidence == 0.90
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "change-correlation" in findings.skills_used

    excerpts = [e.excerpt for e in findings.evidence]
    assert any("shop-api-v2-bad deployed" in e for e in excerpts)
    assert any("Action google.cloud.run.v1.Services.ReplaceService" in e for e in excerpts)


@pytest.mark.asyncio
async def test_changes_timeout_fallback() -> None:
    class SlowLlm(BaseLlm):
        model: str = "stub"

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            await asyncio.sleep(0.5)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="{}")]))

    client = _make_fake_changes_client()
    specialist = ChangesSpecialist(
        project_id="test-proj",
        model=SlowLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
        timeout_seconds=0.05,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


@pytest.mark.asyncio
async def test_changes_offline_fallback() -> None:
    client = _make_fake_changes_client()
    specialist = ChangesSpecialist(
        project_id="test-proj",
        model="",
        client=client,
    )
    if specialist.llm_agent is not None:
        specialist.llm_agent.model = ""

    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


def test_changes_sync_wrapper() -> None:
    client = _make_fake_changes_client()
    specialist = ChangesSpecialist(
        project_id="test-proj",
        model=ChangesSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = specialist.run_investigation(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert isinstance(findings, Findings)
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "change-correlation" in findings.skills_used


# ---------------------------------------------------------------------------
# ResourcesSpecialist Agentic Tests
# ---------------------------------------------------------------------------


class ResourcesSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for Resources."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "cloud-run-troubleshooting"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="describe_cloud_run_service",
                            args={"project_id": "test-proj", "service_name": "shop-api"},
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "Resources Pass 1 found memory limit misconfiguration.", '
                '"confidence": 0.88, '
                '"anomalies": ["Memory limit restricted to 256Mi causing OOM risk"], '
                '"evidence": [{"kind": "cloud_run_service", '
                '"excerpt": "Cloud Run service configured with cpu=1, mem=256Mi", '
                '"timestamp": "2026-10-12T09:00:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


def _make_fake_resources_client() -> MagicMock:
    client = MagicMock()
    # Mock Cloud Run
    svc = MagicMock()
    svc.uri = "https://shop-api.run.app"
    template = MagicMock()
    container = MagicMock()
    container.resources.limits = {"cpu": "1", "memory": "256Mi"}
    container.env = [{"name": "DATABASE_URL"}, {"name": "PORT"}]
    template.containers = [container]
    template.max_instance_request_concurrency = 80
    svc.template = template
    client.get_service.return_value = svc

    # Mock Cloud SQL
    sql_inst = MagicMock()
    sql_inst.name = "shop-db"
    sql_inst.database_version = "POSTGRES_15"
    sql_inst.state = "RUNNABLE"
    client.list_instances.return_value = [sql_inst]

    client.list_clusters.return_value = MagicMock(clusters=[])
    client.aggregated_list.return_value = []
    return client


@pytest.mark.asyncio
async def test_resources_two_pass_success_with_stubbed_llm() -> None:
    client = _make_fake_resources_client()
    specialist = ResourcesSpecialist(
        project_id="test-proj",
        model=ResourcesSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    plan = _make_plan("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=start,
        end_time=now,
        plan=plan,
    )

    assert "Resources Pass 1 found memory limit misconfiguration" in findings.summary
    assert findings.confidence == 0.88
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "cloud-run-troubleshooting" in findings.skills_used

    excerpts = [e.excerpt for e in findings.evidence]
    assert any("cpu=1, mem=256Mi" in e for e in excerpts)
    assert any("Cloud SQL shop-db" in e for e in excerpts)


@pytest.mark.asyncio
async def test_resources_timeout_fallback() -> None:
    class SlowLlm(BaseLlm):
        model: str = "stub"

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            await asyncio.sleep(0.5)
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="{}")]))

    client = _make_fake_resources_client()
    specialist = ResourcesSpecialist(
        project_id="test-proj",
        model=SlowLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
        timeout_seconds=0.05,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


@pytest.mark.asyncio
async def test_resources_offline_fallback() -> None:
    client = _make_fake_resources_client()
    specialist = ResourcesSpecialist(
        project_id="test-proj",
        model="",
        client=client,
    )
    if specialist.llm_agent is not None:
        specialist.llm_agent.model = ""

    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert findings.fallback_used is True
    assert len(findings.evidence) > 0


def test_resources_sync_wrapper() -> None:
    client = _make_fake_resources_client()
    specialist = ResourcesSpecialist(
        project_id="test-proj",
        model=ResourcesSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = specialist.run_investigation(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert isinstance(findings, Findings)
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "cloud-run-troubleshooting" in findings.skills_used


@pytest.mark.asyncio
async def test_metrics_non_skill_first_turn_1() -> None:
    class QueryFirstLlm(BaseLlm):
        model: str = "stub"
        _turn: int = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            if self._turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="query_time_series",
                                args={"service": "shop-api"},
                            )
                        ],
                    )
                )
            elif self._turn == 2:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="load_skill",
                                args={"skill_name": "cloud-monitoring-metrics"},
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"summary": "ok", "confidence": 0.8}')],
                    )
                )

    client = _make_fake_metrics_client()
    specialist = MetricsSpecialist(
        project_id="test-proj",
        model=QueryFirstLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )
    assert findings.skill_first is False
    assert "cloud-monitoring-metrics" in findings.skills_used


@pytest.mark.asyncio
async def test_changes_non_skill_first_turn_1() -> None:
    class QueryFirstLlm(BaseLlm):
        model: str = "stub"
        _turn: int = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            if self._turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="query_audit_logs",
                                args={"service_name": "shop-api"},
                            )
                        ],
                    )
                )
            elif self._turn == 2:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="load_skill",
                                args={"skill_name": "change-correlation"},
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"summary": "ok", "confidence": 0.8}')],
                    )
                )

    client = _make_fake_changes_client()
    specialist = ChangesSpecialist(
        project_id="test-proj",
        model=QueryFirstLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )
    assert findings.skill_first is False
    assert "change-correlation" in findings.skills_used


@pytest.mark.asyncio
async def test_resources_non_skill_first_turn_1() -> None:
    class QueryFirstLlm(BaseLlm):
        model: str = "stub"
        _turn: int = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            if self._turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="describe_cloud_run_service",
                                args={"service_name": "shop-api"},
                            )
                        ],
                    )
                )
            elif self._turn == 2:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="load_skill",
                                args={"skill_name": "cloud-run-troubleshooting"},
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"summary": "ok", "confidence": 0.8}')],
                    )
                )

    client = _make_fake_resources_client()
    specialist = ResourcesSpecialist(
        project_id="test-proj",
        model=QueryFirstLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )
    assert findings.skill_first is False
    assert "cloud-run-troubleshooting" in findings.skills_used


# ---------------------------------------------------------------------------
# Datetime Boundary Normalization Regression Tests
# ---------------------------------------------------------------------------


def _make_fake_logs_client() -> MagicMock:
    client = MagicMock()
    entry1 = MagicMock()
    entry1.timestamp = datetime(2026, 10, 12, 9, 30, 0, tzinfo=timezone.utc)
    entry1.severity = "ERROR"
    entry1.payload = "Database connection pool timeout"
    entry1.log_name = "projects/test-proj/logs/app"
    entry1.resource.type = "cloud_run_revision"
    client.list_entries.return_value = [entry1]

    grp = MagicMock()
    grp.group_id = "grp-123"
    grp.count = 42
    grp.representative.message = "DB connection pool timeout"
    grp.first_seen_time = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)
    grp.last_seen_time = datetime(2026, 10, 12, 9, 50, 0, tzinfo=timezone.utc)
    client.list_group_stats.return_value = [grp]
    return client


def test_tools_boundary_accept_iso_datetime_strings() -> None:
    """Verify tool functions accept ISO-8601 strings without
    'AttributeError: 'str' object has no attribute 'tzinfo' / 'isoformat''.
    """
    logs_client = _make_fake_logs_client()
    logs_res = query_logs(
        filter_str="severity>=ERROR",
        resource_names=["projects/test-proj"],
        start_time="2026-10-12T09:00:00Z",
        end_time="2026-10-12T10:00:00Z",
        client=logs_client,
    )
    assert logs_res["total_returned"] == 1
    assert (
        'timestamp >= "2026-10-12T09:00:00+00:00"'
        in logs_client.list_entries.call_args.kwargs["filter_"]
    )

    error_groups_res = summarize_error_groups(
        project_id="test-proj",
        start_time="2026-10-12T09:00:00Z",
        end_time="2026-10-12T10:00:00Z",
        client=logs_client,
    )
    assert error_groups_res["total_groups"] == 1

    metrics_client = _make_fake_metrics_client()
    ts_res = query_time_series(
        filter_str='metric.type = "run.googleapis.com/request_count"',
        project_id="test-proj",
        start_time="2026-10-12T09:00:00Z",
        end_time="2026-10-12T10:00:00Z",
        client=metrics_client,
    )
    assert ts_res["total_points"] >= 1

    baseline_res = compare_to_baseline(
        filter_str="metric.type=run.googleapis.com/request_count",
        project_id="test-proj",
        current_start="2026-10-12T09:00:00Z",
        current_end="2026-10-12T10:00:00Z",
        baseline_start="2026-10-12T08:00:00Z",
        baseline_end="2026-10-12T09:00:00Z",
        client=metrics_client,
    )
    assert "current_period" in baseline_res
    assert "baseline_period" in baseline_res

    changes_client = _make_fake_changes_client()
    audit_res = query_audit_logs(
        project_id="test-proj",
        service_name="shop-api",
        start_time="2026-10-12T09:00:00Z",
        end_time="2026-10-12T10:00:00Z",
        client=changes_client,
    )
    assert isinstance(audit_res, list)


@pytest.mark.asyncio
async def test_specialists_investigation_accept_iso_datetime_strings() -> None:
    """Verify all 4 specialists accept ISO-8601 string datetimes at entry
    and pass through cleanly.
    """
    incident = _make_incident("shop-api")
    plan = _make_plan("shop-api")
    start_str = "2026-10-12T09:00:00Z"
    end_str = "2026-10-12T10:00:00Z"

    logs_spec = LogsSpecialist(
        project_id="test-proj",
        model="",  # Baseline sweep path
        client=_make_fake_logs_client(),
    )
    findings_logs = await logs_spec.run_investigation_async(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(findings_logs, Findings)
    assert not any("AttributeError" in err for err in findings_logs.errors)

    metrics_spec = MetricsSpecialist(
        project_id="test-proj",
        model="",
        client=_make_fake_metrics_client(),
    )
    findings_metrics = await metrics_spec.run_investigation_async(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(findings_metrics, Findings)
    assert not any("AttributeError" in err for err in findings_metrics.errors)

    changes_spec = ChangesSpecialist(
        project_id="test-proj",
        model="",
        client=_make_fake_changes_client(),
    )
    findings_changes = await changes_spec.run_investigation_async(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(findings_changes, Findings)
    assert not any("AttributeError" in err for err in findings_changes.errors)

    resources_spec = ResourcesSpecialist(
        project_id="test-proj",
        model="",
        client=_make_fake_resources_client(),
    )
    findings_resources = await resources_spec.run_investigation_async(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(findings_resources, Findings)
    assert not any("AttributeError" in err for err in findings_resources.errors)

    # Test sync investigate & async investigate_async methods with string datetimes
    f_sync = logs_spec.investigate(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(f_sync, Findings)
    assert logs_spec.last_findings is f_sync

    f_async = await metrics_spec.investigate_async(
        incident=incident,
        start_time=start_str,
        end_time=end_str,
        plan=plan,
    )
    assert isinstance(f_async, Findings)
    assert metrics_spec.last_findings is f_async
