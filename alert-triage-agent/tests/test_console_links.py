import re
import urllib.parse
import pytest

from bridge.app.contracts.links import ConsoleLinkBuilder
from bridge.app.contracts.report import InvestigationReport, ActionItem
from bridge.app.templates.renderer import render_standalone_html


@pytest.fixture
def builder():
    return ConsoleLinkBuilder(project_id="cloud-assist-fde-2", region="us-west1")


def test_monitoring_incident_url(builder):
    # Direct incident ID
    url1 = builder.build_incident_url("0.m182379")
    assert url1 == "https://console.cloud.google.com/monitoring/alerting/incidents/0.m182379?project=cloud-assist-fde-2"

    # Prefixed incident path
    url2 = builder.build_incident_url("projects/cloud-assist-fde-2/incidents/0.m999999")
    assert url2 == "https://console.cloud.google.com/monitoring/alerting/incidents/0.m999999?project=cloud-assist-fde-2"

    # Already full URL (from webhook incident.url)
    webhook_url = "https://console.cloud.google.com/monitoring/alerting/incidents/0.m182379?project=cloud-assist-fde-2"
    url3 = builder.build_incident_url(webhook_url)
    assert url3 == webhook_url

    # Group ID should not be treated as incident ID -> falls back to alerting overview
    url4 = builder.build_incident_url("grp_test_group_001")
    assert url4 == "https://console.cloud.google.com/monitoring/alerting?project=cloud-assist-fde-2"

    # Empty / None fallback
    url5 = builder.build_incident_url(None)
    assert url5 == "https://console.cloud.google.com/monitoring/alerting?project=cloud-assist-fde-2"


def test_alert_policy_url(builder):
    # Pure numeric ID
    url1 = builder.build_policy_url("1638291048123")
    assert url1 == "https://console.cloud.google.com/monitoring/alerting/policies/1638291048123?project=cloud-assist-fde-2"

    # Full policy resource path
    url2 = builder.build_policy_url("projects/cloud-assist-fde-2/alertPolicies/1638291048123")
    assert url2 == "https://console.cloud.google.com/monitoring/alerting/policies/1638291048123?project=cloud-assist-fde-2"

    # Non-numeric policy display name without numeric ID -> falls back to policies list
    url3 = builder.build_policy_url("ata-high-5xx-rate")
    assert url3 == "https://console.cloud.google.com/monitoring/alerting/policies?project=cloud-assist-fde-2"

    # Already full URL
    pol_full = "https://console.cloud.google.com/monitoring/alerting/policies/1638291048123?project=cloud-assist-fde-2"
    url4 = builder.build_policy_url(pol_full)
    assert url4 == pol_full

    # Empty / None fallback
    url5 = builder.build_policy_url("")
    assert url5 == "https://console.cloud.google.com/monitoring/alerting/policies?project=cloud-assist-fde-2"


def test_dashboard_url(builder):
    url = builder.build_dashboard_url()
    assert url == "https://console.cloud.google.com/home/dashboard?project=cloud-assist-fde-2"


def test_cloud_run_service_url(builder):
    # Metrics tab
    url1 = builder.build_cloud_run_service_url("ata-inventory-api", tab="metrics")
    assert url1 == "https://console.cloud.google.com/run/detail/us-west1/ata-inventory-api/metrics?project=cloud-assist-fde-2"

    # Revisions tab
    url2 = builder.build_cloud_run_service_url("ata-inventory-api", tab="revisions")
    assert url2 == "https://console.cloud.google.com/run/detail/us-west1/ata-inventory-api/revisions?project=cloud-assist-fde-2"

    # URL encoding for special chars
    custom_builder = ConsoleLinkBuilder(project_id="test-proj", region="europe-west1")
    url3 = custom_builder.build_cloud_run_service_url("my service")
    assert url3 == "https://console.cloud.google.com/run/detail/europe-west1/my%20service/metrics?project=test-proj"


def test_cloud_sql_instance_url(builder):
    # Simple instance name
    url1 = builder.build_cloud_sql_instance_url("ata-inventory-db")
    assert url1 == "https://console.cloud.google.com/sql/instances/ata-inventory-db/overview?project=cloud-assist-fde-2"

    # Project:instance format
    url2 = builder.build_cloud_sql_instance_url("cloud-assist-fde-2:ata-inventory-db")
    assert url2 == "https://console.cloud.google.com/sql/instances/ata-inventory-db/overview?project=cloud-assist-fde-2"

    # Resource path format
    url3 = builder.build_cloud_sql_instance_url("projects/cloud-assist-fde-2/instances/ata-inventory-db")
    assert url3 == "https://console.cloud.google.com/sql/instances/ata-inventory-db/overview?project=cloud-assist-fde-2"


def test_apigee_proxy_url(builder):
    # Overview
    url1 = builder.build_apigee_proxy_url("ata-apigee-orders-v1")
    assert url1 == "https://console.cloud.google.com/apigee/proxies/ata-apigee-orders-v1/overview?project=cloud-assist-fde-2"

    # Deployments / develop revision
    url2 = builder.build_apigee_proxy_url("ata-apigee-orders-v1", revision="2")
    assert url2 == "https://console.cloud.google.com/apigee/proxies/ata-apigee-orders-v1/develop/2?project=cloud-assist-fde-2"


def test_logs_explorer_url(builder):
    # Query without timeRange
    query = 'resource.type="cloud_run_revision" AND severity>=ERROR'
    url1 = builder.build_logs_explorer_url(query)
    enc_q = urllib.parse.quote(query, safe="")
    assert url1 == f"https://console.cloud.google.com/logs/query;query={enc_q}?project=cloud-assist-fde-2"

    # Query with timeRange
    start_ts = "2026-10-23T01:00:00Z"
    end_ts = "2026-10-23T02:00:00Z"
    url2 = builder.build_logs_explorer_url(query, start_time=start_ts, end_time=end_ts)
    enc_start = urllib.parse.quote(start_ts, safe="")
    enc_end = urllib.parse.quote(end_ts, safe="")
    assert f";query={enc_q};timeRange={enc_start}%2F{enc_end}?project=cloud-assist-fde-2" in url2


def test_metrics_explorer_url(builder):
    # Falls back to policy url if numeric policy_id provided
    url1 = builder.build_metrics_explorer_url("ata-inventory-api", policy_id="12345")
    assert url1 == "https://console.cloud.google.com/monitoring/alerting/policies/12345?project=cloud-assist-fde-2"

    # Falls back to resource url if no policy_id
    url2 = builder.build_metrics_explorer_url("ata-inventory-api")
    assert url2 == "https://console.cloud.google.com/run/detail/us-west1/ata-inventory-api/metrics?project=cloud-assist-fde-2"


def test_resource_url_routing(builder):
    # Cloud SQL
    url_sql = builder.build_resource_url("ata-inventory-db")
    assert "/sql/instances/ata-inventory-db/overview" in url_sql

    # Apigee
    url_apigee = builder.build_resource_url("ata-apigee-orders-v1")
    assert "/apigee/proxies/ata-apigee-orders-v1/overview" in url_apigee

    # Cloud Run
    url_run = builder.build_resource_url("ata-inventory-api")
    assert "/run/detail/us-west1/ata-inventory-api/metrics" in url_run


ALLOWED_HREF_PATTERN = re.compile(
    r"^https://console\.cloud\.google\.com/("
    r"home/dashboard\?project=[^\"'&]+"
    r"|monitoring/alerting/incidents/[^\"'&?]+\?project=[^\"'&]+"
    r"|monitoring/alerting/policies/\d+\?project=[^\"'&]+"
    r"|monitoring/alerting(\?project=[^\"'&]+|/policies\?project=[^\"'&]+)"
    r"|run/detail/[^/\"']+/([^/\"']+)/(metrics|revisions)\?project=[^\"'&]+"
    r"|run\?project=[^\"'&]+"
    r"|sql/instances/[^/\"']+/overview\?project=[^\"'&]+"
    r"|sql/instances\?project=[^\"'&]+"
    r"|apigee/proxies/[^/\"']+/(overview|develop/[^/\"'?]+)\?project=[^\"'&]+"
    r"|apigee/proxies\?project=[^\"'&]+"
    r"|logs/query(;query=[^\"'&;?]+)?(;timeRange=[^\"'&;?]+)?\?project=[^\"'&]+"
    r")$"
)


def test_every_href_matches_allowed_pattern():
    """Assert that 100% of <a href="..."> links rendered across all 9 sections match canonical Google Cloud Console patterns."""
    report = InvestigationReport(
        group_id="grp-sweep-all-links",
        summary="High 5xx error rate on backend",
        observations="Observed connection pool saturation on database",
        origin="ata-inventory-db",
        blast_radius=["ata-inventory-api", "ata-orders-api", "ata-apigee-orders-v1"],
        priority="P1",
        project_id="cloud-assist-fde-2",
        region="us-west1",
        alerts=[
            {
                "policy": "Cloud SQL High Connections",
                "policy_id": "1638291048123",
                "resource": "ata-inventory-db",
                "resource_type": "cloudsql_database",
                "incident_id": "0.m182379",
                "state": "open",
                "started_at": "2026-10-23T01:00:00Z",
                "url": "https://console.cloud.google.com/monitoring/alerting/incidents/0.m182379?project=cloud-assist-fde-2",
            },
            {
                "policy": "Backend 5xx Error Rate",
                "policy_name": "projects/cloud-assist-fde-2/alertPolicies/1638291048456",
                "resource": "ata-inventory-api",
                "resource_type": "cloud_run_revision",
                "incident_id": "0.m182380",
                "state": "open",
                "started_at": "2026-10-23T01:05:00Z",
            }
        ],
        topology_nodes=[
            {
                "resource": "ata-inventory-db",
                "type": "Cloud SQL PostgreSQL",
                "role": "Origin",
                "impact": "Saturated",
            },
            {
                "resource": "ata-inventory-api",
                "type": "Cloud Run Service",
                "role": "Dependent",
                "impact": "5xx errors",
            },
            {
                "resource": "ata-apigee-orders-v1",
                "type": "Apigee API Proxy",
                "role": "Ingress",
                "impact": "503 responses",
            }
        ],
        metrics_comparison=[
            {
                "layer": "Cloud SQL",
                "metric_name": "cloudsql.googleapis.com/database/postgresql/num_backends",
                "resource": "ata-inventory-db",
                "before_value": "10",
                "during_value": "100",
                "variance": "+900%",
            },
            {
                "layer": "Cloud Run",
                "metric_name": "run.googleapis.com/request_count",
                "resource": "ata-inventory-api",
                "before_value": "1",
                "during_value": "45",
                "variance": "+4400%",
            }
        ],
        top_log_signatures=[
            {
                "layer": "Cloud Run",
                "signature": "remaining connection slots are reserved",
                "count": 42,
                "sample": "FATAL: remaining connection slots are reserved",
            }
        ],
        apigee_analytics={
            "proxy_name": "ata-apigee-orders-v1",
            "response_codes": {"503": 85},
        },
        revision_diffs=[
            {
                "resource": "ata-inventory-api",
                "current_revision": "ata-inventory-api-00005",
                "previous_revision": "ata-inventory-api-00004",
                "diff_summary": "DB pool max size increased",
            },
            {
                "resource": "ata-apigee-orders-v1",
                "current_revision": "2",
                "previous_revision": "1",
                "diff_summary": "Proxy target endpoint update",
            }
        ],
        queries_appendix={
            "logging": ['resource.type="cloud_run_revision" AND severity>=ERROR'],
            "metrics": ['fetch cloudsql_database | metric "num_backends"'],
        },
    )

    html = render_standalone_html(report)

    # Extract all href attributes
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert len(hrefs) > 0, "No href links found in report!"

    for href in hrefs:
        assert ALLOWED_HREF_PATTERN.match(href), f"Disallowed or malformed URL found in report: {href}"


def test_structured_description_canonical_links():
    report = InvestigationReport(
        group_id="grp-test-links-02",
        summary="High 5xx error rate",
        observations="Observed connection saturation",
        origin="ata-inventory-db",
        blast_radius=["ata-inventory-api"],
        priority="P1",
        project_id="cloud-assist-fde-2",
        region="us-west1",
        alerts=[
            {
                "policy": "Cloud SQL High Connections",
                "resource": "ata-inventory-db",
                "incident_id": "0.m987654",
                "state": "open",
                "started_at": "2026-10-23T01:00:00Z",
            }
        ],
    )

    desc = report.build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="test-project-ata-incidents")

    # Verify canonical incident URL is present in alert line
    assert "https://console.cloud.google.com/monitoring/alerting/incidents/0.m987654?project=cloud-assist-fde-2" in desc

    # Verify ZERO markdown asterisks and ZERO hashes
    assert "*" not in desc
    assert "#" not in desc
