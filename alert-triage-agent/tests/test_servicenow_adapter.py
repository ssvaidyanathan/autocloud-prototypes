import json
import httpx
import pytest

from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.alert import AlertEvent
from bridge.app.contracts.report import (
    EvidenceItem,
    InvestigationReport,
    ProbableRootCause,
    TimelineItem,
)


@pytest.fixture
def sample_report():
    return InvestigationReport(
        group_id="grp_20261022_inv_db",
        filename="incident-db-conn-20261022.md",
        summary="Cloud SQL database connection exhaustion causing cascading 5xx service failures",
        observations="Active database connections reached 100/100 (100% capacity) on ata-inventory-db",
        recommended_actions=[
            "Terminate idle database sessions",
            "Verify application connection release logic in ata-inventory-api",
        ],
        probable_root_cause=ProbableRootCause(
            text="Exhaustion of PostgreSQL backend connections due to idle transaction leaks.",
            confidence=0.94,
        ),
        origin="ata-inventory-db",
        blast_radius=["ata-inventory-api", "ata-inventory-v1", "ata-orders-api"],
        priority="P1",
        timeline=[
            TimelineItem(timestamp="2026-10-22T21:14:30Z", description="Connections exceeded 95% threshold"),
        ],
        evidence_by_area={
            "cloud_sql": [
                EvidenceItem(
                    headline="Database Connection Metric",
                    content="cloudsql.googleapis.com/database/postgresql/num_backends = 100",
                )
            ]
        },
    )


def test_4_bullet_description_formatting(sample_report):
    bullets = sample_report.build_executive_summary_bullets()
    assert len(bullets) == 4
    assert bullets[0].startswith("* **Problem:** Cloud SQL database connection exhaustion")
    assert "* **Systems:** Origin: `ata-inventory-db` (94% confidence) | Blast Radius: ata-inventory-api, ata-inventory-v1, ata-orders-api" in bullets[1]
    assert "* **Observations:** Active database connections reached 100/100" in bullets[2]
    assert "* **Recommended Actions:** Terminate idle database sessions; Verify application connection release logic in ata-inventory-api" in bullets[3]


def test_executive_summary_html_work_notes_formatting(sample_report):
    """Assert work notes format wraps in [code] and uses valid HTML <ul> and <li> tags."""
    html_notes = sample_report.build_executive_summary_html()
    assert html_notes.startswith("[code]\n<p><b>Executive Summary:</b></p>\n<ul>")
    assert html_notes.endswith("</ul>\n[/code]")
    assert "<li><b>Problem:</b> Cloud SQL database connection exhaustion" in html_notes
    assert "<li><b>Systems:</b>\n    <ul>\n      <li>Origin: <code>ata-inventory-db</code> (94% confidence)</li>" in html_notes
    assert "<li><b>Observations:</b>\n    <ul>\n      <li>Active database connections reached 100/100" in html_notes
    assert "<li><b>Recommended Actions:</b>" in html_notes
    assert "Terminate idle database sessions" in html_notes


def test_structured_description_plain_text_zero_markdown(sample_report):
    """Assert ServiceNow description uses clean plain text with ZERO * and ZERO #."""
    desc = sample_report.build_structured_description(project_id="cloud-assist-fde-2")
    assert "*" not in desc
    assert "#" not in desc
    assert "Incident Group ID: grp_20261022_inv_db" in desc
    assert "Priority: P1" in desc
    assert "Origin: ata-inventory-db" in desc
    assert "Blast Radius: ata-inventory-api, ata-inventory-v1, ata-orders-api" in desc
    assert "Executive Summary:" in desc
    assert "Problem:\n  - Cloud SQL database connection exhaustion" in desc
    assert "Systems:\n  - Origin: ata-inventory-db [Cloud SQL PostgreSQL Instance]" in desc
    # The fixture carries no topology path, so the line is omitted rather than guessed.
    assert "Dependency Path:" not in desc
    assert "Observations:" in desc
    assert "Recommended Actions:" in desc
    # No action names a rollback, so there is no generic "revert the latest change" line.
    assert "Rollback:" not in desc
    assert "Revert latest" not in desc


def test_servicenow_adapter_table_api_and_attachment_success(sample_report):
    posted_requests = []
    captured_payload = {}
    patch_calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "GET" and "/api/now/table/sys_user" in str(request.url):
            return httpx.Response(
                200,
                json={"result": [{"sys_id": "usr-admin-001", "user_name": "admin", "name": "System Administrator"}]},
            )
        if request.method == "POST" and "/api/now/table/incident" in str(request.url):
            captured_payload.update(json.loads(request.read()))
            return httpx.Response(
                201,
                json={"result": {"sys_id": "sn-sys-12345", "number": "INC0010482", "state": "1"}},
            )
        if request.method == "PATCH" and "/api/now/table/incident/sn-sys-12345" in str(request.url):
            patch_calls.append(json.loads(request.read()))
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-12345", "state": "2"}})
        if request.method == "POST" and "/api/now/attachment/file" in str(request.url):
            return httpx.Response(
                201,
                json={"result": {"sys_id": "att-9999", "file_name": "report.html"}},
            )
        if request.method == "GET" and "/api/now/table/incident/sn-sys-12345" in str(request.url):
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-12345", "state": "2"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    config = BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        incidents_bucket="test-incidents-bucket",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)

    ref = adapter.create_incident(sample_report)
    assert ref.sys_id == "sn-sys-12345"
    assert ref.number == "INC0010482"

    # Verify call sequence: sys_user GET -> incident POST (state=1) -> state=2 PATCH -> work_notes PATCH -> attachment POST -> readback GET
    assert len(posted_requests) == 6
    assert posted_requests[0].method == "GET"
    assert "/api/now/table/sys_user" in str(posted_requests[0].url)
    assert posted_requests[1].method == "POST"
    assert "/api/now/table/incident" in str(posted_requests[1].url)
    assert posted_requests[2].method == "PATCH"
    assert "/api/now/table/incident/sn-sys-12345" in str(posted_requests[2].url)
    assert posted_requests[3].method == "PATCH"
    assert "/api/now/table/incident/sn-sys-12345" in str(posted_requests[3].url)
    assert posted_requests[4].method == "POST"
    assert "/api/now/attachment/file" in str(posted_requests[4].url)
    assert posted_requests[5].method == "GET"
    assert "/api/now/table/incident/sn-sys-12345" in str(posted_requests[5].url)

    # Verify Table API creation request body: caller_id, state=1 (New), correlation_id
    assert captured_payload["caller_id"] == "usr-admin-001"
    assert captured_payload["state"] == "1"
    assert captured_payload["correlation_id"] == "incident-db-conn-20261022.md"
    assert captured_payload["urgency"] == "1"
    assert captured_payload["short_description"] == "[P1] ata-inventory-db: Resource exhaustion (1 alert)"

    # Verify state and work_notes PATCH payloads (state=2 immediately, then work_notes)
    assert len(patch_calls) == 2
    assert patch_calls[0] == {"state": "2"}
    assert "work_notes" in patch_calls[1]
    assert "[code]" in patch_calls[1]["work_notes"]
    assert "<b>Problem:</b>" in patch_calls[1]["work_notes"]
    assert "<b>Systems:</b>" in patch_calls[1]["work_notes"]
    assert "<b>Observations:</b>" in patch_calls[1]["work_notes"]
    assert "<b>Recommended Actions:</b>" in patch_calls[1]["work_notes"]
    assert "[/code]" in patch_calls[1]["work_notes"]

    # Verify clean plain-text description has ZERO * and ZERO #
    desc = captured_payload["description"]
    assert "*" not in desc
    assert "#" not in desc
    assert "Problem:" in desc
    assert "Systems:" in desc
    assert "Observations:" in desc
    assert "Recommended Actions:" in desc


@pytest.mark.parametrize("failure_mode", ["http_429", "http_500", "timeout"])
def test_attachment_failure_triggers_gcs_fallback_and_work_note(sample_report, monkeypatch, failure_mode):
    posted_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "GET" and "/api/now/table/sys_user" in str(request.url):
            return httpx.Response(200, json={"result": [{"sys_id": "usr-admin-001"}]})
        if request.method == "POST" and "/api/now/table/incident" in str(request.url):
            return httpx.Response(
                201,
                json={"result": {"sys_id": "sn-sys-12345", "number": "INC0010482", "state": "1"}},
            )
        if request.method == "POST" and "/api/now/attachment/file" in str(request.url):
            if failure_mode == "http_429":
                return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})
            elif failure_mode == "http_500":
                return httpx.Response(500, json={"error": {"message": "Attachment upload failed"}})
            elif failure_mode == "timeout":
                raise httpx.ReadTimeout("Timeout uploading attachment")
        if request.method == "PATCH" and "/api/now/table/incident/sn-sys-12345" in str(request.url):
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-12345", "state": "2"}})
        if request.method == "GET" and "/api/now/table/incident/sn-sys-12345" in str(request.url):
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-12345", "state": "2"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    config = BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        incidents_bucket="test-incidents-bucket",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)

    gcs_uploaded = []

    def fake_gcs(bucket, gid, html):
        uri = f"gs://{bucket}/reports/{gid}.html"
        gcs_uploaded.append((uri, len(html)))
        return uri

    monkeypatch.setattr(adapter, "_persist_report_to_gcs", fake_gcs)

    ref = adapter.create_incident(sample_report)
    assert ref.sys_id == "sn-sys-12345"
    assert ref.number == "INC0010482"

    # Verify GCS fallback was invoked with expected URI
    assert len(gcs_uploaded) == 1
    assert gcs_uploaded[0][0] == "gs://test-incidents-bucket/reports/grp_20261022_inv_db.html"

    # Verify call sequence: sys_user GET (0), Table API POST (1), state=2 PATCH (2), work_notes PATCH (3), Attachment API attempt (4), fallback work_notes PATCH (5), readback GET (6)
    assert len(posted_requests) == 7
    assert posted_requests[0].method == "GET"
    assert "/api/now/table/sys_user" in str(posted_requests[0].url)
    assert posted_requests[1].method == "POST"
    assert "/api/now/table/incident" in str(posted_requests[1].url)
    assert posted_requests[4].method == "POST"
    assert "/api/now/attachment/file" in str(posted_requests[4].url)


def test_append_late_alert_work_note():
    posted_requests = []
    patch_body = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "PATCH" and "/api/now/table/incident/sn-999" in str(request.url):
            patch_body.update(json.loads(request.read()))
            return httpx.Response(200, json={"result": {"sys_id": "sn-999"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    config = BridgeConfig(
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)

    alert = AlertEvent(
        incident_id="alert-late-1",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-22T21:20:00Z",
    )
    adapter.append_late_alert_work_note("sn-999", alert, blast_radius=["ata-inventory-api", "ata-orders-api"])

    assert len(posted_requests) == 1
    assert "Correlated Alert Ingested" in patch_body["work_notes"]
    assert "ata-orders-api" in patch_body["work_notes"]
    assert "Updated Blast Radius" in patch_body["work_notes"]


def test_append_resolution_work_note():
    posted_requests = []
    patch_body = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "PATCH" and "/api/now/table/incident/sn-999" in str(request.url):
            patch_body.update(json.loads(request.read()))
            return httpx.Response(200, json={"result": {"sys_id": "sn-999"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    config = BridgeConfig(
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)

    adapter.append_resolution_work_note("sn-999")

    assert len(posted_requests) == 1
    assert "Incident Resolved" in patch_body["work_notes"]


def test_insecure_http_url_raises_value_error():
    config = BridgeConfig(
        servicenow_instance_url="http://insecure.service-now.com",
        is_local=True,
    )
    with pytest.raises(ValueError, match="HTTPS"):
        ServiceNowAdapter(config=config)


def test_servicenow_state_patch_if_not_2(sample_report):
    """Assert incident is created with state New (1), work notes and attachment added, then PATCH state 2 (In Progress)."""
    posted_requests = []
    patch_calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "GET" and "/api/now/table/sys_user" in str(request.url):
            return httpx.Response(200, json={"result": [{"sys_id": "usr-admin-001"}]})
        if request.method == "POST" and "/api/now/table/incident" in str(request.url):
            # ServiceNow creates incident with state 1 (New)
            return httpx.Response(
                201,
                json={"result": {"sys_id": "sn-sys-patch123", "number": "INC009999", "state": "1"}},
            )
        if request.method == "PATCH" and "/api/now/table/incident/sn-sys-patch123" in str(request.url):
            patch_calls.append(json.loads(request.read()))
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-patch123", "state": "2"}})
        if request.method == "POST" and "/api/now/attachment/file" in str(request.url):
            return httpx.Response(201, json={"result": {"sys_id": "att-1"}})
        if request.method == "GET" and "/api/now/table/incident/sn-sys-patch123" in str(request.url):
            return httpx.Response(200, json={"result": {"sys_id": "sn-sys-patch123", "state": "2"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    config = BridgeConfig(
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)
    ref = adapter.create_incident(sample_report)

    assert ref.number == "INC009999"
    assert len(patch_calls) == 2
    assert patch_calls[0] == {"state": "2"}
    assert "work_notes" in patch_calls[1]


def test_servicenow_caller_configurable_env_var(sample_report):
    """Assert SERVICENOW_CALLER can be set via env var or config."""
    posted_requests = []
    captured_payload = {}
    patch_calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        posted_requests.append(request)
        if request.method == "POST" and "/api/now/table/incident" in str(request.url):
            captured_payload.update(json.loads(request.read()))
            return httpx.Response(201, json={"result": {"sys_id": "sn-1", "number": "INC1", "state": "1"}})
        if request.method == "PATCH" and "/api/now/table/incident/sn-1" in str(request.url):
            patch_calls.append(json.loads(request.read()))
            return httpx.Response(200, json={"result": {"sys_id": "sn-1", "state": "2"}})
        if request.method == "POST" and "/api/now/attachment/file" in str(request.url):
            return httpx.Response(201, json={"result": {"sys_id": "att-1"}})
        if request.method == "GET" and "/api/now/table/incident/sn-1" in str(request.url):
            return httpx.Response(200, json={"result": {"sys_id": "sn-1", "state": "2"}})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    # 32-character hex sys_id provided directly in SERVICENOW_CALLER
    direct_sys_id = "0123456789abcdef0123456789abcdef"
    config = BridgeConfig(
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        servicenow_caller=direct_sys_id,
        is_local=True,
    )
    adapter = ServiceNowAdapter(config=config, http_client=mock_client)
    adapter.create_incident(sample_report)

    # When SERVICENOW_CALLER is configured, sys_user lookup GET is skipped
    assert not any("/api/now/table/sys_user" in str(r.url) for r in posted_requests)
    assert captured_payload["caller_id"] == direct_sys_id
    assert captured_payload["state"] == "1"
    assert len(patch_calls) == 2
    assert patch_calls[0] == {"state": "2"}
    assert "work_notes" in patch_calls[1]


def test_action_item_code_highlighting_and_plain_text():
    """Assert commands are styled with inline CSS in HTML and indented with $ in plain text."""
    from bridge.app.contracts.report import ActionItem
    from bridge.app.templates.renderer import render_standalone_html

    report = InvestigationReport(
        group_id="grp_action_test",
        summary="Elevated 5xx error rate",
        observations="Error rate at 12%",
        recommended_actions=[
            ActionItem(
                title="Roll back Cloud Run traffic to previous stable revision",
                command="gcloud run services update-traffic ata-inventory-api --to-revisions=ata-inventory-api-00003-prev=100 --region=us-west1",
                verification="Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring",
                rollback="Re-route traffic via gcloud run services update-traffic ata-inventory-api --to-latest --region=us-west1 if regression persists.",
            ),
            ActionItem(
                title="Inspect container stdout/stderr logs for unhandled exceptions",
                command="gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"ata-inventory-api\"' --limit=20",
                verification="Confirm zero unhandled exceptions in stdout",
            ),
        ],
        origin="ata-inventory-api",
        priority="P1",
    )

    # 1. HTML Work Note
    html_notes = report.build_executive_summary_html()
    assert "[code]" in html_notes
    assert '<pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">gcloud run services update-traffic ata-inventory-api' in html_notes
    assert "Roll back Cloud Run traffic to previous stable revision" in html_notes
    assert "(Verification: Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring)" in html_notes
    assert "<b>Rollback:</b>" in html_notes

    # 2. HTML Diagnostic Report
    html_report = render_standalone_html(report)
    assert '<pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">gcloud run services update-traffic ata-inventory-api' in html_report

    # 3. Plain Text Description
    desc = report.build_structured_description()
    assert "*" not in desc
    assert "#" not in desc
    assert "  1. Roll back Cloud Run traffic to previous stable revision (Verification: Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring)" in desc
    assert "       $ gcloud run services update-traffic ata-inventory-api --to-revisions=ata-inventory-api-00003-prev=100 --region=us-west1" in desc
    assert "  2. Inspect container stdout/stderr logs for unhandled exceptions (Verification: Confirm zero unhandled exceptions in stdout)" in desc
    assert "       $ gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"ata-inventory-api\"' --limit=20" in desc
    assert "  - Rollback: Re-route traffic via gcloud run services update-traffic ata-inventory-api --to-latest --region=us-west1 if regression persists." in desc


def test_comprehensive_diagnostic_report_9_sections_and_escaping():
    """Verify HTML diagnostic report generates all 9 elaborate sections, escapes untrusted text, and includes inline CSS."""
    from bridge.app.contracts.report import ActionItem, EvidenceItem, InvestigationReport, ProbableRootCause, TimelineItem
    from bridge.app.templates.renderer import render_standalone_html

    report = InvestigationReport(
        group_id="grp_comprehensive_test_001",
        filename="incident-comprehensive-001.md",
        summary="Database connection exhaustion causing cascading 5xx on inventory service <script>alert('xss')</script>",
        observations="Active connections reached 98/100 threshold | Lock wait contention observed",
        recommended_actions=[
            ActionItem(
                title="Roll back Cloud Run traffic to previous stable revision",
                command="gcloud run services update-traffic ata-inventory-api --to-revisions=ata-inventory-api-00003-prev=100 --region=us-west1",
                verification="Confirm HTTP 5xx error rate drops below 1% via Cloud Monitoring",
                rollback="Re-route traffic via gcloud run services update-traffic ata-inventory-api --to-latest --region=us-west1 if regression persists.",
            ),
            ActionItem(
                title="Terminate blocking locks on database instance",
                command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'active' AND query LIKE '%pg_sleep%';",
                verification="Confirm pg_locks has zero ungranted locks",
            ),
        ],
        probable_root_cause=ProbableRootCause(
            text="PostgreSQL connection saturation and transaction lock wait contention on ata-inventory-db",
            confidence=0.94,
        ),
        origin="ata-inventory-db",
        blast_radius=["ata-inventory-db", "ata-inventory-api", "ata-orders-api", "ata-apigee-orders-v1"],
        priority="P1",
        status="In Progress",
        first_seen="2026-10-23T01:50:00Z",
        last_alert="2026-10-23T01:55:00Z",
        report_generated_at="2026-10-23T01:56:00Z",
        project_id="cloud-assist-fde-2",
        servicenow_number="INC0010158",
        alerts=[
            {
                "policy": "Cloud SQL Connections Saturated",
                "resource": "ata-inventory-db",
                "incident_id": "0.sql_conn_999",
                "state": "open",
                "started_at": "2026-10-23T01:50:00Z",
            },
            {
                "policy": "Cloud Run 5xx Errors",
                "resource": "ata-inventory-api",
                "incident_id": "0.cr_5xx_888",
                "state": "open",
                "started_at": "2026-10-23T01:52:00Z",
            },
        ],
        triage_decision={
            "correlation_score": 0.94,
            "grouping_rationale": "Direct causal failure chain across Database -> Backend -> Ingress within 300s window.",
            "alerts_considered_and_excluded": [
                {
                    "policy": "ata-cpu-high-unrelated",
                    "resource": "ata-analytics-worker",
                    "reason": "Outside topological blast radius.",
                }
            ],
            "duplicate_status": "Active (0 duplicate alerts suppressed into group)",
        },
        dependency_path="Apigee (ata-apigee-orders-v1) -> Cloud Run (ata-orders-api) -> Cloud Run (ata-inventory-api) -> Cloud SQL (ata-inventory-db)",
        topology_nodes=[
            {"resource": "ata-inventory-db", "type": "Cloud SQL PostgreSQL", "role": "Origin (Root Callee)", "impact": "Connection pool saturation"},
            {"resource": "ata-inventory-api", "type": "Cloud Run Service", "role": "Immediate Callee", "impact": "Cascading HTTP 5xx errors"},
            {"resource": "ata-orders-api", "type": "Cloud Run Service", "role": "Downstream Dependent", "impact": "HTTP 504 gateway timeout"},
            {"resource": "ata-apigee-orders-v1", "type": "Apigee API Proxy", "role": "Ingress Gateway", "impact": "Client requests impacted"},
        ],
        timeline=[
            TimelineItem(timestamp="2026-10-23T01:45:00Z", description="Deployment revision ata-inventory-api-00004 completed", event_type="Deployment / Change"),
            TimelineItem(timestamp="2026-10-23T01:50:00Z", description="Connection pool saturated at 98/100", event_type="Log Anomaly"),
            TimelineItem(timestamp="2026-10-23T01:50:30Z", description="Alert policy Cloud SQL Connections Saturated triggered", event_type="Alert Trigger"),
            TimelineItem(timestamp="2026-10-23T01:56:00Z", description="Triage diagnosis synthesized by multi-agent specialists", event_type="Triage Synthesis"),
        ],
        evidence_by_area={
            "database": [EvidenceItem(headline="Active Connections", content="Active connections: 98 / 100 max")],
        },
        metrics_comparison=[
            {"layer": "Cloud SQL", "metric_name": "database/network/connections", "resource": "ata-inventory-db", "before_value": "12", "during_value": "98", "variance": "+716%"},
            {"layer": "Cloud Run", "metric_name": "run.googleapis.com/request_count (5xx)", "resource": "ata-inventory-api", "before_value": "0.1%", "during_value": "14.2%", "variance": "+14100%"},
        ],
        top_log_signatures=[
            {"layer": "Cloud Run", "signature": "Database query timeout after 5000ms", "count": 42, "sample": "ERROR: /api/v1/orders db_timeout <script>"},
        ],
        apigee_analytics={
            "proxy_name": "ata-apigee-orders-v1",
            "response_codes": {"200": 1420, "500": 34, "504": 145},
            "target_latency_p50": "45ms",
            "target_latency_p95": "5400ms",
            "target_latency_p99": "5800ms",
        },
        revision_diffs=[
            {"resource": "ata-inventory-api", "current_revision": "ata-inventory-api-00004-abc", "previous_revision": "ata-inventory-api-00003-prev", "diff_summary": "Image updated to latest commit"},
        ],
        root_cause_analysis={
            "hypothesis": "PostgreSQL connection saturation and transaction lock wait contention on ata-inventory-db",
            "supporting_evidence": ["Active connections reached 98% of max_connections", "Query timeouts correlate with lock contention"],
            "contradicting_evidence": ["Zero compute host faults detected in us-west1"],
            "confidence": 0.94,
            "confidence_rationale": "Direct temporal correlation between locks, connection exhaustion, and 5xx errors.",
            "alternatives_ruled_out": [{"alternative": "Network VPC egress partition", "reason": "Internal VPC latency remained <2ms"}],
        },
        longer_term_fixes=[
            "Deploy Cloud SQL PgBouncer connection pooling sidecar to multiplex container client connections.",
            "Tune connection pool max_size in Cloud Run service configuration.",
        ],
        queries_appendix={
            "cloud_monitoring": ["fetch cloudsql_database :: cloudsql.googleapis.com/database/network/connections"],
        },
        tools_used=["list_open_alerts", "list_time_series", "list_log_entries", "get_service", "get_topology_graph", "get_blast_radius"],
        skills_loaded=["cloud-run-diagnostics", "apigee-telemetry", "cloud-sql-triage", "report-style"],
        agent_run_metadata={
            "duration_seconds": "1.74s",
            "model": "gemini-2.5-pro",
            "agent_revision": "ata-agent-00007-zzw",
            "bridge_revision": "ata-bridge-00020-p22",
        },
    )

    html = render_standalone_html(report)

    # 1. Size check: Comprehensive report must be substantial (> 5 KB)
    assert len(html.encode("utf-8")) > 5000

    # 2. Section 1: Header
    assert "INC0010158" in html
    assert "grp_comprehensive_test_001" in html
    assert "cloud-assist-fde-2" in html
    assert "2026-10-23T01:50:00Z" in html
    assert "In Progress" in html

    # 3. Section 2: Executive Summary (4-Bullet)
    assert "Executive Summary" in html
    assert "ata-inventory-db" in html
    assert "94% confidence" in html

    # 4. Section 3: Triage Decision & Grouping
    assert "Triage Decision &amp; Alert Grouping Analysis" in html or "Triage Decision" in html
    assert "0.sql_conn_999" in html
    assert "Correlation Score:" in html
    assert "0.94" in html
    assert "ata-analytics-worker" in html

    # 5. Section 4: Topology and Blast Radius
    assert "Topology &amp; Blast Radius Mapping" in html or "Topology" in html
    assert "Apigee (ata-apigee-orders-v1)" in html
    assert "Cloud SQL PostgreSQL" in html

    # 6. Section 5: Timeline
    assert "Incident Timeline (UTC Order)" in html or "Timeline" in html
    assert "Deployment / Change" in html
    assert "Alert Trigger" in html

    # 7. Section 6: Evidence Per Layer
    assert "Technical Evidence Dossier" in html
    assert "database/network/connections" in html
    assert "+716%" in html
    assert "Database query timeout after 5000ms" in html
    assert "HTTP 504" in html
    assert "5400ms" in html
    assert "ata-inventory-api-00004-abc" in html

    # 8. Section 7: Root-Cause Analysis
    assert "Probable Root Cause Analysis" in html
    assert "PostgreSQL connection saturation" in html
    assert "Network VPC egress partition" in html

    # 9. Section 8: Remediation & Longer-Term Fixes
    assert "Remediation &amp; Recovery Plan" in html or "Remediation" in html
    assert '<pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">gcloud run services update-traffic' in html
    assert '<pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">SELECT pg_terminate_backend' in html
    assert "Deploy Cloud SQL PgBouncer connection pooling sidecar" in html

    # 10. Section 9: Appendix
    assert "Appendix: Diagnostic Execution &amp; Telemetry Queries" in html or "Appendix" in html
    assert "fetch cloudsql_database" in html
    assert "ata-agent-00007-zzw" in html
    assert "cloud-run-diagnostics" in html

    # 11. Escaping check: verify raw <script> is properly escaped and NEVER rendered raw
    assert "<script>" not in html
    assert "&lt;script&gt;" in html




