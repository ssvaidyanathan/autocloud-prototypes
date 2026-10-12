"""Mandatory Guard Tests: Verify zero post-LLM root cause template overrides,
exact stubbed LLM text matching, confidence monotonicity, and AST static checks.
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest
from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import ProbableRootCause, Report
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import InvestigationPlan


class StubLlmClient:
    """Stubbed LLM client returning deterministic, custom LLM-authored reports."""

    def __init__(
        self,
        prc_text: str,
        prc_confidence: float = 0.90,
        recommended_actions: Optional[List[str]] = None,
    ) -> None:
        self.prc_text = prc_text
        self.prc_confidence = prc_confidence
        self.recommended_actions = recommended_actions or [
            "Custom LLM Action: Validate application performance metrics.",
            "Custom LLM Action: Inspect deployment telemetry.",
        ]

    def generate_report(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        return Report(
            summary=f"LLM Summary: Investigation for {incident.service_hint} complete.",
            impact=f"LLM Impact: Severity {incident.severity} on {incident.service_hint}.",
            timeline=[],
            evidence_by_area={},
            probable_root_cause=ProbableRootCause(
                text=self.prc_text,
                confidence=self.prc_confidence,
            ),
            recent_changes=[],
            recommended_actions=list(self.recommended_actions),
            specialists_status=[],
            skills_used=skills_used or ["cloud-run-troubleshooting"],
            caveats=[],
            queries_appendix={},
            metadata={"affected_service": plan.affected_service},
        )


def _build_test_incident(title: str, description: str, service: str = "demo-api") -> Incident:
    return Incident(
        id="inc-guard-001",
        source="servicenow",
        external_id="INC0099991",
        external_key="INC0099991",
        title=title,
        description=description,
        severity="P1",
        service_hint=service,
        opened_at=datetime.now(timezone.utc),
    )


def _build_test_plan(service: str = "demo-api") -> InvestigationPlan:
    now = datetime.now(timezone.utc)
    return InvestigationPlan(
        affected_service=service,
        symptoms=["Service degradation"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "changes", "resources"],
    )


# ============================================================================
# 1. Stubbed LLM Integrity Tests Across All 5 Failure Categories
# ============================================================================


@pytest.mark.parametrize(
    ("scenario_category", "incident_title", "findings_dict", "custom_prc_text", "custom_actions"),
    [
        # Category 1: Container Memory Exhaustion / OOM Killer
        (
            "oom",
            "demo-api: Container OOM terminations and restarts",
            {
                "logs": Findings(
                    summary="OOM kill logs",
                    evidence=[
                        EvidenceItem(
                            kind="log_entry",
                            excerpt=(
                                "Memory limit of 1024 MiB exceeded with 1057 MiB used. "
                                "Container terminated."
                            ),
                        )
                    ],
                    anomalies=["Container exit code 137 (SIGKILL / OOM)"],
                    queries_run=[],
                    confidence=0.90,
                    errors=[],
                ),
                "metrics": Findings(
                    summary="Memory utilization 100%",
                    evidence=[
                        EvidenceItem(
                            kind="metric",
                            excerpt="run.googleapis.com/container/memory/utilizations = 1.0",
                        )
                    ],
                    anomalies=["Memory saturation: 100% limit reached"],
                    queries_run=[],
                    confidence=0.90,
                    errors=[],
                ),
            },
            (
                "Custom LLM Root Cause: Progressive memory heap exhaustion inside worker "
                "batch processor"
            ),
            [
                "Custom LLM Action: Review heap profiler allocation graphs in staging",
                "Custom LLM Action: Increase container memory limits to 2GiB temporarily",
            ],
        ),
        # Category 2: 503 / Readiness Probe / Configuration Regression
        (
            "config_regression",
            "demo-api: Readiness probe 503 errors",
            {
                "logs": Findings(
                    summary="Readiness probe failed",
                    evidence=[
                        EvidenceItem(
                            kind="log_entry",
                            excerpt=(
                                "Readiness probe /readyz failed with HTTP 503: missing DB_PASSWD"
                            ),
                        )
                    ],
                    anomalies=["100% readiness probe failures returning HTTP 503"],
                    queries_run=[],
                    confidence=0.95,
                    errors=[],
                ),
                "changes": Findings(
                    summary="Revision deployment",
                    evidence=[
                        EvidenceItem(
                            kind="revision",
                            excerpt="Revision demo-api-00042 (traffic: 100%)",
                        )
                    ],
                    anomalies=["Revision deployed 10m ago"],
                    queries_run=[],
                    confidence=0.90,
                    errors=[],
                ),
            },
            (
                "Custom LLM Root Cause: Startup configuration omission of database credentials "
                "in revision demo-api-00042"
            ),
            [
                "Custom LLM Action: Re-inject DB_PASSWD secret into Cloud Run configuration",
                "Custom LLM Action: Rollback traffic to previous stable revision demo-api-00041",
            ],
        ),
        # Category 3: 403 / Downstream Dependency Failure
        (
            "dependency_403",
            "demo-api: Downstream service call returning 403 Forbidden",
            {
                "logs": Findings(
                    summary="Downstream 403 errors",
                    evidence=[
                        EvidenceItem(
                            kind="log_entry",
                            excerpt=(
                                "403 Forbidden received calling backend-service: "
                                "IAM invoker check failed"
                            ),
                        )
                    ],
                    anomalies=["Downstream authorization failure: HTTP 403"],
                    queries_run=[],
                    confidence=0.95,
                    errors=[],
                ),
                "changes": Findings(
                    summary="IAM policy update",
                    evidence=[
                        EvidenceItem(
                            kind="audit_log",
                            excerpt=(
                                "SetIamPolicy on backend-service removed roles/run.invoker for "
                                "demo-api-sa"
                            ),
                        )
                    ],
                    anomalies=["IAM invoker role removed 15m prior to incident"],
                    queries_run=[],
                    confidence=0.95,
                    errors=[],
                ),
            },
            "Custom LLM Root Cause: IAM policy binding revocation on backend-service target",
            [
                "Custom LLM Action: Rebind roles/run.invoker to demo-api-sa service account",
                "Custom LLM Action: Add alerting on downstream 403 response rates",
            ],
        ),
        # Category 4: Database Connection Pool Exhaustion
        (
            "db_pool",
            "demo-api: 500 error spike during checkout",
            {
                "logs": Findings(
                    summary="Database connection pool timeout",
                    evidence=[
                        EvidenceItem(
                            kind="log_entry",
                            excerpt=(
                                "TooManyConnectionsError: connection pool limit 100 exhausted "
                                "on /checkout"
                            ),
                        )
                    ],
                    anomalies=["Burst of HTTP 500 responses due to pool exhaustion"],
                    queries_run=[],
                    confidence=0.92,
                    errors=[],
                ),
                "metrics": Findings(
                    summary="5xx rate spike",
                    evidence=[
                        EvidenceItem(
                            kind="metric",
                            excerpt="run.googleapis.com/request_count 5xx = 25/s",
                        )
                    ],
                    anomalies=["5xx error rate elevated to 32%"],
                    queries_run=[],
                    confidence=0.90,
                    errors=[],
                ),
            },
            (
                "Custom LLM Root Cause: Thread starvation acquiring Postgres connection "
                "from asyncpg pool"
            ),
            [
                "Custom LLM Action: Scale database max_connections and adjust pool sizing",
                "Custom LLM Action: Implement exponential backoff on pool connection timeout",
            ],
        ),
        # Category 5: Elevated Request Latency / Processing Delay
        (
            "latency",
            "demo-api: Extreme latency degradation",
            {
                "logs": Findings(
                    summary="Latency warnings",
                    evidence=[
                        EvidenceItem(
                            kind="warning_log",
                            excerpt=(
                                "[WARNING] demo-api: Injecting 7.5s artificial delay into "
                                "catalog requests"
                            ),
                        )
                    ],
                    anomalies=["Application warning logs show delay injection"],
                    queries_run=[],
                    confidence=0.85,
                    errors=[],
                ),
                "metrics": Findings(
                    summary="p95 latency spike",
                    evidence=[
                        EvidenceItem(
                            kind="latency_metric",
                            excerpt="run.googleapis.com/request_latencies p95 = 7800ms",
                        )
                    ],
                    anomalies=["p95 latency breached 500ms SLO threshold"],
                    queries_run=[],
                    confidence=0.88,
                    errors=[],
                ),
            },
            "Custom LLM Root Cause: Unbounded blocking call inside request handling pipeline",
            [
                "Custom LLM Action: Profile asyncio event loop for blocking synchronous calls",
                "Custom LLM Action: Remove testing delay injection flags from service environment",
            ],
        ),
    ],
)
def test_stubbed_llm_integrity_all_categories(
    scenario_category: str,
    incident_title: str,
    findings_dict: Dict[str, Findings],
    custom_prc_text: str,
    custom_actions: List[str],
) -> None:
    """Verify that the synthesizer preserves 100% of LLM-authored probable_root_cause.text

    and recommended_actions without post-LLM template overrides across all 5 failure categories.
    """
    incident = _build_test_incident(
        title=incident_title, description="Detailed incident description"
    )
    plan = _build_test_plan(service="demo-api")

    stub_llm = StubLlmClient(
        prc_text=custom_prc_text,
        prc_confidence=0.90,
        recommended_actions=custom_actions,
    )
    agent = SynthesizerAgent(llm_client=stub_llm)

    report = agent.synthesize(incident=incident, plan=plan, findings=findings_dict)

    # Probable root cause text must match the LLM's text EXACTLY
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.text == custom_prc_text

    # Recommended actions must match the LLM's actions EXACTLY
    assert report.recommended_actions == custom_actions


# ============================================================================
# 2. Confidence Monotonicity Tests
# ============================================================================


@pytest.mark.parametrize("initial_confidence", [0.95, 0.85, 0.70, 0.50, 0.35, 0.20])
def test_confidence_monotonicity_never_raised(initial_confidence: float) -> None:
    """Verify that report.probable_root_cause.confidence is NEVER raised above the

    LLM's returned value (only down-calibrated or maintained).
    """
    incident = _build_test_incident(
        title="Test Incident", description="Testing confidence monotonicity"
    )
    plan = _build_test_plan()

    findings = {
        "logs": Findings(
            summary="Test logs",
            evidence=[EvidenceItem(kind="log_entry", excerpt="Database error occurred")],
            anomalies=["Log error anomaly"],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
        "metrics": Findings(
            summary="Test metrics",
            evidence=[EvidenceItem(kind="metric", excerpt="5xx rate elevated")],
            anomalies=["Metric anomaly"],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    }

    stub_llm = StubLlmClient(
        prc_text="Custom LLM Root Cause: Database connection deadlock",
        prc_confidence=initial_confidence,
    )
    agent = SynthesizerAgent(llm_client=stub_llm)

    report = agent.synthesize(incident=incident, plan=plan, findings=findings)

    assert report.probable_root_cause is not None
    # Confidence MUST be <= initial_confidence
    assert report.probable_root_cause.confidence <= initial_confidence


def test_confidence_down_calibration_on_weak_evidence() -> None:
    """Verify that synthesizer down-calibrates confidence when evidence is weak or empty,

    and that caveats explain the down-calibration.
    """
    incident = _build_test_incident(
        title="Empty Evidence Incident", description="Testing down-calibration"
    )
    plan = _build_test_plan()

    empty_findings: Dict[str, Findings] = {
        "logs": Findings(
            summary="Empty", evidence=[], anomalies=[], queries_run=[], confidence=0.0, errors=[]
        ),
        "metrics": Findings(
            summary="Empty", evidence=[], anomalies=[], queries_run=[], confidence=0.0, errors=[]
        ),
    }

    stub_llm = StubLlmClient(
        prc_text="Custom LLM Root Cause: Speculative failure hypothesis",
        prc_confidence=0.95,
    )
    agent = SynthesizerAgent(llm_client=stub_llm)

    report = agent.synthesize(incident=incident, plan=plan, findings=empty_findings)

    assert report.probable_root_cause is not None
    # Must be down-calibrated below initial 0.95 (Condition 1 caps at 0.25)
    assert report.probable_root_cause.confidence <= 0.25
    assert len(report.caveats) >= 1
    assert any("zero evidence items" in c.lower() for c in report.caveats)


# ============================================================================
# 3. Static AST Guard Check Test
# ============================================================================


def test_ast_guard_no_canned_template_constructors_or_assignments() -> None:
    """Verify that synthesizer.py and llm.py pass the AST static check:

    zero ProbableRootCause constructions or assignments with canned templates or f-strings
    outside the labeled fallback branch.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    script_path = repo_root / "scripts" / "check-no-rootcause-overrides.py"
    assert script_path.exists(), f"Guard script not found: {script_path}"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, (
        f"AST static check failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


# ============================================================================
# 4. Labeled Heuristic Fallback Test
# ============================================================================


def test_heuristic_fallback_when_llm_unavailable() -> None:
    """Verify that when LLM call fails completely, the synthesizer provides an explicitly

    labeled fallback with synthesizer_mode = 'heuristic_fallback', text starting with
    'Automated fallback (LLM unavailable):', and confidence <= 0.3.
    """
    incident = _build_test_incident(title="Fallback Incident", description="Testing fallback mode")
    plan = _build_test_plan(service="demo-api")

    # Synthesizer with no LLM client
    agent = SynthesizerAgent(llm_client=None)

    findings: Dict[str, Findings] = {
        "logs": Findings(
            summary="Some logs",
            evidence=[EvidenceItem(kind="log_entry", excerpt="Error in service")],
            anomalies=["Log anomaly"],
            queries_run=[],
            confidence=0.8,
            errors=[],
        ),
    }

    report = agent.synthesize(incident=incident, plan=plan, findings=findings)

    assert report.metadata["synthesizer_mode"] == "heuristic_fallback"
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.text.startswith("Automated fallback (LLM unavailable):")
    assert report.probable_root_cause.confidence <= 0.30
    assert len(report.recommended_actions) >= 1
