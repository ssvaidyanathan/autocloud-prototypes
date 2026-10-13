"""Opt-in live integration test for LogsSpecialist running through ADK Runner with real Gemini.

This test is strictly opt-in, marked with @pytest.mark.live, and skipped by default
in CI and local environments unless explicitly opted in with RUN_LIVE_TESTS=1 and
valid Gemini or Vertex AI credentials are provided.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from agent.app.config import get_settings
from agent.app.contracts.findings import Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.request import InvestigationRequest
from agent.app.dispatcher import Dispatcher
from agent.app.specialists.base import SkillTracker
from agent.app.specialists.changes import ChangesSpecialist
from agent.app.specialists.logs import LogsSpecialist
from agent.app.specialists.metrics import MetricsSpecialist
from agent.app.specialists.resources import ResourcesSpecialist
from agent.app.triage import InvestigationPlan

pytestmark = pytest.mark.live


def _resolve_model_name() -> str:
    settings = get_settings()
    model = (
        os.getenv("GEMINI_MODEL")
        or os.getenv("MODEL_SPECIALIST")
        or os.getenv("GEMINI_FLASH_MODEL")
        or getattr(settings, "gemini_flash_model", None)
        or getattr(settings, "model_specialist", None)
    )
    return str(model or "")


def _check_live_credentials() -> None:
    """Verify live credentials and opt-in flag; skip cleanly otherwise."""
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("Live tests are opt-in: set RUN_LIVE_TESTS=1 to run")

    has_api_key = bool(
        os.getenv("GOOGLE_API_KEY", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    )
    has_vertex = bool(
        (os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"))
        and (
            os.getenv("GOOGLE_GENAI_USE_ENTERPRISE", "").lower() in ("true", "1")
            or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("APP_ENV") == "production"
        )
    )
    if not (has_api_key or has_vertex):
        pytest.skip(
            "No real Gemini credentials found (set GEMINI_API_KEY, "
            "GOOGLE_API_KEY, or GCP_PROJECT with ADC)"
        )


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_logs_specialist_turn_1_skill_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that in a live ADK Runner run with real Gemini, LogsSpecialist:

    1. Invokes load_skill on Turn 1 (skill_first=True)
    2. Populates skills_used including 'cloud-logging-queries'
    3. Pass 1 succeeds without DynamicNodeFailError (fallback_used=False)
    4. Executes >= 1 drill-down tool call and produces evidence/findings
    """
    _check_live_credentials()
    settings = get_settings()

    # Resolve model from environment or settings
    model_name = _resolve_model_name()
    project_id = (
        os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or getattr(settings, "gcp_project", None)
        or "live-test-project"
    )

    specialist = LogsSpecialist(
        project_id=project_id,
        model=model_name,
        skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
    )

    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-live-logs-001",
        source="generic",
        external_id="EXT-LIVE-001",
        external_key="INC-LIVE-001",
        title="High error rate observed in service endpoint",
        description="HTTP 500 status codes elevated above threshold in application logs",
        severity="P1",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 500 error spike", "Latency degradation"],
        initial_hypotheses=["Upstream timeout or database connection exhaustion"],
        selected_specialists=["logs"],
        start_time=now - timedelta(minutes=30),
        end_time=now,
    )

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now - timedelta(minutes=30),
        end_time=now,
        plan=plan,
        timeout_seconds=120.0,
    )

    # Asserts that in a live run, the specialist calls load_skill on Turn 1 (skill_first=True)
    # and skills_used includes 'cloud-logging-queries'.
    tracker = specialist.skill_tracker
    assert tracker.skill_first is True, (
        f"Expected skill_first=True, got {tracker.skill_first}. "
        f"Skills tracked: {tracker.skills_used}"
    )
    assert "cloud-logging-queries" in tracker.skills_used, (
        f"'cloud-logging-queries' not in tracker.skills_used: {tracker.skills_used}"
    )

    assert isinstance(findings, Findings)
    assert findings.skill_first is True, (
        f"Expected findings.skill_first=True, got {findings.skill_first}"
    )
    assert "cloud-logging-queries" in findings.skills_used, (
        f"'cloud-logging-queries' not in findings.skills_used: {findings.skills_used}"
    )
    assert findings.fallback_used is False, (
        f"LogsSpecialist Pass 1 failed (fallback_used=True): {findings.summary}. "
        f"Errors: {findings.errors}"
    )
    assert len(findings.queries_run) >= 1 or tracker.tool_call_count >= 2, (
        f"Expected >= 1 drill-down query or >= 2 tool calls, got queries={findings.queries_run}, "
        f"tool_calls={tracker.tool_call_count}"
    )
    assert len(findings.evidence) > 0 or bool(findings.summary and findings.summary.strip()), (
        "Expected non-empty evidence or summary from Pass 1"
    )
    assert "DynamicNodeFailError" not in caplog.text


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_metrics_specialist_turn_1_skill_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that in a live ADK Runner run with real Gemini, MetricsSpecialist:

    1. Invokes load_skill on Turn 1 (skill_first=True)
    2. Populates skills_used including 'cloud-monitoring-metrics'
    3. Pass 1 succeeds without DynamicNodeFailError (fallback_used=False)
    4. Executes >= 1 drill-down tool call and produces evidence/findings
    """
    _check_live_credentials()
    settings = get_settings()

    model_name = _resolve_model_name()
    project_id = (
        os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or getattr(settings, "gcp_project", None)
        or "live-test-project"
    )

    specialist = MetricsSpecialist(
        project_id=project_id,
        model=model_name,
        skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
    )

    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-live-metrics-001",
        source="generic",
        external_id="EXT-LIVE-METRICS-001",
        external_key="INC-LIVE-METRICS-001",
        title="Elevated latency and error rate in microservice",
        description="P99 latency spiking and error count exceeding SLO thresholds",
        severity="P1",
        service_hint="checkout-service",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="checkout-service",
        symptoms=["Latency degradation", "Error rate elevation"],
        initial_hypotheses=["Database pool saturation or CPU starvation"],
        selected_specialists=["metrics"],
        start_time=now - timedelta(minutes=30),
        end_time=now,
    )

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now - timedelta(minutes=30),
        end_time=now,
        plan=plan,
        timeout_seconds=120.0,
    )

    tracker = specialist.skill_tracker
    assert tracker.skill_first is True, (
        f"Expected skill_first=True, got {tracker.skill_first}. "
        f"Skills tracked: {tracker.skills_used}"
    )
    assert "cloud-monitoring-metrics" in tracker.skills_used, (
        f"'cloud-monitoring-metrics' not in tracker.skills_used: {tracker.skills_used}"
    )

    assert isinstance(findings, Findings)
    assert findings.skill_first is True, (
        f"Expected findings.skill_first=True, got {findings.skill_first}"
    )
    assert "cloud-monitoring-metrics" in findings.skills_used, (
        f"'cloud-monitoring-metrics' not in findings.skills_used: {findings.skills_used}"
    )
    assert findings.fallback_used is False, (
        f"MetricsSpecialist Pass 1 failed (fallback_used=True): {findings.summary}. "
        f"Errors: {findings.errors}"
    )
    assert len(findings.queries_run) >= 1 or tracker.tool_call_count >= 2, (
        f"Expected >= 1 drill-down query or >= 2 tool calls, got queries={findings.queries_run}, "
        f"tool_calls={tracker.tool_call_count}"
    )
    assert len(findings.evidence) > 0 or bool(findings.summary and findings.summary.strip()), (
        "Expected non-empty evidence or summary from Pass 1"
    )
    assert "DynamicNodeFailError" not in caplog.text


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_changes_specialist_turn_1_skill_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that in a live ADK Runner run with real Gemini, ChangesSpecialist:

    1. Invokes load_skill on Turn 1 (skill_first=True)
    2. Populates skills_used including 'change-correlation'
    3. Pass 1 succeeds without DynamicNodeFailError (fallback_used=False)
    4. Executes >= 1 drill-down tool call and produces evidence/findings
    """
    _check_live_credentials()
    settings = get_settings()

    model_name = _resolve_model_name()
    project_id = (
        os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or getattr(settings, "gcp_project", None)
        or "live-test-project"
    )

    specialist = ChangesSpecialist(
        project_id=project_id,
        model=model_name,
        skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
    )

    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-live-changes-001",
        source="generic",
        external_id="EXT-LIVE-CHANGES-001",
        external_key="INC-LIVE-CHANGES-001",
        title="Regression triggered after recent deployment",
        description="Crash looping or configuration drift correlated with recent release",
        severity="P2",
        service_hint="orders-worker",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="orders-worker",
        symptoms=["Crash loops", "High error rate"],
        initial_hypotheses=["Bad deployment revision or secret update"],
        selected_specialists=["changes"],
        start_time=now - timedelta(minutes=60),
        end_time=now,
    )

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now - timedelta(minutes=60),
        end_time=now,
        plan=plan,
        timeout_seconds=120.0,
    )

    tracker = specialist.skill_tracker
    assert tracker.skill_first is True, (
        f"Expected skill_first=True, got {tracker.skill_first}. "
        f"Skills tracked: {tracker.skills_used}"
    )
    assert "change-correlation" in tracker.skills_used, (
        f"'change-correlation' not in tracker.skills_used: {tracker.skills_used}"
    )

    assert isinstance(findings, Findings)
    assert findings.skill_first is True, (
        f"Expected findings.skill_first=True, got {findings.skill_first}"
    )
    assert "change-correlation" in findings.skills_used, (
        f"'change-correlation' not in findings.skills_used: {findings.skills_used}"
    )
    assert findings.fallback_used is False, (
        f"ChangesSpecialist Pass 1 failed (fallback_used=True): {findings.summary}. "
        f"Errors: {findings.errors}"
    )
    assert len(findings.queries_run) >= 1 or tracker.tool_call_count >= 2, (
        f"Expected >= 1 drill-down query or >= 2 tool calls, got queries={findings.queries_run}, "
        f"tool_calls={tracker.tool_call_count}"
    )
    assert len(findings.evidence) > 0 or bool(findings.summary and findings.summary.strip()), (
        "Expected non-empty evidence or summary from Pass 1"
    )
    assert "DynamicNodeFailError" not in caplog.text


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_resources_specialist_turn_1_skill_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that in a live ADK Runner run with real Gemini, ResourcesSpecialist:

    1. Invokes load_skill on Turn 1 (skill_first=True)
    2. Populates skills_used including 'cloud-run-troubleshooting'
    3. Pass 1 succeeds without DynamicNodeFailError (fallback_used=False)
    4. Executes >= 1 drill-down tool call and produces evidence/findings
    """
    _check_live_credentials()
    settings = get_settings()

    model_name = _resolve_model_name()
    project_id = (
        os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or getattr(settings, "gcp_project", None)
        or "live-test-project"
    )

    specialist = ResourcesSpecialist(
        project_id=project_id,
        model=model_name,
        skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
    )

    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-live-resources-001",
        source="generic",
        external_id="EXT-LIVE-RESOURCES-001",
        external_key="INC-LIVE-RESOURCES-001",
        title="Cloud Run service instances failing health checks",
        description="Container startup failures and CPU throttling on instance revision",
        severity="P2",
        service_hint="billing-processor",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="billing-processor",
        symptoms=["Health check failures", "Container crash"],
        initial_hypotheses=["Memory limit exceeded or invalid container entrypoint"],
        selected_specialists=["resources"],
        start_time=now - timedelta(minutes=45),
        end_time=now,
    )

    findings = await specialist.run_investigation_async(
        incident=incident,
        start_time=now - timedelta(minutes=45),
        end_time=now,
        plan=plan,
        timeout_seconds=120.0,
    )

    tracker = specialist.skill_tracker
    assert tracker.skill_first is True, (
        f"Expected skill_first=True, got {tracker.skill_first}. "
        f"Skills tracked: {tracker.skills_used}"
    )
    assert "cloud-run-troubleshooting" in tracker.skills_used, (
        f"'cloud-run-troubleshooting' not in tracker.skills_used: {tracker.skills_used}"
    )

    assert isinstance(findings, Findings)
    assert findings.skill_first is True, (
        f"Expected findings.skill_first=True, got {findings.skill_first}"
    )
    assert "cloud-run-troubleshooting" in findings.skills_used, (
        f"'cloud-run-troubleshooting' not in findings.skills_used: {findings.skills_used}"
    )
    assert findings.fallback_used is False, (
        f"ResourcesSpecialist Pass 1 failed (fallback_used=True): {findings.summary}. "
        f"Errors: {findings.errors}"
    )
    assert len(findings.queries_run) >= 1 or tracker.tool_call_count >= 2, (
        f"Expected >= 1 drill-down query or >= 2 tool calls, got queries={findings.queries_run}, "
        f"tool_calls={tracker.tool_call_count}"
    )
    assert len(findings.evidence) > 0 or bool(findings.summary and findings.summary.strip()), (
        "Expected non-empty evidence or summary from Pass 1"
    )
    assert "DynamicNodeFailError" not in caplog.text


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_dispatcher_concurrent_fanout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify end-to-end multi-agent fanout via Dispatcher with all 4 specialists in parallel.

    Asserts:
    1. All 4 specialists complete cleanly.
    2. fallback_used is False across all specialists.
    3. result.skills_used aggregates skills from all 4 specialists without crosstalk.
    4. Zero DynamicNodeFailError in logs.
    """
    _check_live_credentials()
    settings = get_settings()
    model_name = _resolve_model_name()
    project_id = (
        os.getenv("GCP_PROJECT")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or getattr(settings, "gcp_project", None)
        or "live-test-project"
    )

    specs = {
        "logs": LogsSpecialist(
            project_id=project_id,
            model=model_name,
            skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
        ),
        "metrics": MetricsSpecialist(
            project_id=project_id,
            model=model_name,
            skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
        ),
        "changes": ChangesSpecialist(
            project_id=project_id,
            model=model_name,
            skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
        ),
        "resources": ResourcesSpecialist(
            project_id=project_id,
            model=model_name,
            skill_tracker=SkillTracker(max_tool_calls=get_settings().specialist_max_tool_calls),
        ),
    }

    dispatcher = Dispatcher(
        config=settings,
        specialists=specs,
    )

    now = datetime.now(timezone.utc)
    incident = Incident(
        id="inc-live-disp-001",
        source="generic",
        external_id="EXT-LIVE-DISP-001",
        external_key="INC-LIVE-DISP-001",
        title="Major latency spike and 5xx errors across checkout and order flow",
        description=(
            "Multiple services reporting elevated 500 responses and CPU throttling after deployment"
        ),
        severity="P1",
        service_hint="shop-api",
        opened_at=now,
    )

    request = InvestigationRequest(
        incident=incident,
        callback_url="https://bridge-test.run.app/callback/report",
        requested_at=now,
    )

    result = await dispatcher.investigate(request)

    assert result.status == "completed"
    assert "DynamicNodeFailError" not in caplog.text

    # Verify per-specialist findings and check for fallback
    for name, specialist in specs.items():
        finding = getattr(specialist, "last_findings", None)
        assert finding is not None, f"Specialist {name} did not record findings"
        assert finding.fallback_used is False, (
            f"Specialist {name} used fallback: {finding.summary}. Errors: {finding.errors}"
        )
        assert finding.skill_first is True, f"Specialist {name} skill_first was not True"

    # Verify skill isolation (no crosstalk across specialists)
    assert "cloud-logging-queries" in specs["logs"].skill_tracker.skills_used
    assert "cloud-monitoring-metrics" not in specs["logs"].skill_tracker.skills_used

    assert "cloud-monitoring-metrics" in specs["metrics"].skill_tracker.skills_used
    assert "cloud-logging-queries" not in specs["metrics"].skill_tracker.skills_used

    assert "change-correlation" in specs["changes"].skill_tracker.skills_used
    assert "cloud-run-troubleshooting" not in specs["changes"].skill_tracker.skills_used

    assert "cloud-run-troubleshooting" in specs["resources"].skill_tracker.skills_used
    assert "change-correlation" not in specs["resources"].skill_tracker.skills_used

    # Verify result aggregates skills from all 4 specialists
    expected_skills = [
        "cloud-logging-queries",
        "cloud-monitoring-metrics",
        "change-correlation",
        "cloud-run-troubleshooting",
    ]
    for expected_skill in expected_skills:
        assert expected_skill in result.skills_used, (
            f"Expected skill '{expected_skill}' missing from result.skills_used: "
            f"{result.skills_used}"
        )
