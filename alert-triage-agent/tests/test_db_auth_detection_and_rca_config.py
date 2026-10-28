"""DB-auth detection precision and config-derived RCA environment text.

1. Database authentication detection must key on specific DB login failure signatures,
   never on a bare "auth"/"denied"/"password" substring. Ordinary request logs that mention
   "Authorization: Bearer" or "OAuth" (S2 live false positive) must not be reported as DB
   auth failures; the S5 invalid IAM DB user log must still be detected.
2. The RCA must not state unverified "contradicting evidence" (host faults, Google Cloud
   outages are never queried), and report text must not contain hardcoded environment literals.
"""

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import CloudRunSpecialist, has_db_auth_signature
from agent.app.specialists.synthesizer import SynthesizerAgent, _dossier_has_db_auth_error

AGENT_APP = Path(__file__).parent.parent / "agent" / "app"

# Log line from the S5 scenario fixture (invalid IAM DB user on ata-inventory-api).
S5_LOG = "Failed to connect to Cloud SQL: 'NoneType' object has no attribute 'decode'"

# S2-like request/application log text with HTTP auth vocabulary but no DB failure.
S2_LIKE_LOGS = [
    "POST /orders 503 upstream inventory timeout; headers: Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.x.y",
    "OAuth token refreshed for service account; calling ata-inventory-api",
    "Request authenticated via Apigee; forwarding to backend",
]


# ---------------------------------------------------------------------------
# has_db_auth_signature: unit level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        S5_LOG,
        'FATAL: password authentication failed for user "ata-inventory-sa"',
        'FATAL: Cloud SQL IAM user authentication failed for user "invalid-iam-user@example-project.iam"',
        'FATAL: Cloud SQL IAM service account authentication failed for user "sa@example-project.iam"',
        'FATAL: PAM authentication failed for user "ata-inventory-sa"',
        "FATAL:  password authentication is required",
        "ERROR: permission denied for database inventory",
        'FATAL: role "invalid-iam-user@example-project.iam" does not exist',
        "pg8000.exceptions.DatabaseError: {'S': 'FATAL', 'V': 'FATAL', 'C': '28P01', 'M': 'login rejected'}",
        "psycopg2.OperationalError: SQLSTATE 28000 invalid authorization specification",
        "OperationalError: (1045, \"Access denied for user 'app'@'10.0.0.1' (using password: YES)\")",
    ],
)
def test_db_auth_signature_detects_specific_db_login_failures(text):
    assert has_db_auth_signature(text) is True


@pytest.mark.parametrize(
    "text",
    S2_LIKE_LOGS
    + [
        "auth",
        "Authentication middleware: 401 Unauthorized for /orders",
        "403 Access denied: caller lacks run.routes.invoke",
        "IAM permission denied on resource projects/example-project",
        "Password reset endpoint called",
        "TLS handshake failed with upstream",
        # S3 capacity failure goes through the same connect path but is not auth.
        "Failed to connect to Cloud SQL: FATAL: sorry, too many clients already",
        "Failed to connect to Cloud SQL: timed out",
        "request completed in 28000 ms",
        "",
    ],
)
def test_db_auth_signature_ignores_generic_auth_vocabulary(text):
    assert has_db_auth_signature(text) is False


def test_db_auth_signature_handles_none():
    assert has_db_auth_signature(None) is False


# ---------------------------------------------------------------------------
# CloudRunSpecialist.investigate: log scan through the specialist
# ---------------------------------------------------------------------------

def _log_entry(payload: str) -> MagicMock:
    entry = MagicMock()
    entry.log_name = "projects/test-proj/logs/run.googleapis.com%2Fstderr"
    entry.payload = payload
    entry.text_payload = None
    entry.http_request = None
    entry.severity = "ERROR"
    entry.timestamp = datetime.now(timezone.utc)
    return entry


async def _investigate_with_logs(payloads: list[str], service: str, policy: str) -> EvidenceDossier:
    spec = CloudRunSpecialist()
    alert = AlertEvent(
        incident_id="0.test-db-auth",
        policy_name=policy,
        resource_name=service,
        started_at="2026-10-24T13:21:01Z",
    )
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with patch("google.cloud.logging.Client") as mock_log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_metric_cls:
        mock_log_client = MagicMock()
        mock_log_client.list_entries.return_value = [_log_entry(p) for p in payloads]
        mock_log_cls.return_value = mock_log_client
        mock_metric_client = MagicMock()
        mock_metric_client.list_time_series.return_value = []
        mock_metric_cls.return_value = mock_metric_client
        return await spec.investigate(alert, config)


@pytest.mark.asyncio
async def test_s2_like_logs_with_bearer_and_oauth_do_not_flag_db_auth():
    dossier = await _investigate_with_logs(S2_LIKE_LOGS, "ata-orders-api", "ata-orders-5xx")

    assert not dossier.metrics.get("has_db_auth_error")
    assert "database authentication" not in str(dossier.metrics.get("observed_symptom", "")).lower()
    assert not any("database authentication" in f.lower() for f in dossier.findings)


@pytest.mark.asyncio
async def test_s2_like_logs_alone_do_not_make_dossier_anomalous():
    # Non-target service (upstream probe): only a DB-auth hit could make it anomalous.
    spec = CloudRunSpecialist()
    alert = AlertEvent(
        incident_id="0.test-db-auth-upstream",
        policy_name="ata-apigee-latency",
        resource_name="ata-apigee-proxy",
        started_at="2026-10-24T13:21:01Z",
    )
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with patch("google.cloud.logging.Client") as mock_log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_metric_cls:
        mock_log_cls.return_value.list_entries.return_value = [_log_entry(p) for p in S2_LIKE_LOGS]
        mock_metric_cls.return_value.list_time_series.return_value = []
        dossier = await spec.investigate(alert, config, target_service="ata-orders-api")

    assert dossier.anomaly_detected is False


@pytest.mark.asyncio
async def test_s5_invalid_iam_user_log_flags_db_auth():
    dossier = await _investigate_with_logs([S5_LOG], "ata-inventory-api", "ata-inventory-5xx")

    assert dossier.metrics.get("has_db_auth_error") is True
    assert any("database authentication" in f.lower() for f in dossier.findings)
    assert dossier.anomaly_detected is True


@pytest.mark.asyncio
async def test_cloud_sql_iam_auth_failure_log_flags_db_auth():
    log = ('Failed to connect to Cloud SQL: {\'S\': \'FATAL\', \'C\': \'28000\', \'M\': '
           '\'Cloud SQL IAM user authentication failed for user "invalid-iam-user@example-project.iam"\'}')
    dossier = await _investigate_with_logs([log], "ata-inventory-api", "ata-inventory-5xx")

    assert dossier.metrics.get("has_db_auth_error") is True


# ---------------------------------------------------------------------------
# Synthesizer: DB-auth decision must not use a bare "auth" substring
# ---------------------------------------------------------------------------

def _cr_dossier(findings: list[str], metrics: dict | None = None) -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="CloudRunSpecialist",
        target_resource="ata-orders-api",
        findings=findings,
        metrics=metrics or {},
        anomaly_detected=True,
    )


@pytest.mark.parametrize(
    "findings",
    [
        ["Requests to `ata-orders-api` carried Authorization: Bearer headers."],
        ["OAuth token refresh observed on `ata-orders-api`."],
        ["Unauthenticated requests rejected on `ata-orders-api`."],
    ],
)
def test_dossier_with_auth_words_but_no_db_auth_is_not_db_auth(findings):
    assert _dossier_has_db_auth_error(_cr_dossier(findings)) is False


def test_dossier_db_auth_from_metric_or_explicit_finding():
    assert _dossier_has_db_auth_error(_cr_dossier([], {"has_db_auth_error": True})) is True
    assert _dossier_has_db_auth_error(
        _cr_dossier(["Application error logs on `x` indicate database authentication failure."])
    ) is True
    assert _dossier_has_db_auth_error(None) is False


def test_observations_do_not_claim_db_auth_for_oauth_findings():
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.test-s2-obs",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-24T13:21:01Z",
    )
    dossiers = [
        _cr_dossier(
            ["OAuth token refresh observed on `ata-orders-api`; Authorization: Bearer forwarded."],
            {"error_rate_5xx": "12.0%"},
        )
    ]

    obs = synth._synthesize_observations(dossiers, alert)
    text, _ = synth._determine_root_cause(dossiers, "ata-orders-api", alert)

    assert "database authentication" not in obs.lower()
    assert "database authentication" not in text.lower()


# ---------------------------------------------------------------------------
# RCA contradicting evidence: no unverified boilerplate claims
# ---------------------------------------------------------------------------
# Nothing in the pipeline queries host/hypervisor faults or Google Cloud Service Health, so
# the RCA must not claim either was checked. contradicting_evidence holds only what was
# actually observed; today nothing produces such entries, so it is empty.

UNVERIFIED_CLAIM_MARKERS = ("host", "hypervisor", "hyperthreading", "outage", "service health")


def _rca_alert() -> AlertEvent:
    return AlertEvent(
        incident_id="0.test-rca-config",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-24T13:21:01Z",
    )


def _assert_no_unverified_claims(rca: dict) -> None:
    assert rca["contradicting_evidence"] == []
    text = " ".join(
        str(x) for key in ("supporting_evidence", "contradicting_evidence") for x in rca.get(key, [])
    ).lower()
    for marker in UNVERIFIED_CLAIM_MARKERS:
        assert marker not in text, marker


def test_rca_has_no_unverified_contradicting_evidence():
    rca = SynthesizerAgent()._build_root_cause_analysis(
        root_cause_text="x", confidence=0.5, dossiers=[], origin="ata-orders-api", alert=_rca_alert(),
    )
    _assert_no_unverified_claims(rca)


@pytest.mark.asyncio
async def test_synthesize_report_rca_has_no_unverified_contradicting_evidence():

    async def empty_dossier(self, alert, config, target_service=None):
        return EvidenceDossier(specialist_name=self.__class__.__name__, target_resource=target_service or "")

    config = AgentConfig(project_id="example-project-id", region="test-region-9", is_local=True)
    synth = SynthesizerAgent()
    with patch.object(CloudRunSpecialist, "investigate", empty_dossier), \
         patch("agent.app.specialists.synthesizer.ApigeeSpecialist.investigate", empty_dossier), \
         patch("agent.app.specialists.synthesizer.CloudSQLSpecialist.investigate", empty_dossier), \
         patch("agent.app.specialists.synthesizer.ChangeSpecialist.investigate", empty_dossier), \
         patch.object(SynthesizerAgent, "_update_okf_file", lambda *a, **k: None):
        report = await synth.execute_investigation(_rca_alert(), "grp-test", "grp-test.md", config)

    _assert_no_unverified_claims(report.root_cause_analysis)


def test_triage_baseline_report_has_no_unverified_contradicting_evidence(tmp_path):
    from agent.app.triage import generate_investigation_report

    config = AgentConfig(project_id="example-project-id", region="test-region-9", is_local=True,
                         INCIDENT_STORE_PATH=str(tmp_path), FALLBACK_STORE_PATH=str(tmp_path))
    report = generate_investigation_report(_rca_alert(), "grp-test", "grp-test.md", config)
    _assert_no_unverified_claims(report.root_cause_analysis)


def test_report_modules_have_no_host_fault_or_outage_boilerplate():
    for rel in ("agent/app/specialists/synthesizer.py", "agent/app/triage.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text().lower()
        for phrase in ("hyperthreading", "host faults", "host-level", "infrastructure service outages"):
            assert phrase not in src, (rel, phrase)


# ---------------------------------------------------------------------------
# Source guard: no environment literals in report-text string constants
# ---------------------------------------------------------------------------

ENV_LITERALS = ("us-west1", "cloud-assist-fde-2")
REPORT_TEXT_MODULES = [AGENT_APP / "specialists" / "synthesizer.py", AGENT_APP / "triage.py"]


@pytest.mark.parametrize("path", REPORT_TEXT_MODULES, ids=lambda p: p.name)
def test_report_text_modules_have_no_region_or_project_literals(path):
    tree = ast.parse(path.read_text())
    violations = [
        f"{path.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and any(lit in node.value for lit in ENV_LITERALS)
    ]
    assert not violations, "Region/project literals must come from config:\n" + "\n".join(violations)
