"""Unit tests verifying alert scoping and defensive filtering across tools, bridge, and agent."""

from typing import Any, Dict
import pytest
import yaml
from fastapi.testclient import TestClient

from agent.app.config import AgentConfig
from agent.app.contracts.alert import (
    AlertEvent,
    is_ata_alert,
    is_ata_alert_event,
    is_ata_alert_payload,
)
from agent.app.correlation.engine import CorrelationEngine
from agent.app.main import app as agent_app
from agent.app.triage import write_preanalysis_lock
from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.main import app as bridge_app, set_dispatcher
from tools.app.server import list_open_alerts


class MockResponse:
    def __init__(self, json_data: Dict[str, Any], status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")


class MockHttpClient:
    def __init__(self, response_data: Dict[str, Any]):
        self.response_data = response_data
        self.last_url: str = ""
        self.last_params: Dict[str, Any] = {}

    def get(self, url: str, params: Dict[str, Any] = None, **kwargs) -> MockResponse:
        self.last_url = url
        self.last_params = params or {}
        return MockResponse(self.response_data)


def test_scoping_helper_criteria():
    """Assert is_ata_alert_payload matches all 4 scoping criteria and rejects foreign alerts."""
    # 1. Policy display name prefix 'ata-'
    raw_prefix = {
        "incident": {
            "incident_id": "0.1",
            "policy": {"displayName": "ata-backend-latency-breach", "name": "projects/p/alertPolicies/1"},
        }
    }
    assert is_ata_alert_payload(raw_prefix) is True

    # 2. Policy name containing 'ata-'
    raw_pol_name = {
        "incident": {
            "incident_id": "0.2",
            "policy": {"displayName": "Unprefixed Alert", "name": "projects/p/alertPolicies/ata-inventory-5xx"},
        }
    }
    assert is_ata_alert_payload(raw_pol_name) is True

    # 3. Incident summary containing 'ata-'
    raw_summary = {
        "incident": {
            "incident_id": "0.3",
            "policy": {"displayName": "Unprefixed", "name": "projects/p/alertPolicies/99"},
            "summary": "High latency detected on ata-orders-v1 proxy",
        }
    }
    assert is_ata_alert_payload(raw_summary) is True

    # 4a. user_labels containing 'app': 'alert-triage-agent'
    raw_user_labels_app = {
        "incident": {
            "incident_id": "0.4",
            "policy": {"displayName": "Generic Alert", "name": "projects/p/alertPolicies/88"},
            "user_labels": {"app": "alert-triage-agent"},
        }
    }
    assert is_ata_alert_payload(raw_user_labels_app) is True

    # 4b. user_labels containing 'managed_by': 'alert-triage-agent'
    raw_user_labels_managed = {
        "incident": {
            "incident_id": "0.5",
            "policy": {"displayName": "Generic Alert 2", "name": "projects/p/alertPolicies/77"},
            "userLabels": {"managed_by": "alert-triage-agent"},
        }
    }
    assert is_ata_alert_payload(raw_user_labels_managed) is True

    # Foreign alerts: suite_cd_001, IMA Alert
    raw_suite_cd = {
        "incident": {
            "incident_id": "0.suite1",
            "policy": {
                "name": "projects/cloud-assist-fde-2/alertPolicies/17294085183604742595",
                "displayName": "suite_cd_001: High Latency Detected in Central (>5s)",
            },
            "resource": {"type": "https_lb_rule"},
        }
    }
    assert is_ata_alert_payload(raw_suite_cd) is False

    raw_ima = {
        "incident": {
            "incident_id": "0.ima1",
            "policy": {
                "name": "projects/cloud-assist-fde-2/alertPolicies/8936123673110941053",
                "displayName": "IMA Alert: Secret Manager Permission Denied (HTTP 403)",
            },
            "resource": {"type": "k8s_container"},
        }
    }
    assert is_ata_alert_payload(raw_ima) is False


def test_list_open_alerts_filters_foreign_alerts():
    """Assert list_open_alerts returns ONLY ata alerts and filters out foreign alerts."""
    mock_payload = {
        "alerts": [
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.ata1",
                "state": "OPEN",
                "openTime": "2026-10-22T23:00:00Z",
                "resource": {"labels": {"service_name": "ata-inventory-api"}},
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/7353684053620403739",
                    "displayName": "ata-metric-test-policy",
                },
            },
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.foreign1",
                "state": "OPEN",
                "openTime": "2026-10-22T23:05:00Z",
                "resource": {"type": "https_lb_rule"},
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/17294085183604742595",
                    "displayName": "suite_cd_001: High Latency Detected in Central (>5s)",
                },
            },
            {
                "name": "projects/cloud-assist-fde-2/alerts/0.foreign2",
                "state": "OPEN",
                "openTime": "2026-10-22T23:10:00Z",
                "resource": {"type": "k8s_container"},
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/8936123673110941053",
                    "displayName": "IMA Alert: Secret Manager Permission Denied (HTTP 403)",
                },
            },
        ]
    }
    client = MockHttpClient(mock_payload)
    result = list_open_alerts(project_id="cloud-assist-fde-2", http_client=client)

    assert result["total_count"] == 1
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["incident_id"] == "0.ata1"
    assert result["alerts"][0]["policy_name"] == "projects/cloud-assist-fde-2/alertPolicies/7353684053620403739"
    assert result["alerts"][0]["summary"] == "ata-metric-test-policy"


def test_bridge_webhook_defensively_ignores_foreign_alerts():
    """Assert bridge webhook returns 200 with ignored status for foreign alert policies."""
    config = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        webhook_token="test-secret",
        is_local=True,
    )
    reset_settings(config)
    dispatcher = TaskDispatcher(config=config)
    set_dispatcher(dispatcher)

    with TestClient(bridge_app) as client:
        # 1. Foreign alert: suite_cd_001
        payload_suite = {
            "incident": {
                "incident_id": "0.suite123",
                "policy_name": "projects/cloud-assist-fde-2/alertPolicies/17294085183604742595",
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/17294085183604742595",
                    "displayName": "suite_cd_001: High Latency Detected in Central (>5s)",
                },
            }
        }
        resp = client.post("/webhook/monitoring?token=test-secret", json=payload_suite)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "non-ata alert policy"}

        # 2. Foreign alert: IMA Alert
        payload_ima = {
            "incident": {
                "incident_id": "0.ima123",
                "policy_name": "projects/cloud-assist-fde-2/alertPolicies/8936123673110941053",
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/8936123673110941053",
                    "displayName": "IMA Alert: Secret Manager Permission Denied (HTTP 403)",
                },
            }
        }
        resp_ima = client.post("/webhook/monitoring?token=test-secret", json=payload_ima)
        assert resp_ima.status_code == 200
        assert resp_ima.json() == {"status": "ignored", "reason": "non-ata alert policy"}

        # 3. ATA alert is enqueued normally
        payload_ata = {
            "incident": {
                "incident_id": "0.ata999",
                "resource_name": "ata-inventory-api",
                "policy_name": "ata-inventory-5xx",
                "policy": {
                    "name": "projects/cloud-assist-fde-2/alertPolicies/pol1",
                    "displayName": "ata-inventory-5xx",
                },
                "state": "open",
                "started_at": 1729631700,
            }
        }
        resp_ata = client.post("/webhook/monitoring?token=test-secret", json=payload_ata)
        assert resp_ata.status_code == 202
        data_ata = resp_ata.json()
        assert data_ata["status"] == "accepted"
        assert data_ata["incident_id"] == "0.ata999"

    reset_settings(None)


def test_agent_correlation_drops_non_ata_alert():
    """Assert CorrelationEngine defensively drops non-ata alerts."""
    engine = CorrelationEngine(incident_store_path="/tmp/test-incidents")

    foreign_alert = AlertEvent(
        incident_id="0.foreign_corr",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/17294085183604742595",
        summary="suite_cd_001: High Latency Detected in Central (>5s)",
        resource_name="https_lb_rule",
        state="open",
        started_at="2026-10-22T23:00:00Z",
    )

    decision = engine.evaluate_alert(foreign_alert)
    assert decision.decision == "drop"
    assert decision.best_score == 0.0
    assert "Defensively filtered" in decision.rationale


def test_agent_triage_drops_non_ata_alert(tmp_path):
    """Assert Agent /triage endpoint ignores non-ata alert without creating lock or report."""
    store_dir = tmp_path / "knowledge" / "incident-manager"
    agent_config = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )

    from agent.app.config import get_agent_settings
    agent_app.dependency_overrides[get_agent_settings] = lambda: agent_config

    client = TestClient(agent_app)

    foreign_alert = AlertEvent(
        incident_id="0.foreign_triage",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/8936123673110941053",
        summary="IMA Alert: Secret Manager Permission Denied",
        resource_name="k8s_container",
        state="open",
        started_at="2026-10-22T23:00:00Z",
    )

    resp = client.post("/triage", json=foreign_alert.model_dump(mode="json"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "non-ata alert"

    # Verify no lock file was written in store_dir
    created_files = list(store_dir.glob("*.md")) if store_dir.exists() else []
    assert len(created_files) == 0

    # Also test /investigate endpoint
    resp_inv = client.post("/investigate", json=foreign_alert.model_dump(mode="json"))
    assert resp_inv.status_code == 200
    assert resp_inv.json()["status"] == "ignored"

    agent_app.dependency_overrides.clear()


def test_write_preanalysis_lock_raises_for_non_ata_alert(tmp_path):
    """Assert write_preanalysis_lock raises ValueError for non-ata alerts."""
    store_dir = tmp_path / "knowledge" / "incident-manager"
    agent_config = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )

    foreign_alert = AlertEvent(
        incident_id="0.foreign_lock",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/999",
        summary="Foreign Alert",
        resource_name="external-vm",
        state="open",
        started_at="2026-10-22T23:00:00Z",
    )

    with pytest.raises(ValueError, match="Cannot acquire pre-analysis lock for non-ata alert"):
        write_preanalysis_lock(foreign_alert, agent_config)


def test_guarantee_rfc3339_timestamps(tmp_path):
    """Assert missing/empty started_at or timestamp defaults to valid RFC3339 UTC string."""
    store_dir = tmp_path / "knowledge" / "incident-manager"
    agent_config = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )

    # Empty started_at string
    alert_empty = AlertEvent(
        incident_id="0.empty_ts",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="",
    )
    assert alert_empty.started_at
    assert alert_empty.started_at.endswith("Z") or "+" in alert_empty.started_at

    file_path, _, _ = write_preanalysis_lock(alert_empty, agent_config)
    content = file_path.read_text(encoding="utf-8")
    parts = content.split("---")
    import yaml
    meta = yaml.safe_load(parts[1])

    assert meta["timestamp"] != ""
    assert meta["timestamp"].endswith("Z") or "+" in str(meta["timestamp"])
    assert meta["alerts"][0]["started_at"] != ""


def test_full_downstream_blast_radius_computation(tmp_path):
    """Assert blast_radius includes the origin and all downstream dependent components."""
    store_dir = tmp_path / "knowledge" / "incident-manager"
    agent_config = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )

    # Origin at ata-inventory-db
    alert_db = AlertEvent(
        incident_id="0.db_alert",
        policy_name="ata-db-saturation",
        resource_name="ata-inventory-db",
        started_at="2026-10-22T23:30:00Z",
    )

    file_path, _, _ = write_preanalysis_lock(alert_db, agent_config)
    content = file_path.read_text(encoding="utf-8")
    import yaml
    meta = yaml.safe_load(content.split("---")[1])

    blast = meta["blast_radius"]
    assert "ata-inventory-db" in blast
    assert "ata-inventory-api" in blast
    assert "ata-orders-api" in blast
    assert "ata-apigee-inventory-v1" in blast or "ata-inventory-v1" in blast
    assert "ata-apigee-orders-v1" in blast or "ata-orders-v1" in blast
    assert len(blast) >= 5


def test_update_gcs_okf_with_servicenow_details(tmp_path):
    """Assert OKF frontmatter is updated with servicenow_number, servicenow_sys_id, and status: ticketed."""
    import yaml
    from pathlib import Path
    from bridge.app.core.windowing import update_gcs_okf_servicenow

    # Create dummy local OKF file
    dummy_dir = Path("/tmp/knowledge/incident-manager")
    dummy_dir.mkdir(parents=True, exist_ok=True)
    test_file = dummy_dir / "incident-test-sn.md"
    initial_meta = {
        "type": "incident",
        "group_id": "grp_123",
        "status": "mitigation-in-progress",
        "servicenow_number": "",
        "servicenow_sys_id": "",
    }
    test_file.write_text(f"---\n{yaml.dump(initial_meta, sort_keys=False).strip()}\n---\n# Test", encoding="utf-8")

    # Mock GCS client and bucket
    class MockBlob:
        def __init__(self, name: str, text: str):
            self.name = name
            self._text = text
            self.uploaded_content = ""

        def exists(self):
            return True

        def download_as_text(self):
            return self._text

        def upload_from_string(self, content, **kwargs):
            self.uploaded_content = content

    mock_blob = MockBlob("incident-test-sn.md", test_file.read_text(encoding="utf-8"))

    class MockBucket:
        def blob(self, name):
            return mock_blob

        def list_blobs(self, **kwargs):
            return [mock_blob]

    class MockStorageClient:
        def bucket(self, name):
            return MockBucket()

    config = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        is_local=True,
    )

    success = update_gcs_okf_servicenow(
        filename="incident-test-sn.md",
        servicenow_number="INC0012345",
        servicenow_sys_id="sys_id_999",
        status="ticketed",
        config=config,
        storage_client=MockStorageClient(),
    )

    assert success is True
    # Verify local file update
    updated_local = yaml.safe_load(test_file.read_text(encoding="utf-8").split("---")[1])
    assert updated_local["servicenow_number"] == "INC0012345"
    assert updated_local["servicenow_sys_id"] == "sys_id_999"
    assert updated_local["status"] == "ticketed"

    # Verify mock blob upload
    updated_gcs = yaml.safe_load(mock_blob.uploaded_content.split("---")[1])
    assert updated_gcs["servicenow_number"] == "INC0012345"
    assert updated_gcs["servicenow_sys_id"] == "sys_id_999"
    assert updated_gcs["status"] == "ticketed"


def test_canonical_resource_normalization_cases():
    """Assert canonical normalization handles Cloud Run, Apigee, Cloud SQL, and raw regex display strings."""
    from agent.app.contracts.alert import normalize_resource_name

    # 1. Cloud Run labels
    assert normalize_resource_name("", "cloud_run_revision", {"service_name": "ata-inventory-api"}) == "ata-inventory-api"
    assert normalize_resource_name("", "cloud_run_revision", {"configuration_name": "ata-orders-api"}) == "ata-orders-api"

    # 2. Apigee labels
    assert normalize_resource_name("", "apigee_proxy", {"proxy_name": "ata-apigee-orders-v1"}) == "ata-apigee-orders-v1"
    assert normalize_resource_name("", "apigee_proxy", {"apiproxy": "ata-apigee-inventory-v1"}) == "ata-apigee-inventory-v1"
    assert normalize_resource_name("", "apigee_proxy", {"proxy": "ata-apigee-orders-v1"}) == "ata-apigee-orders-v1"

    # 3. Cloud SQL labels
    assert normalize_resource_name("", "cloudsql_database", {"database_id": "cloud-assist-fde-2:ata-inventory-db"}) == "ata-inventory-db"
    assert normalize_resource_name("", "cloudsql_database", {"instance_id": "ata-inventory-db"}) == "ata-inventory-db"

    # 4. Raw display string regex extraction
    raw_cr = "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00001}"
    assert normalize_resource_name(raw_cr) == "ata-inventory-api"

    raw_apigee = "Apigee Proxy labels {proxy_name=ata-apigee-orders-v1}"
    assert normalize_resource_name(raw_apigee) == "ata-apigee-orders-v1"

    raw_sql = "Cloud SQL labels {database_id=cloud-assist-fde-2:ata-inventory-db}"
    assert normalize_resource_name(raw_sql) == "ata-inventory-db"


def test_raw_monitoring_payload_canonicalization_and_preanalysis_lock(tmp_path):
    """Assert AlertEvent.from_monitoring_payload produces canonical resource and correct incident filename."""
    store_dir = tmp_path / "knowledge" / "incident-manager"
    agent_config = AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        is_local=True,
    )

    raw_payload = {
        "incident": {
            "incident_id": "0.real_live_123",
            "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
            "policy": {"displayName": "ata-inventory-5xx"},
            "resource_name": "cloud-assist-fde-2 Cloud Run Revision labels {project_id=cloud-assist-fde-2, service_name=ata-inventory-api, revision_name=ata-inventory-api-00001}",
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "project_id": "cloud-assist-fde-2",
                    "service_name": "ata-inventory-api",
                    "revision_name": "ata-inventory-api-00001",
                },
            },
            "started_at": "2026-10-23T00:15:00Z",
            "state": "open",
        }
    }

    event = AlertEvent.from_monitoring_payload(raw_payload)
    assert event.resource_name == "ata-inventory-api"
    assert event.resource_slug == "ata-inventory-api"

    file_path, group_id, filename = write_preanalysis_lock(event, agent_config)

    # Check filename format: incident-<policy>-<canonical-resource>-<ts>.md
    assert filename.startswith("incident-ata-inventory-5xx-ata-inventory-api-")
    assert filename.endswith(".md")
    assert len(filename) < 80  # Compact filename, not 300+ characters!

    content = file_path.read_text(encoding="utf-8")
    meta = yaml.safe_load(content.split("---")[1])
    assert meta["origin"] == "ata-inventory-api"
    assert meta["resource_name"] == "ata-inventory-api"
    # Topology blast radius must include downstream dependencies
    assert "ata-inventory-api" in meta["blast_radius"]
    assert "ata-orders-api" in meta["blast_radius"]
    assert "ata-apigee-orders-v1" in meta["blast_radius"] or "ata-orders-v1" in meta["blast_radius"]
    assert "ata-apigee-inventory-v1" in meta["blast_radius"] or "ata-inventory-v1" in meta["blast_radius"]


