from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import ProbableRootCause, Report
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import InvestigationPlan, TriageAgent


class StubScenarioLlmClient:
    """Stubbed LLM client returning scenario-specific LLM authored reports."""

    def __init__(self, prc_text: str, prc_confidence: float, actions: List[str]) -> None:
        self.prc_text = prc_text
        self.prc_confidence = prc_confidence
        self.actions = actions

    def generate_report(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        return Report(
            summary=(
                f"Investigation for {incident.external_key} ({plan.affected_service}) complete."
            ),
            impact=f"Severity {incident.severity} on {plan.affected_service}.",
            timeline=[],
            evidence_by_area={},
            probable_root_cause=ProbableRootCause(
                text=self.prc_text,
                confidence=self.prc_confidence,
            ),
            recent_changes=[],
            recommended_actions=list(self.actions),
            specialists_status=[],
            skills_used=skills_used or ["cloud-run-troubleshooting"],
            caveats=[],
            queries_appendix={},
            metadata={"affected_service": plan.affected_service},
        )


def make_incident(scenario_id: int, title: str, description: str, severity: str = "P1") -> Incident:
    return Incident(
        id=f"inc-sc-{scenario_id}",
        source="generic",
        external_id=f"ext-sc-{scenario_id}",
        external_key=f"GEN-SC{scenario_id}",
        title=title,
        description=description,
        severity=severity,  # type: ignore[arg-type]
        service_hint="shop-api",
        labels=[f"scenario:{scenario_id}", "service:shop-api"],
        opened_at=datetime.now(timezone.utc),
        reporter="SRE Monitoring",
    )


def test_scenario_1_diagnosis_error_spike() -> None:
    """Scenario 1: Checkout error spike due to database pool exhaustion."""
    incident = make_incident(
        scenario_id=1,
        title="Customers getting errors at checkout on shop-api",
        description=(
            "Elevated HTTP 500 error rates detected on shop-api /checkout endpoint (~40% failures)."
        ),
        severity="P1",
    )

    triage = TriageAgent()
    plan = triage.plan(incident)
    assert plan.affected_service == "shop-api"
    assert "logs" in plan.selected_specialists

    now = datetime.now(timezone.utc)
    findings: Dict[str, Findings] = {
        "logs": Findings(
            summary="High volume of 500 errors with DatabaseConnectionError stack traces.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "DatabaseConnectionError: Connection pool exhausted at shop-db:5432 "
                        "(active: 100, max: 100). Failed to acquire lock for checkout transaction."
                    ),
                    timestamp=now - timedelta(minutes=10),
                    console_url="https://console.cloud.google.com/logs/query",
                )
            ],
            anomalies=["Checkout 500 error rate spiked to 41.5%"],
            queries_run=["resource.type=cloud_run_revision severity>=ERROR"],
            confidence=0.90,
            errors=[],
        ),
        "metrics": Findings(
            summary="5xx error rate anomaly detected on shop-api.",
            evidence=[
                EvidenceItem(
                    kind="metric_timeseries",
                    excerpt="run.googleapis.com/request_count 5xx rate = 42 rps (base: 0.1 rps)",
                    timestamp=now - timedelta(minutes=12),
                )
            ],
            anomalies=["5xx error rate 420x above baseline"],
            queries_run=["fetch cloud_run_revision::run.googleapis.com/request_count"],
            confidence=0.85,
            errors=[],
        ),
        "changes": Findings(
            summary="No recent deployments or IAM changes detected in the investigation window.",
            evidence=[],
            anomalies=[],
            queries_run=["cloudaudit.googleapis.com/activity"],
            confidence=0.80,
            errors=[],
        ),
        "resources": Findings(
            summary="Cloud Run shop-api service healthy, 10 active instances.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.90,
            errors=[],
        ),
    }

    stub = StubScenarioLlmClient(
        prc_text=(
            "Service degradation on shop-api is caused by database connection pool exhaustion "
            "(DatabaseConnectionError) on /checkout"
        ),
        prc_confidence=0.88,
        actions=[
            "Increase database connection pool size and max connections for shop-api.",
            "Implement request backoff and review connection timeout on /checkout.",
            "Inspect application error logs for connection leaks or slow queries.",
            "Monitor Cloud Run 5xx response code class rate post-fix.",
        ],
    )
    synthesizer = SynthesizerAgent(llm_client=stub)
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
    )

    assert report.probable_root_cause is not None
    assert (
        "DatabaseConnectionError" in report.probable_root_cause.text
        or "error spike" in report.probable_root_cause.text
        or "Application exception" in report.probable_root_cause.text
    )
    assert report.probable_root_cause.confidence >= 0.65
    assert len(report.recommended_actions) >= 1
    assert any("error" in act.lower() for act in report.recommended_actions)


def test_scenario_2_diagnosis_latency_spike() -> None:
    """Scenario 2: Latency spike exceeding SLO."""
    incident = make_incident(
        scenario_id=2,
        title="shop-api very slow, checkout timing out",
        description="Significant latency spike observed on shop-api exceeding 500ms SLO threshold.",
        severity="P2",
    )

    triage = TriageAgent()
    plan = triage.plan(incident)
    assert plan.affected_service == "shop-api"

    now = datetime.now(timezone.utc)
    findings: Dict[str, Findings] = {
        "metrics": Findings(
            summary="Extreme latency degradation on shop-api endpoints.",
            evidence=[
                EvidenceItem(
                    kind="metric_timeseries",
                    excerpt="run.googleapis.com/request_latencies p95 = 5820ms (SLO: 500ms)",
                    timestamp=now - timedelta(minutes=15),
                )
            ],
            anomalies=["p95 latency degraded to 5820ms exceeding 500ms threshold"],
            queries_run=["fetch cloud_run_revision::run.googleapis.com/request_latencies"],
            confidence=0.90,
            errors=[],
        ),
        "logs": Findings(
            summary="Request timeouts and high latency logs recorded on /catalog and /checkout.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="httpRequest.latency=6.124s path=/catalog status=200",
                    timestamp=now - timedelta(minutes=14),
                )
            ],
            anomalies=["Requests taking > 5s on catalog browsing"],
            queries_run=[],
            confidence=0.80,
            errors=[],
        ),
        "changes": Findings(
            summary="No deployments correlated with latency onset.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "resources": Findings(
            summary="Autoscaler maxed out instances responding to queue backlog.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.80,
            errors=[],
        ),
    }

    stub = StubScenarioLlmClient(
        prc_text=(
            "Elevated request latency and processing delay (httpRequest.latency) on shop-api "
            "endpoints exceeding SLO"
        ),
        prc_confidence=0.85,
        actions=[
            "Profile application request pipelines and asynchronous event loops on shop-api.",
            "Inspect application code and configuration for artificial delays.",
            "Configure Cloud Monitoring alerting policy on run.googleapis.com/request_latencies.",
            "Tune Cloud Run container concurrency and worker thread pool configurations.",
        ],
    )
    synthesizer = SynthesizerAgent(llm_client=stub)
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
    )

    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.65
    assert (
        "latency" in report.probable_root_cause.text.lower()
        or "httpRequest.latency" in report.probable_root_cause.text
    )


def test_scenario_3_diagnosis_bad_release() -> None:
    """Scenario 3: Bad release with missing PAYMENT_GATEWAY_KEY."""
    incident = make_incident(
        scenario_id=3,
        title="shop-api broken after this morning's release",
        description="shop-api returning HTTP 503 following deployment. Readiness probe failing.",
        severity="P1",
    )

    triage = TriageAgent()
    plan = triage.plan(incident)
    assert plan.affected_service == "shop-api"

    now = datetime.now(timezone.utc)
    findings: Dict[str, Findings] = {
        "changes": Findings(
            summary="New Cloud Run revision deployed 12 minutes before incident opened.",
            evidence=[
                EvidenceItem(
                    kind="audit_log",
                    excerpt="Deploy revision shop-api-00042 by author: release-pipeline-sa",
                    timestamp=now - timedelta(minutes=15),
                    console_url="https://console.cloud.google.com/run/detail/us-central1/shop-api/revisions",
                )
            ],
            anomalies=["Deployment completed shortly before 503 errors started"],
            queries_run=["cloudaudit.googleapis.com/activity method:UpdateService"],
            confidence=0.95,
            errors=[],
        ),
        "logs": Findings(
            summary="Startup and readiness probe failure: missing PAYMENT_GATEWAY_KEY.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "Missing required environment variable: PAYMENT_GATEWAY_KEY. "
                        "Readiness probe failed."
                    ),
                    timestamp=now - timedelta(minutes=14),
                )
            ],
            anomalies=["100% readiness probe failures returning HTTP 503"],
            queries_run=["resource.type=cloud_run_revision severity>=ERROR"],
            confidence=0.95,
            errors=[],
        ),
        "metrics": Findings(
            summary="503 Service Unavailable spike.",
            evidence=[],
            anomalies=["100% 503 error rate on shop-api"],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "resources": Findings(
            summary="Revision shop-api-00042 failing readiness checks.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
    }

    stub = StubScenarioLlmClient(
        prc_text=(
            "Deployment / configuration regression on Cloud Run service shop-api: "
            "missing mandatory configuration variable PAYMENT_GATEWAY_KEY on revision "
            "shop-api-00042"
        ),
        prc_confidence=0.95,
        actions=[
            (
                "Immediate Cloud Run revision rollback: rolling back to prior stable revision "
                "shop-api-00041."
            ),
            "Restore missing PAYMENT_GATEWAY_KEY environment variable in Cloud Run configuration.",
            'Verify readiness probe health: verify /readyz returns HTTP 200 {"status": "ready"}.',
            (
                "Configure Cloud Monitoring alerting on HTTP 503 error rates and "
                "readiness probe failures."
            ),
        ],
    )
    synthesizer = SynthesizerAgent(llm_client=stub)
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
    )

    assert report.probable_root_cause is not None
    assert "regression" in report.probable_root_cause.text.lower()
    assert (
        "PAYMENT_GATEWAY_KEY" in report.probable_root_cause.text
        or "shop-api-00042" in report.probable_root_cause.text
    )
    assert report.probable_root_cause.confidence >= 0.85

    # Recommended action must include rolling back recent deployment
    recommendations_text = " ".join(report.recommended_actions)
    assert (
        "rolling back" in recommendations_text.lower() or "rollback" in recommendations_text.lower()
    )


def test_scenario_4_diagnosis_memory_leak_oom() -> None:
    """Scenario 4: Memory leak causing progressive exhaustion towards OOM."""
    incident = make_incident(
        scenario_id=4,
        title="shop-api intermittently unavailable (503s)",
        description="Container instances of shop-api experiencing memory saturation and crashing.",
        severity="P2",
    )

    triage = TriageAgent()
    plan = triage.plan(incident)
    assert plan.affected_service == "shop-api"

    now = datetime.now(timezone.utc)
    findings: Dict[str, Findings] = {
        "resources": Findings(
            summary="Cloud Run instances restarting due to memory saturation.",
            evidence=[
                EvidenceItem(
                    kind="resource_event",
                    excerpt="Instance restarted with container exit code 137 (SIGKILL / OOM)",
                    timestamp=now - timedelta(minutes=8),
                )
            ],
            anomalies=[
                "Memory saturation: Cloud Run container exceeded 1024MiB limit with exit code 137"
            ],
            queries_run=[],
            confidence=0.90,
            errors=[],
        ),
        "logs": Findings(
            summary="System logs confirm memory limit exceeded.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "Memory limit of 1024 MiB exceeded with 1032 MiB used. "
                        "Container terminated."
                    ),
                    timestamp=now - timedelta(minutes=9),
                )
            ],
            anomalies=["Repeated Memory limit exceeded log messages"],
            queries_run=["resource.type=cloud_run_revision 'Memory limit'"],
            confidence=0.90,
            errors=[],
        ),
        "metrics": Findings(
            summary="Linear ramp in memory utilization towards 100%.",
            evidence=[],
            anomalies=["Memory utilization reached 100% threshold"],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "changes": Findings(
            summary="No deployments correlated with OOM onset.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.80,
            errors=[],
        ),
    }

    stub = StubScenarioLlmClient(
        prc_text=(
            "Container memory exhaustion / OOM killer termination on Cloud Run service shop-api: "
            "progressive memory leak exceeding Memory limit of 1024 MiB"
        ),
        prc_confidence=0.90,
        actions=[
            (
                "Review and temporarily scale container memory allocation: gcloud run services "
                "update shop-api --memory <LIMIT>"
            ),
            (
                "Inspect application heap profiles and memory allocations for unreleased objects "
                "across request lifecycles."
            ),
            (
                "Configure Cloud Monitoring alerting on "
                "run.googleapis.com/container/memory/utilizations exceeding threshold over "
                "a 5-minute window."
            ),
            (
                "Monitor container restart counts and verify memory utilization stabilizes "
                "post-mitigation."
            ),
        ],
    )
    synthesizer = SynthesizerAgent(llm_client=stub)
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
    )

    assert report.probable_root_cause is not None
    assert (
        "Memory saturation" in report.probable_root_cause.text
        or "Memory limit" in report.probable_root_cause.text
        or "memory exhaustion" in report.probable_root_cause.text.lower()
        or "oom" in report.probable_root_cause.text.lower()
        or "resource" in report.probable_root_cause.text.lower()
    )
    assert report.probable_root_cause.confidence >= 0.80


def test_scenario_5_diagnosis_dependency_failure() -> None:
    """Scenario 5: Downstream IAM invoker removed on inventory-svc (403 Forbidden)."""
    incident = make_incident(
        scenario_id=5,
        title="Product availability not loading on shop-api",
        description="Downstream inventory verification failing with HTTP 403 Forbidden errors.",
        severity="P2",
    )

    triage = TriageAgent()
    plan = triage.plan(incident)
    assert plan.affected_service == "shop-api"

    now = datetime.now(timezone.utc)
    findings: Dict[str, Findings] = {
        "changes": Findings(
            summary="IAM policy changed on inventory-svc removing shop-api-sa invoker binding.",
            evidence=[
                EvidenceItem(
                    kind="audit_log",
                    excerpt=(
                        "SetIamPolicy on inventory-svc removing roles/run.invoker for shop-api-sa"
                    ),
                    timestamp=now - timedelta(minutes=20),
                    console_url="https://console.cloud.google.com/iam-admin/audit",
                )
            ],
            anomalies=[
                "IAM policy modification on downstream dependency immediately preceded errors"
            ],
            queries_run=["cloudaudit.googleapis.com/activity methodName:SetIamPolicy"],
            confidence=0.95,
            errors=[],
        ),
        "logs": Findings(
            summary="Downstream calls to inventory-svc failing with 403 Forbidden.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "Downstream dependency inventory-svc returned HTTP 403 Forbidden. "
                        "Cloud Run IAM invoker check failed."
                    ),
                    timestamp=now - timedelta(minutes=18),
                )
            ],
            anomalies=["403 Forbidden errors calling downstream inventory reservation endpoint"],
            queries_run=["resource.type=cloud_run_revision '403 Forbidden'"],
            confidence=0.95,
            errors=[],
        ),
        "metrics": Findings(
            summary="502 Bad Gateway spike on shop-api due to downstream 403s.",
            evidence=[],
            anomalies=["502 error spike matching catalog requests"],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
        "resources": Findings(
            summary="inventory-svc Cloud Run service status: active, ingress internal.",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.85,
            errors=[],
        ),
    }

    stub = StubScenarioLlmClient(
        prc_text=(
            "Downstream dependency failure on Cloud Run service shop-api: HTTP 403 Forbidden "
            "received from dependency inventory-svc following SetIamPolicy modification"
        ),
        prc_confidence=0.92,
        actions=[
            (
                "Verify and refresh IAM service account permissions, roles, and authentication "
                "tokens used to invoke dependency inventory-svc."
            ),
            (
                "Verify network egress, VPC connector, and Cloud Run ingress bindings between "
                "shop-api and inventory-svc."
            ),
            (
                "Configure Cloud Monitoring alerting on HTTP 403 response rates from "
                "downstream dependencies."
            ),
            (
                "Verify graceful degradation and fallback handling while inventory-svc "
                "authorization is restored."
            ),
        ],
    )
    synthesizer = SynthesizerAgent(llm_client=stub)
    report = synthesizer.synthesize(
        incident=incident,
        plan=plan,
        findings=findings,
    )

    assert report.probable_root_cause is not None
    assert (
        "regression" in report.probable_root_cause.text.lower()
        or "dependency failure" in report.probable_root_cause.text.lower()
    )
    prc_text = report.probable_root_cause.text
    assert "SetIamPolicy" in prc_text or "inventory-svc" in prc_text
    assert "403 Forbidden" in prc_text or "inventory-svc" in prc_text
    assert report.probable_root_cause.confidence >= 0.85
