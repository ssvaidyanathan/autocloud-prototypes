import json
from unittest import mock

import httpx
import pytest

from demo.scripts.create_incident import (
    SCENARIO_DEFINITIONS,
    build_incident_payload,
    send_incident,
)


def test_build_incident_payload_servicenow() -> None:
    for sc_id in range(1, 6):
        source, headers, payload = build_incident_payload(
            target="snow",
            scenario_id=sc_id,
            token="test-snow-token",
        )
        assert source == "servicenow"
        assert headers["Authorization"] == "Bearer test-snow-token"
        assert headers["Content-Type"] == "application/json"
        assert payload["cmdb_ci"] == "shop-api"
        assert payload["short_description"] == SCENARIO_DEFINITIONS[sc_id]["title"]
        assert payload["number"].startswith("INC")
        assert payload["sys_id"]
        assert "opened_at" in payload


def test_build_incident_payload_jira() -> None:
    for sc_id in range(1, 6):
        source, headers, payload = build_incident_payload(
            target="jira",
            scenario_id=sc_id,
            token="test-jira-token",
        )
        assert source == "jira"
        assert headers["X-Webhook-Token"] == "test-jira-token"
        assert "issue" in payload
        fields = payload["issue"]["fields"]
        assert fields["summary"] == SCENARIO_DEFINITIONS[sc_id]["title"]
        assert fields["components"] == [{"name": "shop-api"}]
        assert fields["priority"]["name"] in ("Highest", "High")


def test_build_incident_payload_generic() -> None:
    for sc_id in range(1, 6):
        source, headers, payload = build_incident_payload(
            target="generic",
            scenario_id=sc_id,
            token="my-secret-key",
        )
        assert source == "generic"
        assert "X-Hub-Signature-256" in headers
        assert headers["X-Hub-Signature-256"].startswith("sha256=")
        inc = payload["incident"]
        assert inc["service_hint"] == "shop-api"
        assert inc["title"] == SCENARIO_DEFINITIONS[sc_id]["title"]
        assert inc["source"] == "generic"


def test_build_incident_payload_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="Unsupported target"):
        build_incident_payload(target="unknown-target", scenario_id=1)

    with pytest.raises(ValueError, match="Unknown scenario ID"):
        build_incident_payload(target="snow", scenario_id=99)


def test_send_incident_mock_client() -> None:
    mock_client = mock.MagicMock(spec=httpx.Client)
    mock_resp = mock.MagicMock(spec=httpx.Response)
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"status": "enqueued", "task_id": "inv-12345"}
    mock_client.post.return_value = mock_resp

    resp = send_incident(
        bridge_url="http://localhost:8080",
        target="snow",
        scenario_id=1,
        token="test-token",
        client=mock_client,
    )

    assert resp.status_code == 202
    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "http://localhost:8080/webhook/servicenow"
    assert "Authorization" in kwargs["headers"]
    posted_body = json.loads(kwargs["content"])
    assert posted_body["cmdb_ci"] == "shop-api"
