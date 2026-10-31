from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from agent.app.contracts.findings import EvidenceItem, Findings
from agent.app.contracts.incident import Incident
from agent.app.contracts.report import ProbableRootCause, Report
from agent.app.synthesizer import SynthesizerAgent
from agent.app.triage import InvestigationPlan


class StubLlmClient:
    """Deterministic stub LLM client for synthesizer integration testing."""

    def __init__(
        self,
        prc_text: str,
        prc_confidence: float = 0.90,
        recommended_actions: Optional[List[str]] = None,
    ) -> None:
        self.prc_text = prc_text
        self.prc_confidence = prc_confidence
        self.recommended_actions = recommended_actions or []

    def generate_report(
        self,
        incident: Incident,
        plan: InvestigationPlan,
        findings: Dict[str, Findings],
        skills_used: Optional[List[str]] = None,
    ) -> Report:
        return Report(
            summary=(
                f"Investigation for {incident.external_key} ({plan.affected_service}): "
                f"{self.prc_text}"
            ),
            impact=f"Severity {incident.severity} affecting service '{plan.affected_service}'.",
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


def _create_incident() -> Incident:
    return Incident(
        id="inc-synth-1",
        source="servicenow",
        external_id="INC0055",
        external_key="INC0055",
        title="Outage on shop-api after revision deploy",
        description="Checkout endpoint is throwing 500 Internal Server Error",
        severity="P1",
        service_hint="shop-api",
        opened_at=datetime(2026, 10, 9, 2, 10, 0, tzinfo=timezone.utc),
    )


def _create_plan() -> InvestigationPlan:
    opened = datetime(2026, 10, 9, 2, 10, 0, tzinfo=timezone.utc)
    return InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 5xx / application errors", "Recent deployment regression"],
        start_time=opened - timedelta(minutes=60),
        end_time=opened,
        selected_specialists=["logs", "metrics", "changes"],  # resources is unselected
        initial_hypotheses=["Bad revision deploy"],
    )


def test_synthesizer_produces_valid_report() -> None:
    stub = StubLlmClient(
        prc_text=(
            "Deployment regression on shop-api: new revision shop-api-00042 caused "
            "database connection timeouts."
        ),
        prc_confidence=0.85,
        recommended_actions=[
            "Execute immediate rollback to previous stable revision.",
            "Inspect database connection pool settings.",
        ],
    )
    agent = SynthesizerAgent(llm_client=stub)
    incident = _create_incident()
    plan = _create_plan()

    deploy_time = datetime(2026, 10, 9, 2, 5, 0, tzinfo=timezone.utc)
    error_time = datetime(2026, 10, 9, 2, 6, 0, tzinfo=timezone.utc)

    findings = {
        "changes": Findings(
            summary="New revision deployed",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision shop-api-00042 deployed by author: deploy-bot",
                    timestamp=deploy_time,
                    console_url="https://console.cloud.google.com/run/detail/revisions",
                )
            ],
            anomalies=["Revision deployment 5 minutes prior to incident"],
            queries_run=["list_cloud_run_revisions"],
            confidence=0.9,
            errors=[],
        ),
        "logs": Findings(
            summary="Application error spike",
            evidence=[
                EvidenceItem(
                    kind="error",
                    excerpt="Database connection pool timeout",
                    timestamp=error_time,
                    console_url="https://console.cloud.google.com/logs",
                )
            ],
            anomalies=["Error group: DB timeout occurred 45 times"],
            queries_run=["query_logs", "summarize_error_groups"],
            confidence=0.88,
            errors=[],
        ),
        "metrics": Findings(
            summary="5xx error rate spiked to 35%",
            evidence=[],
            anomalies=["Baseline shift: 5xx rate +35.0%"],
            queries_run=["query_time_series"],
            confidence=0.85,
            errors=[],
        ),
    }

    report = agent.synthesize(incident, plan, findings)

    assert isinstance(report, Report)
    assert "shop-api" in report.summary
    assert "P1" in report.impact

    # Validate timeline: chronological ordering
    assert len(report.timeline) >= 3
    for i in range(len(report.timeline) - 1):
        assert report.timeline[i].timestamp <= report.timeline[i + 1].timestamp

    # Probable root cause
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.8
    assert "regression" in report.probable_root_cause.text.lower()

    # Recent changes
    assert len(report.recent_changes) == 1
    assert report.recent_changes[0].author == "deploy-bot"

    # Specialist statuses: logs, metrics, changes = completed, resources = skipped
    status_map = {s.specialist: s.status for s in report.specialists_status}
    assert status_map["logs"] == "completed"
    assert status_map["metrics"] == "completed"
    assert status_map["changes"] == "completed"
    assert status_map["resources"] == "skipped"

    # Recommended actions
    assert any(
        "rollback" in a.lower() or "rolling back" in a.lower() for a in report.recommended_actions
    )


def test_synthesizer_handles_partial_specialist_failures() -> None:
    agent = SynthesizerAgent()
    incident = _create_incident()
    plan = _create_plan()

    findings = {
        "logs": Findings(
            summary="Logs specialist encountered API quota limit",
            evidence=[],
            anomalies=[],
            queries_run=["query_logs"],
            confidence=0.0,
            errors=["ResourceExhausted: 429 Quota exceeded for Cloud Logging API"],
        ),
        "metrics": Findings(
            summary="Metrics succeeded",
            evidence=[
                EvidenceItem(
                    kind="metric",
                    excerpt="Request count anomaly detected",
                    timestamp=datetime(2026, 10, 9, 2, 7, 0, tzinfo=timezone.utc),
                )
            ],
            anomalies=["Baseline shift in latencies"],
            queries_run=["query_time_series"],
            confidence=0.75,
            errors=[],
        ),
    }
    specialist_errors = {"changes": "Timeout connecting to Cloud Audit Logs"}

    report = agent.synthesize(incident, plan, findings, specialist_errors=specialist_errors)

    assert isinstance(report, Report)
    status_map = {s.specialist: s for s in report.specialists_status}

    # logs failed via finding.errors
    assert status_map["logs"].status == "failed"
    assert "429" in (status_map["logs"].error or "")

    # changes failed via specialist_errors
    assert status_map["changes"].status == "failed"
    assert "Timeout" in (status_map["changes"].error or "")

    # metrics completed
    assert status_map["metrics"].status == "completed"

    # resources skipped
    assert status_map["resources"].status == "skipped"

    # Investigation completes cleanly despite partial specialist failures
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence < 0.8


def test_synthesizer_grounded_playbooks_and_no_k8s_endpoints() -> None:
    stub = StubLlmClient(
        prc_text=(
            "Database connection pool exhaustion on Cloud Run service shop-api: "
            "TooManyConnectionsError on /checkout endpoint."
        ),
        prc_confidence=0.90,
        recommended_actions=[
            "Review database pool size and max connections settings.",
            "Implement backoff and circuit breaking in /checkout callers.",
            "Audit application code for connection leak or unclosed database handles.",
            "Monitor Cloud Monitoring 5xx response rates.",
        ],
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-synth-playbook",
        source="servicenow",
        external_id="INC0099",
        external_key="INC0099",
        title="500 errors on shop-api /checkout",
        description="Customers failing to complete purchases due to database connection exhaustion",
        severity="P1",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 5xx / application errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "resources"],
        initial_hypotheses=["DB connection exhaustion"],
    )

    findings = {
        "logs": Findings(
            summary="Database connection exhaustion logs",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt="TooManyConnectionsError: connection limit reached on /checkout",
                    timestamp=now - timedelta(minutes=10),
                )
            ],
            anomalies=["Connection error burst"],
            queries_run=["query_logs"],
            confidence=0.9,
            errors=[],
        ),
        "metrics": Findings(
            summary="5xx spike",
            evidence=[
                EvidenceItem(
                    kind="metric",
                    excerpt="5xx rate +40%",
                    timestamp=now - timedelta(minutes=10),
                )
            ],
            anomalies=["5xx error spike"],
            queries_run=["query_time_series"],
            confidence=0.85,
            errors=[],
        ),
    }

    report = agent.synthesize(incident, plan, findings)

    # Probable root cause mentions service, /checkout endpoint, and TooManyConnectionsError
    assert report.probable_root_cause is not None
    assert "shop-api" in report.probable_root_cause.text
    assert "/checkout" in report.probable_root_cause.text
    assert (
        "TooManyConnectionsError" in report.probable_root_cause.text
        or "connection pool exhaustion" in report.probable_root_cause.text
    )

    # All 4 playbooks present
    actions_text = " ".join(report.recommended_actions).lower()
    assert "pool size" in actions_text or "max connections" in actions_text
    assert "backoff" in actions_text or "circuit breaking" in actions_text
    assert "leak" in actions_text or "unclosed" in actions_text
    assert "5xx" in actions_text and "monitor" in actions_text

    # No Kubernetes/GKE noise in actions or root cause
    for action in report.recommended_actions:
        assert "/api/v1/namespaces/" not in action
        assert "kube-system" not in action
        assert "gmp-system" not in action
    assert "/api/v1/namespaces/" not in report.probable_root_cause.text


def test_synthesizer_latency_root_cause_and_playbooks() -> None:
    stub = StubLlmClient(
        prc_text=(
            "Elevated request latency on Cloud Run service shop-api: "
            "artificial delay injection on /catalog and /checkout endpoints."
        ),
        prc_confidence=0.85,
        recommended_actions=[
            "Profile application request pipelines and event loops on shop-api.",
            "Inspect code for artificial delay injection and downstream dependency latency.",
            (
                "Configure Cloud Monitoring alerting for run.googleapis.com/request_latencies "
                "p95 > 2.0s."
            ),
            "Review container concurrency and worker thread settings.",
        ],
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 10, 1, 30, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-synth-latency",
        source="servicenow",
        external_id="INC0010054",
        external_key="INC0010054",
        title="shop-api very slow, checkout timing out",
        description="Significant latency spike observed on shop-api exceeding SLO threshold.",
        severity="P2",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Latency / response time degradation", "Client request timeouts"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "resources"],
        initial_hypotheses=["Elevated request latency"],
    )

    findings = {
        "metrics": Findings(
            summary="Extreme latency degradation on shop-api endpoints.",
            evidence=[
                EvidenceItem(
                    kind="latency_metric",
                    excerpt=(
                        'Metric metric.type = "run.googleapis.com/request_latencies": '
                        "current p95=16051ms vs baseline p95=93.6ms (+17048.5% shift)"
                    ),
                    timestamp=now - timedelta(minutes=15),
                )
            ],
            anomalies=[
                "Elevated request latency detected on shop-api: p95 latency increased by "
                "+17048.5% (from 93.6ms to 16051ms)"
            ],
            queries_run=["compare_to_baseline(run.googleapis.com/request_latencies)"],
            confidence=0.90,
            errors=[],
        ),
        "logs": Findings(
            summary="Application warning logs recorded delay injection on /catalog and /checkout.",
            evidence=[
                EvidenceItem(
                    kind="warning_log",
                    excerpt="[WARNING] shop-api: Scenario 2 (Latency Spike): Injecting 6.93s delay",
                    timestamp=now - timedelta(minutes=14),
                )
            ],
            anomalies=[
                "Application warning logs indicate latency spike / delay injection on shop-api: "
                "Scenario 2 (Latency Spike): Injecting 6.93s delay"
            ],
            queries_run=["query_logs(severity >= WARNING)"],
            confidence=0.85,
            errors=[],
        ),
    }

    report = agent.synthesize(incident, plan, findings)

    # 1. Grounded probable root cause attributes to latency / delay injection, not 500 errors
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.80
    prc_text = report.probable_root_cause.text.lower()
    assert "shop-api" in prc_text
    assert "latency" in prc_text or "delay" in prc_text
    assert "http 500" not in prc_text

    # 2. Recommended actions include all 4 concrete latency playbooks
    actions_text = " ".join(report.recommended_actions).lower()
    # Playbook 1: profile request pipelines / event loops
    assert "profile application request pipelines" in actions_text or "event loops" in actions_text
    # Playbook 2: inspect artificial delays / dependency latency / locks
    assert "artificial delay" in actions_text or "downstream dependency latency" in actions_text
    # Playbook 3: Cloud Monitoring latency alerting (p95 > 2.0s)
    assert "run.googleapis.com/request_latencies" in actions_text or "alerting" in actions_text
    # Playbook 4: container concurrency and worker threads
    assert "concurrency" in actions_text or "worker thread" in actions_text


def test_synthesizer_scenario3_root_cause_and_playbooks() -> None:
    expected_root_cause = (
        "Deployment / configuration regression on Cloud Run service shop-api: "
        "missing mandatory configuration variable PAYMENT_GATEWAY_KEY, "
        "causing readiness probe (/readyz) failure and HTTP 503 errors on /checkout."
    )
    expected_actions = [
        (
            "Immediate Cloud Run revision rollback: gcloud run services update-traffic shop-api "
            "--to-revisions shop-api-00041=100"
        ),
        (
            "Restore missing PAYMENT_GATEWAY_KEY environment variable or Secret Manager "
            "reference in Cloud Run configuration."
        ),
        'Verify readiness probe health: verify /readyz returns HTTP 200 {"status": "ready"}.',
        "Configure Cloud Monitoring alerting on HTTP 503 error rates and readiness probe failures.",
    ]
    stub = StubLlmClient(
        prc_text=expected_root_cause,
        prc_confidence=0.95,
        recommended_actions=expected_actions,
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 10, 2, 8, 16, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-synth-scen3",
        source="servicenow",
        external_id="INC0010056",
        external_key="INC0010056",
        title="shop-api: Scenario 3 (Bad Release) missing PAYMENT_GATEWAY_KEY",
        description="Checkout failing with HTTP 503 on /readyz and /checkout",
        severity="P1",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["HTTP 5xx / application errors", "Readiness probe failures"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "changes"],
        initial_hypotheses=["Bad release deployment regression"],
    )

    findings = {
        "changes": Findings(
            summary="New revision deployed prior to 503 errors.",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision shop-api-00042 (author: deployer@example.com, traffic: 100%)",
                    timestamp=now - timedelta(minutes=15),
                ),
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision shop-api-00041 (author: deployer@example.com, traffic: 0%)",
                    timestamp=now - timedelta(hours=2),
                ),
            ],
            anomalies=["Revision shop-api-00042 deployed 15m ago"],
            queries_run=["list_cloud_run_revisions(shop-api)"],
            confidence=0.90,
            errors=[],
        ),
        "logs": Findings(
            summary="Readiness probe failed due to missing PAYMENT_GATEWAY_KEY.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "[ERROR] shop-api: Scenario 3 (Bad Release): Cannot process checkout; "
                        "missing PAYMENT_GATEWAY_KEY"
                    ),
                    timestamp=now - timedelta(minutes=14),
                )
            ],
            anomalies=["Readiness probe /readyz failing with HTTP 503"],
            queries_run=["query_logs(severity >= WARNING)"],
            confidence=0.95,
            errors=[],
        ),
        "metrics": Findings(
            summary="503 errors on /checkout and /readyz.",
            evidence=[],
            anomalies=["Elevated 503 error rate on shop-api"],
            queries_run=["compare_to_baseline(run.googleapis.com/request_count)"],
            confidence=0.90,
            errors=[],
        ),
    }

    report = agent.synthesize(incident, plan, findings)

    # 1. Grounded root cause
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.90
    expected_root_cause = (
        "Deployment / configuration regression on Cloud Run service shop-api: "
        "missing mandatory configuration variable PAYMENT_GATEWAY_KEY, "
        "causing readiness probe (/readyz) failure and HTTP 503 errors on /checkout."
    )
    assert report.probable_root_cause.text == expected_root_cause

    # 2. Recommended actions include all 4 SRE playbooks
    assert len(report.recommended_actions) == 4
    # Playbook 1: Immediate revision rollback (with dynamic prior revision shop-api-00041)
    assert (
        "Immediate Cloud Run revision rollback: gcloud run services update-traffic shop-api "
        "--to-revisions shop-api-00041=100" in report.recommended_actions[0]
    )
    # Playbook 2: Restore missing PAYMENT_GATEWAY_KEY
    assert (
        "Restore missing PAYMENT_GATEWAY_KEY environment variable or Secret Manager reference "
        "in Cloud Run configuration." in report.recommended_actions[1]
    )
    # Playbook 3: Verify readiness probe health /readyz returns HTTP 200 {"status": "ready"}
    assert (
        'Verify readiness probe health: verify /readyz returns HTTP 200 {"status": "ready"}.'
        in report.recommended_actions[2]
    )
    # Playbook 4: Cloud Monitoring alerting on HTTP 503 and readiness probe failures
    assert (
        "Configure Cloud Monitoring alerting on HTTP 503 error rates and readiness probe failures."
        in report.recommended_actions[3]
    )


def test_synthesizer_scenario4_memory_leak_and_oom_playbooks() -> None:
    expected_root_cause = (
        "Container memory exhaustion / OOM killer termination on Cloud Run service shop-api: "
        "progressive in-memory buffer allocation exceeding container limit (1024 MiB), "
        "triggering kernel OOM terminations, container restarts, and intermittent HTTP 503 "
        "errors on active revision shop-api-00042."
    )
    expected_actions = [
        (
            "Review and temporarily scale container memory allocation: gcloud run services update "
            "shop-api --memory <LIMIT>"
        ),
        (
            "Inspect application heap profiles and memory allocations for unreleased "
            "objects across request lifecycles."
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
    ]
    stub = StubLlmClient(
        prc_text=expected_root_cause,
        prc_confidence=0.95,
        recommended_actions=expected_actions,
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 10, 2, 45, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-synth-scen4",
        source="servicenow",
        external_id="INC0010058",
        external_key="INC0010058",
        title="shop-api: Scenario 4 (Memory Leak) leading to OOM crash",
        description=(
            "Container instances of shop-api experiencing memory saturation, "
            "exit code 137, and HTTP 503 errors on /checkout"
        ),
        severity="P1",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Memory exhaustion", "Container restarts", "HTTP 503 errors"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "changes", "resources"],
        initial_hypotheses=["Memory leak causing OOM terminations"],
    )

    findings = {
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
            queries_run=["list_resource_events(shop-api)"],
            confidence=0.90,
            errors=[],
        ),
        "logs": Findings(
            summary="System logs confirm memory limit exceeded.",
            evidence=[
                EvidenceItem(
                    kind="log_entry",
                    excerpt=(
                        "Memory limit of 1024 MiB exceeded with 1057 MiB used. "
                        "Container terminated."
                    ),
                    timestamp=now - timedelta(minutes=9),
                )
            ],
            anomalies=["Repeated memory leak and OOM termination log messages"],
            queries_run=["query_logs(severity >= WARNING)"],
            confidence=0.90,
            errors=[],
        ),
        "changes": Findings(
            summary="Active revision serving 100% traffic.",
            evidence=[
                EvidenceItem(
                    kind="revision",
                    excerpt="Revision shop-api-00042 (traffic: 100%)",
                    timestamp=now - timedelta(hours=1),
                )
            ],
            anomalies=[],
            queries_run=["list_cloud_run_revisions(shop-api)"],
            confidence=0.85,
            errors=[],
        ),
        "metrics": Findings(
            summary="Container memory utilization at 100% and intermittent 503 errors.",
            evidence=[],
            anomalies=[
                "run.googleapis.com/container/memory/utilizations reaching 100%",
                "Intermittent HTTP 503 errors on /checkout during container restarts",
            ],
            queries_run=["compare_to_baseline(run.googleapis.com/container/memory/utilizations)"],
            confidence=0.85,
            errors=[],
        ),
    }

    report = agent.synthesize(incident, plan, findings)

    # 1. Grounded root cause
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.90
    assert report.probable_root_cause.text == expected_root_cause

    # 2. Mutually exclusive detection: Ensure Scenario 3 was NOT triggered
    assert "PAYMENT_GATEWAY_KEY" not in report.probable_root_cause.text
    assert "readiness probe" not in report.probable_root_cause.text
    for act in report.recommended_actions:
        assert "PAYMENT_GATEWAY_KEY" not in act

    # 3. Recommended actions include all 4 concrete SRE playbooks
    assert len(report.recommended_actions) == 4
    # Playbook 1: Memory scaling
    assert (
        "Review and temporarily scale container memory allocation: gcloud run services update "
        "shop-api --memory <LIMIT>" in report.recommended_actions[0]
    )
    # Playbook 2: Heap profiles and memory leak inspection
    assert (
        "Inspect application heap profiles and memory allocations for unreleased "
        "objects across request lifecycles." in report.recommended_actions[1]
    )
    # Playbook 3: Cloud Monitoring alerting on memory utilization
    assert (
        "Configure Cloud Monitoring alerting on run.googleapis.com/container/memory/utilizations "
        "exceeding threshold over a 5-minute window." in report.recommended_actions[2]
    )
    # Playbook 4: Container restart count monitoring and stabilization
    assert (
        "Monitor container restart counts and verify memory utilization stabilizes "
        "post-mitigation." in report.recommended_actions[3]
    )


def test_synthesizer_scenario5_dependency_failure_playbooks() -> None:
    expected_root_cause = (
        "Downstream dependency failure on Cloud Run service shop-api: "
        "HTTP 403 Forbidden received from dependency inventory-svc due to missing or invalid "
        "IAM authentication token."
    )
    expected_actions = [
        (
            "Verify and refresh IAM service account permissions, roles, and authentication tokens "
            "used to invoke dependency inventory-svc."
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
            "Verify graceful degradation and fallback handling while inventory-svc authorization "
            "is restored."
        ),
    ]
    stub = StubLlmClient(
        prc_text=expected_root_cause,
        prc_confidence=0.95,
        recommended_actions=expected_actions,
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 10, 2, 50, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-synth-scen5",
        source="servicenow",
        external_id="INC0010059",
        external_key="INC0010059",
        title="Product availability not loading on shop-api (Scenario 5 Dependency Failure)",
        description="Downstream inventory verification failing with HTTP 403 Forbidden errors.",
        severity="P2",
        service_hint="shop-api",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="shop-api",
        symptoms=["Downstream dependency failure", "HTTP 403 Forbidden"],
        start_time=now - timedelta(hours=1),
        end_time=now,
        selected_specialists=["logs", "metrics", "changes", "resources"],
        initial_hypotheses=["IAM permission denied on inventory-svc"],
    )

    findings = {
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
                        "Scenario 5 (Dependency Failure): 403 Forbidden from inventory-svc. "
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

    report = agent.synthesize(incident, plan, findings)

    # 1. Grounded root cause
    assert report.probable_root_cause is not None
    assert report.probable_root_cause.confidence >= 0.90
    assert report.probable_root_cause.text == expected_root_cause

    # 2. Mutually exclusive detection: Ensure Scenario 3 and 4 were NOT triggered
    assert "PAYMENT_GATEWAY_KEY" not in report.probable_root_cause.text
    assert "OOM" not in report.probable_root_cause.text
    assert "memory" not in report.probable_root_cause.text.lower()
    for act in report.recommended_actions:
        assert "PAYMENT_GATEWAY_KEY" not in act
        assert "memory" not in act.lower()

    # 3. Recommended actions include all 4 concrete SRE playbooks
    assert len(report.recommended_actions) == 4
    # Playbook 1: IAM service account token verification & refresh
    assert (
        "Verify and refresh IAM service account permissions, roles, and authentication tokens "
        "used to invoke dependency inventory-svc." in report.recommended_actions[0]
    )
    # Playbook 2: Egress and ingress bindings verification
    assert (
        "Verify network egress, VPC connector, and Cloud Run ingress bindings between shop-api and "
        "inventory-svc." in report.recommended_actions[1]
    )
    # Playbook 3: Cloud Monitoring alerting on HTTP 403 response rates
    assert (
        "Configure Cloud Monitoring alerting on HTTP 403 response rates from "
        "downstream dependencies." in report.recommended_actions[2]
    )
    # Playbook 4: Graceful degradation and fallback handling
    assert (
        "Verify graceful degradation and fallback handling while inventory-svc authorization "
        "is restored." in report.recommended_actions[3]
    )


def test_synthesizer_handles_string_prior_incidents() -> None:
    """Verify synthesizer does not crash when plan.prior_incidents contains strings."""
    stub = StubLlmClient(
        prc_text="Root cause description",
        prc_confidence=0.85,
        recommended_actions=["Action 1"],
    )
    agent = SynthesizerAgent(llm_client=stub)
    now = datetime(2026, 10, 9, 2, 0, 0, tzinfo=timezone.utc)
    incident = Incident(
        id="inc-str-prior",
        source="jira",
        external_id="AMJTEST-23",
        external_key="AMJTEST-23",
        title="Alert violation on test-service-1",
        description="Healthcheck failure",
        severity="P2",
        opened_at=now,
    )
    plan = InvestigationPlan(
        affected_service="test-service-1",
        symptoms=["Healthcheck failure"],
        start_time=now - timedelta(minutes=60),
        end_time=now,
        selected_specialists=["metrics"],
        prior_incidents=["AMJTEST-20: Prior healthcheck glitch", "AMJTEST-21: DB timeout"],
    )
    findings = {
        "metrics": Findings(
            summary="Metrics ok",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        )
    }

    report = agent.synthesize(incident, plan, findings)
    assert report is not None
    # Verify timeline was built without crashing
    assert len(report.timeline) > 0
    timeline_texts = [item.description for item in report.timeline]
    assert any("[PRIOR_INCIDENT]" in t for t in timeline_texts)
