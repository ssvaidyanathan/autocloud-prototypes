"""Unit tests for Stage 3 Two-Pass Agentic pattern in LogsSpecialist."""

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional

import pytest
from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.specialists.logs import LogsSpecialist
from agent.app.tools.registry import default_query_cache
from agent.app.triage import InvestigationPlan
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _make_incident(service_hint: str = "shop-api") -> Incident:
    return Incident(
        id="inc-test-logs-01",
        source="generic",
        external_id="EXT-101",
        external_key="INC-101",
        title="Spike in 500 errors on shop-api",
        description="Checkout endpoint is returning HTTP 500 intermittently",
        severity="P1",
        service_hint=service_hint,
        opened_at=datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc),
    )


def _make_plan(service: str = "shop-api") -> InvestigationPlan:
    t = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    return InvestigationPlan(
        affected_service=service,
        symptoms=["HTTP 500 spike", "Database pool exhaustion"],
        initial_hypotheses=["Database connection saturation"],
        selected_specialists=["logs"],
        start_time=t,
        end_time=t,
    )


class FakeEntry:
    def __init__(
        self,
        severity: str = "ERROR",
        text_payload: str = "DB connection timeout",
        timestamp: Optional[datetime] = None,
        log_name: str = "projects/test-proj/logs/shop-api",
    ) -> None:
        self.severity = severity
        self.text_payload = text_payload
        self.payload = text_payload
        self.timestamp = timestamp
        self.log_name = log_name
        self.resource = type("Resource", (), {"type": "cloud_run_revision"})()
        self.http_request = None
        self.httpRequest = None


class FakeGroup:
    def __init__(self, group_id: str, message: str, count: int) -> None:
        self.group_id = group_id
        self.message = message
        self.count = count


class FakeGcpLoggingClient:
    """Hermetic GCP Logging client returning fake entries and groups."""

    def __init__(
        self,
        entries: Optional[List[Any]] = None,
        groups: Optional[List[Any]] = None,
    ) -> None:
        self.entries = (
            entries
            if entries is not None
            else [
                FakeEntry(
                    severity="ERROR",
                    text_payload="Baseline DB connection pool timeout",
                    timestamp=datetime(2026, 10, 12, 9, 55, 0, tzinfo=timezone.utc),
                ),
                FakeEntry(
                    severity="WARNING",
                    text_payload="Scenario 2: Injecting 5.0s latency delay",
                    timestamp=datetime(2026, 10, 12, 9, 58, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        self.groups = (
            groups
            if groups is not None
            else [FakeGroup("grp-101", "DB connection pool timeout", 15)]
        )
        self.list_entries_calls: int = 0
        self.list_group_stats_calls: int = 0

    def list_entries(self, **kwargs: Any) -> List[Any]:
        self.list_entries_calls += 1
        return list(self.entries)

    def list_group_stats(self, **kwargs: Any) -> List[Any]:
        self.list_group_stats_calls += 1
        return list(self.groups)


class TwoPassSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            # Turn 1: load domain skill
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "cloud-logging-queries"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            # Turn 2: query logs tool call
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="query_logs",
                            args={"service": "shop-api", "severity": "ERROR"},
                        )
                    ],
                )
            )
        else:
            # Turn 3: structured output Findings
            json_text = (
                '{"summary": "Pass 1 drill-down identified database connection '
                'pool exhaustion on shop-api.", '
                '"confidence": 0.95, '
                '"anomalies": ["Connection pool exhausted (50/50 connections active)"], '
                '"evidence": [{"kind": "log_entry", '
                '"excerpt": "[ERROR] Pool exhausted in checkout", '
                '"timestamp": "2026-10-12T09:59:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


@pytest.mark.asyncio
async def test_two_pass_execution_success_with_stubbed_llm() -> None:
    """Verify Pass 1 (LLM drill-down) and Pass 2 (baseline sweep) succeed and merge."""
    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=TwoPassSuccessLlm(),
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

    # 1. Pass 1 success attributes
    assert (
        findings.summary
        == "Pass 1 drill-down identified database connection pool exhaustion on shop-api."
    )
    assert findings.confidence == 0.95
    assert findings.fallback_used is False

    # 2. Skill tracking on Findings
    assert findings.skill_first is True
    assert "cloud-logging-queries" in findings.skills_used

    # 3. Evidence deduplicated and merged from Pass 1 + Pass 2
    excerpts = [e.excerpt for e in findings.evidence]
    # Pass 1 evidence present
    assert any("Pool exhausted in checkout" in e for e in excerpts)
    # Pass 2 baseline sweep evidence also present
    assert any("Baseline DB connection pool timeout" in e for e in excerpts)

    # 4. Anomalies merged from both passes
    assert any("Connection pool exhausted" in a for a in findings.anomalies)


@pytest.mark.asyncio
async def test_turn_1_load_skill_produces_skill_first_true() -> None:
    """Verify Turn 1 load_skill sets skill_first=True and populates skills_used."""
    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=TwoPassSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )
    assert findings.skill_first is True
    assert findings.skills_used == ["cloud-logging-queries"]


@pytest.mark.asyncio
async def test_non_skill_first_turn_1_produces_skill_first_false() -> None:
    """Verify Turn 1 invoking a query tool instead of load_skill produces skill_first=False."""

    class QueryFirstLlm(BaseLlm):
        model: str = "stub"
        _turn: int = 0

        def __init__(self) -> None:
            super().__init__(model="stub")
            self._turn = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            if self._turn == 1:
                # Query first instead of load_skill
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="query_logs",
                                args={"service": "shop-api", "severity": "ERROR"},
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
                                args={"skill_name": "cloud-logging-queries"},
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(text='{"summary": "Late skill load", "confidence": 0.8}')
                        ],
                    )
                )

    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
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
    assert "cloud-logging-queries" in findings.skills_used


@pytest.mark.asyncio
async def test_budget_enforcement_max_llm_calls() -> None:
    """Verify execution halts at RunConfig(max_llm_calls=8) with graceful fallback."""

    class InfiniteTurnsLlm(BaseLlm):
        model: str = "stub"
        call_count: int = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self.call_count += 1
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="list_log_names",
                            args={},
                        )
                    ],
                )
            )

    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=InfiniteTurnsLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    # Exceeded max_llm_calls budget -> graceful fallback to Pass 2 baseline sweep
    assert findings.fallback_used is True
    assert findings.confidence == 0.85
    assert len(findings.evidence) > 0


@pytest.mark.asyncio
async def test_budget_enforcement_tool_call_cap_at_5() -> None:
    """Verify tool call cap of max 5 is enforced via callback counter with graceful fallback."""

    class ExcessiveToolsLlm(BaseLlm):
        model: str = "stub"
        _turn: int = 0

        def __init__(self) -> None:
            super().__init__(model="stub")
            self._turn = 0

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            self._turn += 1
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="query_logs",
                            args={
                                "service": f"shop-api-{self._turn}",
                                "severity": "ERROR",
                            },
                        )
                    ],
                )
            )

    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=ExcessiveToolsLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert findings.fallback_used is True
    # The tool cap blocked further execution at 5 calls
    assert specialist.skill_tracker.tool_call_count == 5


@pytest.mark.asyncio
async def test_budget_enforcement_timeout_fallback() -> None:
    """Verify 60s wall clock timeout enforcement via asyncio.wait_for falls back to Pass 2."""

    class SlowHangingLlm(BaseLlm):
        model: str = "stub"

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            await asyncio.sleep(0.5)
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text='{"summary": "Never finished"}')],
                )
            )

    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=SlowHangingLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    # Trigger with a tight timeout to verify wait_for aborts Pass 1
    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now,
        end_time=now,
        timeout_seconds=0.05,
    )

    assert findings.fallback_used is True
    assert findings.confidence == 0.85
    assert len(findings.evidence) > 0


@pytest.mark.asyncio
async def test_cache_reuse_between_pass_1_and_pass_2() -> None:
    """Verify in-memory query cache reuses identical queries without redundant GCP calls."""

    class CacheCheckLlm(BaseLlm):
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
                                args={"skill_name": "cloud-logging-queries"},
                            )
                        ],
                    )
                )
            elif self._turn == 2:
                # Issue the exact query that Pass 2 baseline sweep runs
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_function_call(
                                name="summarize_error_groups",
                                args={
                                    "project_id": "test-proj",
                                    "service_name": "shop-api",
                                    "time_window_hours": 1,
                                    "start_time": "2026-10-12T09:00:00+00:00",
                                    "end_time": "2026-10-12T10:00:00+00:00",
                                },
                            )
                        ],
                    )
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"summary": "Cache verified", "confidence": 0.9}')],
                    )
                )

    default_query_cache.clear_sync()
    client = FakeGcpLoggingClient()

    specialist = LogsSpecialist(
        project_id="test-proj",
        model=CacheCheckLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=start,
        end_time=now,
    )

    assert findings.fallback_used is False
    # Both Pass 1 and Pass 2 invoked summarize_error_groups, but cache prevented duplicate GCP call
    assert client.list_group_stats_calls == 1


@pytest.mark.asyncio
async def test_offline_fallback_floor_when_llm_unavailable() -> None:
    """Verify offline fallback floor gracefully degrades to Pass 2 when no LLM is configured."""
    client = FakeGcpLoggingClient()
    # Explicitly no model configured / offline
    specialist = LogsSpecialist(
        project_id="test-proj",
        model="",
        client=client,
    )
    # Ensure llm_agent model is also empty string
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
    assert findings.confidence == 0.85
    assert len(findings.evidence) > 0
    assert not findings.errors


def test_sync_run_investigation_wrapper_parity() -> None:
    """Verify synchronous run_investigation wrapper executes and matches contract."""
    client = FakeGcpLoggingClient()
    specialist = LogsSpecialist(
        project_id="test-proj",
        model=TwoPassSuccessLlm(),
        client=client,
    )
    incident = _make_incident("shop-api")
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    # Invoke via synchronous wrapper
    findings = specialist.run_investigation(
        incident=incident,
        start_time=now,
        end_time=now,
    )

    assert isinstance(findings, Findings)
    assert findings.fallback_used is False
    assert findings.skill_first is True
    assert "cloud-logging-queries" in findings.skills_used
