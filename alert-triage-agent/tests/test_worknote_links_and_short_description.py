import html
import pytest
from pydantic import BaseModel

from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import (
    InvestigationReport as AgentReport,
    clean_problem_text as agent_clean_problem,
    balance_brackets as agent_balance_brackets,
)
from bridge.app.contracts.report import (
    InvestigationReport as BridgeReport,
    clean_problem_text as bridge_clean_problem,
    balance_brackets as bridge_balance_brackets,
)
from agent.app.grouping.suppression import format_late_alert_work_note
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
import httpx


class TestWorkNoteLinks:
    """Tests for Cloud Console links in 'Correlated Alert Ingested' work notes."""

    def test_note_includes_payload_url(self):
        """When incident.url is provided in payload, it is preferred."""
        payload_url = "https://console.cloud.google.com/monitoring/alerting/incidents/0.payload123?project=custom-proj"
        alert = AlertEvent(
            incident_id="0.payload123",
            policy_name="ata-inventory-5xx",
            resource_name="ata-inventory-api",
            url=payload_url,
        )
        note = format_late_alert_work_note(alert, blast_radius=["ata-inventory-api"], project_id="custom-proj")

        assert payload_url in note
        assert f'href="{payload_url}"' in note
        assert "0.payload123" in note
        assert "Correlated Alert Ingested" in note

    def test_note_fallback_canonical_link_when_url_missing(self):
        """When incident.url is missing, fallback to ConsoleLinkBuilder canonical incident URL."""
        alert = AlertEvent(
            incident_id="0.fallback456",
            policy_name="ata-orders-latency",
            resource_name="ata-orders-api",
            url=None,
        )
        note = format_late_alert_work_note(alert, blast_radius=["ata-orders-api"], project_id="cloud-assist-fde-2")

        expected_url = "https://console.cloud.google.com/monitoring/alerting/incidents/0.fallback456?project=cloud-assist-fde-2"
        assert expected_url in note
        assert f'href="{expected_url}"' in note
        assert "0.fallback456" in note

    def test_no_link_when_incident_id_is_missing(self):
        """When incident_id is missing, empty, or placeholder, do not render an incident link."""
        # Test None / empty
        alert_no_id = AlertEvent(
            incident_id="",
            policy_name="ata-policy",
            resource_name="ata-service",
        )
        # Note: AlertEvent validator might allow empty or string
        note_empty = format_late_alert_work_note(alert_no_id, project_id="cloud-assist-fde-2")
        assert "incidents/" not in note_empty
        assert "href" not in note_empty.split("Alert ID:")[1]

        # Test N/A placeholder
        alert_na = AlertEvent(
            incident_id="N/A",
            policy_name="ata-policy",
            resource_name="ata-service",
        )
        note_na = format_late_alert_work_note(alert_na, project_id="cloud-assist-fde-2")
        assert "incidents/" not in note_na
        assert "href" not in note_na.split("Alert ID:")[1]
        assert "(Alert ID: N/A)" in note_na

    def test_policy_link_with_numeric_id(self):
        """Policy link uses the numeric policy ID if present."""
        alert = AlertEvent(
            incident_id="0.test1234",
            policy_name="High Error Rate",
            policy_id="9876543210",
            resource_name="ata-orders-api",
        )
        note = format_late_alert_work_note(alert, project_id="test-proj")
        expected_pol_url = "https://console.cloud.google.com/monitoring/alerting/policies/9876543210?project=test-proj"
        assert expected_pol_url in note
        assert f'href="{expected_pol_url}"' in note

    def test_policy_link_fallback_to_list_page(self):
        """Policy link falls back to policies list page when numeric ID is absent."""
        alert = AlertEvent(
            incident_id="0.test1234",
            policy_name="Named Policy Without Numeric ID",
            policy_id=None,
            resource_name="ata-orders-api",
        )
        note = format_late_alert_work_note(alert, project_id="test-proj")
        expected_list_url = "https://console.cloud.google.com/monitoring/alerting/policies?project=test-proj"
        assert expected_list_url in note
        assert f'href="{expected_list_url}"' in note

    def test_html_escaping_in_work_note(self):
        """Ensure all fields are properly HTML-escaped to prevent XSS."""
        alert = AlertEvent(
            incident_id='<alert"&>',
            policy_name='<script>alert("xss")</script>',
            resource_name='<img src=x onerror=alert(1)>',
            url='https://console.cloud.google.com/monitoring/alerting/incidents/test?a=1&b=2',
        )
        note = format_late_alert_work_note(
            alert,
            blast_radius=['<b style="color:red">bad</b>'],
            project_id="test-proj",
        )
        assert "<script>" not in note
        assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in note
        assert "<img" not in note
        assert "&lt;img src=x onerror=alert(1)&gt;" in note
        assert '<b style="color:red">' not in note
        assert "&lt;b style=&quot;color:red&quot;&gt;bad&lt;/b&gt;" in note
        assert "a=1&amp;b=2" in note

    def test_plain_text_rendering(self):
        """Plain text rendering renders clean URLs and no HTML tags."""
        alert = AlertEvent(
            incident_id="0.plain123",
            policy_name="ata-orders-5xx",
            resource_name="ata-orders-api",
            url="https://console.cloud.google.com/monitoring/alerting/incidents/0.plain123?project=test-proj",
        )
        note = format_late_alert_work_note(
            alert,
            blast_radius=["ata-inventory-api", "ata-orders-api"],
            project_id="test-proj",
            as_html=False,
        )
        assert "[code]" not in note
        assert "</a>" not in note
        assert "<code>" not in note
        assert "Correlated Alert Ingested: ata-orders-5xx" in note
        assert "https://console.cloud.google.com/monitoring/alerting/incidents/0.plain123?project=test-proj" in note
        assert "Updated Blast Radius: ata-inventory-api, ata-orders-api" in note

    def test_servicenow_adapter_append_late_alert_work_note(self):
        """ServiceNowAdapter append_late_alert_work_note renders links and posts to ServiceNow."""
        posted_requests = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            posted_requests.append(request)
            return httpx.Response(200, json={"result": {"sys_id": "sn-test"}})

        mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
        config = BridgeConfig(
            servicenow_instance_url="https://fake.service-now.com",
            project_id="test-proj",
            is_local=True,
        )
        adapter = ServiceNowAdapter(config=config, http_client=mock_client)

        payload_url = "https://console.cloud.google.com/monitoring/alerting/incidents/0.sn999?project=test-proj"
        alert = AlertEvent(
            incident_id="0.sn999",
            policy_name="ata-orders-5xx",
            policy_id="54321",
            resource_name="ata-orders-api",
            url=payload_url,
        )
        adapter.append_late_alert_work_note("sn-test", alert, blast_radius=["ata-orders-api"])

        assert len(posted_requests) == 1
        body = posted_requests[0].read().decode()
        assert "Correlated Alert Ingested" in body
        assert payload_url in body
        assert "https://console.cloud.google.com/monitoring/alerting/policies/54321?project=test-proj" in body

    def test_unknown_project_omits_console_links_rather_than_guessing(self):
        """When project_id is unknown/None and no payload url exists, omit console links entirely."""
        alert = AlertEvent(
            incident_id="0.no-proj-1",
            policy_name="ata-inventory-5xx",
            resource_name="ata-inventory-api",
            url=None,
        )
        note = format_late_alert_work_note(alert, blast_radius=["ata-inventory-api"], project_id=None)

        assert "console.cloud.google.com" not in note
        assert "<a " not in note
        assert "href" not in note
        assert "<b>Correlated Alert Ingested:</b> ata-inventory-5xx on <code>ata-inventory-api</code> (Alert ID: 0.no-proj-1)" in note

    def test_unknown_project_preserves_payload_url_without_guessing_policy(self):
        """When project_id is unknown but payload url is provided, use payload url and omit policy link."""
        payload_url = "https://console.cloud.google.com/monitoring/alerting/incidents/0.sn123?project=actual-proj"
        alert = AlertEvent(
            incident_id="0.sn123",
            policy_name="ata-orders-5xx",
            resource_name="ata-orders-api",
            url=payload_url,
        )
        note = format_late_alert_work_note(alert, blast_radius=["ata-orders-api"], project_id=None)

        assert payload_url in note
        assert f'href="{payload_url}"' in note
        # Policy link should NOT be guessed since project_id is unknown
        assert "policies" not in note

    def test_servicenow_adapter_unknown_project_omits_console_links(self):
        """ServiceNowAdapter omits console links when no project is configured or passed."""
        posted_requests = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            posted_requests.append(request)
            return httpx.Response(200, json={"result": {"sys_id": "sn-test"}})

        mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
        config = BridgeConfig(
            servicenow_instance_url="https://fake.service-now.com",
            project_id="",
            is_local=True,
        )
        adapter = ServiceNowAdapter(config=config, http_client=mock_client)

        alert = AlertEvent(
            incident_id="0.sn999",
            policy_name="ata-orders-5xx",
            policy_id="54321",
            resource_name="ata-orders-api",
            url=None,
        )
        adapter.append_late_alert_work_note("sn-test", alert, blast_radius=["ata-orders-api"], project_id=None)

        assert len(posted_requests) == 1
        body = posted_requests[0].read().decode()
        assert "Correlated Alert Ingested" in body
        assert "console.cloud.google.com" not in body
        assert "<a " not in body


class TestShortDescriptionCleaning:
    """Tests for build_short_description bracket balancing, regex cleaning, and length cap."""

    @pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
    def test_reproduce_and_fix_double_parens(self, ReportCls):
        """Reproduce the bug where stripped policy/labels free text left '((1 alert)'."""
        # In real tickets, summary was generated as:
        # 'Cloud Monitoring alert triggered by threshold breach on ata-inventory-api (projects/123/alertPolicies/456)'
        report = ReportCls(
            group_id="test-grp",
            origin="ata-inventory-api",
            priority="P1",
            summary="Cloud Monitoring alert triggered by threshold breach on ata-inventory-api (projects/cloud-assist-fde-2/alertPolicies/123456)",
            observations="Active anomaly",
            alerts=[{"incident_id": "alert-1", "state": "open"}],
        )
        short_desc = report.build_short_description(max_length=160)

        # Bug produced '... ata-inventory-api ((1 alert)' or '... () (1 alert)'
        assert "((" not in short_desc
        assert "( )" not in short_desc
        assert "()" not in short_desc
        assert short_desc.endswith("(1 alert)")
        assert short_desc.count("(") == short_desc.count(")")

    @pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
    def test_reproduce_and_fix_labels_stripped_bracket_fragment(self, ReportCls):
        """Labels stripping leaves trailing parenthesis fragment '... (' producing '((1 alert)'."""
        report = ReportCls(
            group_id="test-grp",
            origin="ata-orders-api",
            priority="P2",
            summary="Elevated latency on ata-orders-api (labels{service_name=ata-orders-api, env=prod})",
            observations="Latency > 5s",
            alerts=[{"incident_id": "a1"}, {"incident_id": "a2"}],
        )
        short_desc = report.build_short_description(max_length=160)

        assert "((" not in short_desc
        assert "( )" not in short_desc
        assert "()" not in short_desc
        assert short_desc.endswith("(2 alerts)")
        assert short_desc.count("(") == short_desc.count(")")

    @pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
    def test_unbalanced_brackets_cleaned(self, ReportCls):
        """Various unbalanced bracket fragments are cleanly balanced without '((' or '( )'."""
        cases = [
            "Database connection failure (",
            "Database connection failure )",
            "Database connection failure ( )",
            "Database connection failure ((projects/123/alertPolicies/456))",
            "Database connection failure [labels{foo=bar}]",
            "Database connection failure: - ; ",
        ]
        for c in cases:
            report = ReportCls(
                group_id="grp",
                origin="ata-db",
                priority="P1",
                summary=c,
                observations="test",
                alerts=[{"incident_id": "a1"}],
            )
            desc = report.build_short_description(max_length=160)
            assert "((" not in desc, f"Failed on '{c}': got '{desc}'"
            assert "( )" not in desc, f"Failed on '{c}': got '{desc}'"
            assert "()" not in desc, f"Failed on '{c}': got '{desc}'"
            assert desc.count("(") == desc.count(")"), f"Unbalanced parens on '{c}': got '{desc}'"
            assert desc.count("[") == desc.count("]"), f"Unbalanced brackets on '{c}': got '{desc}'"

    @pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
    def test_structured_fields_fallback_when_summary_completely_stripped(self, ReportCls):
        """When summary only contained policy path or labels, build from structured fields."""
        report = ReportCls(
            group_id="grp",
            origin="ata-inventory-api",
            priority="P1",
            summary="(projects/cloud-assist-fde-2/alertPolicies/999999)",
            issue_type="runtime_exhaustion",
            observations="exhausted",
            alerts=[{"incident_id": "a1"}],
        )
        desc = report.build_short_description(max_length=160)
        assert "[P1] ata-inventory-api:" in desc
        assert "Resource exhaustion" in desc
        assert desc.endswith("(1 alert)")
        assert "((" not in desc
        assert desc.count("(") == desc.count(")")

    @pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
    def test_length_cap_strictly_respected(self, ReportCls):
        """The max_length cap is respected under short and long caps without broken brackets."""
        very_long_summary = (
            "Extremely long problem statement describing an intricate cascading breakdown "
            "across multiple microservices beginning with Cloud SQL connection saturation "
            "and progressing into widespread HTTP 5xx errors across downstream API gateways."
        )
        for max_len in [50, 80, 100, 120, 160]:
            report = ReportCls(
                group_id="grp",
                origin="ata-inventory-api",
                priority="P1",
                summary=very_long_summary,
                observations="test",
                alerts=[{"incident_id": "a1"}],
            )
            desc = report.build_short_description(max_length=max_len)
            assert len(desc) <= max_len, f"Exceeded max_len={max_len}: len={len(desc)} -> '{desc}'"
            assert "((" not in desc
            assert "( )" not in desc
            assert desc.count("(") == desc.count(")")
            assert desc.count("[") == desc.count("]")

    @pytest.mark.parametrize("clean_fn", [agent_clean_problem, bridge_clean_problem])
    def test_clean_problem_text_preserves_legitimate_parentheses(self, clean_fn):
        """Legitimate balanced parentheses with valid content are preserved."""
        text = "Database pool saturated (active=25, max=25)"
        assert clean_fn(text) == text

        mixed = "Database pool saturated (active=25) (projects/p/alertPolicies/1)"
        assert clean_fn(mixed) == "Database pool saturated (active=25)"
