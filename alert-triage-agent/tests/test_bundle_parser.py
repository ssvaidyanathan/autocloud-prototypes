"""Unit tests for in-memory Apigee proxy bundle parsing without disk writes."""

import io
import os
from pathlib import Path
from unittest.mock import patch
import zipfile
import pytest
import requests

from tools.app.bundle_parser import parse_proxy_bundle_bytes
from tools.app.server import get_proxy_bundle


def create_sample_apigee_bundle(
    target_server: str = "ata-inventory-target",
    spike_arrest_rate: str = "1pm",
    base_path: str = "/orders/v1",
) -> bytes:
    """Generate in-memory Apigee revision ZIP bundle."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Target Endpoint
        target_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<TargetEndpoint name="default">
  <PreFlow name="PreFlow"/>
  <HTTPTargetConnection>
    <Authentication>
      <GoogleIDToken>
        <Audience useTargetUrl="true"/>
      </GoogleIDToken>
    </Authentication>
    <LoadBalancer>
      <Server name="{target_server}"/>
    </LoadBalancer>
    <Path>/items</Path>
  </HTTPTargetConnection>
</TargetEndpoint>"""
        zf.writestr("apiproxy/targets/default.xml", target_xml)

        # 2. SpikeArrest Policy
        spike_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<SpikeArrest name="Spike-Arrest-Protection">
  <DisplayName>Spike Arrest Protection</DisplayName>
  <Properties/>
  <Rate>{spike_arrest_rate}</Rate>
</SpikeArrest>"""
        zf.writestr("apiproxy/policies/spike-arrest-protection.xml", spike_xml)

        # 3. MessageLogging Policy
        logging_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<MessageLogging name="Log-To-Cloud-Logging">
  <DisplayName>Log To Cloud Logging</DisplayName>
  <CloudLogging>
    <LogName>projects/cloud-assist-fde-2/logs/ata-apigee-access</LogName>
  </CloudLogging>
</MessageLogging>"""
        zf.writestr("apiproxy/policies/log-access.xml", logging_xml)

        # 4. Proxy Endpoint
        proxy_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ProxyEndpoint name="default">
  <HTTPProxyConnection>
    <BasePath>{base_path}</BasePath>
  </HTTPProxyConnection>
  <RouteRule name="default">
    <TargetEndpoint>default</TargetEndpoint>
  </RouteRule>
</ProxyEndpoint>"""
        zf.writestr("apiproxy/proxies/default.xml", proxy_xml)

        # 5. Root descriptor
        root_xml = """<APIProxy revision="1" name="ata-orders-v1"/>"""
        zf.writestr("apiproxy/ata-orders-v1.xml", root_xml)

    return buf.getvalue()


def test_bundle_parser_in_memory_zero_disk_writes(tmp_path):
    """Verify bundle unpacking runs strictly in-memory without creating disk files."""
    bundle_bytes = create_sample_apigee_bundle(
        target_server="ata-orders-target-broken",
        spike_arrest_rate="1pm",
    )

    # Monitor directory file list before and after parsing
    before_files = set(Path(".").rglob("*"))

    # Spy on built-in open to assert no file writes occur
    original_open = open
    write_calls = []

    def guard_open(file, mode="r", *args, **kwargs):
        if any(m in mode for m in ("w", "a", "x", "+")):
            write_calls.append(str(file))
        return original_open(file, mode, *args, **kwargs)

    with patch("builtins.open", side_effect=guard_open):
        result = parse_proxy_bundle_bytes(bundle_bytes, api_name="ata-orders-v1", revision="1")

    after_files = set(Path(".").rglob("*"))

    # Assert zero write calls and zero newly created files
    assert len(write_calls) == 0, f"Detected disallowed disk write calls: {write_calls}"
    assert after_files == before_files, "Filesystem state changed during in-memory parse"

    # Assert parsed structure
    assert result["api_name"] == "ata-orders-v1"
    assert result["revision"] == "1"


def test_target_server_and_spike_arrest_xml_extraction():
    """Verify exact extraction of TargetServer host and SpikeArrest rate strings."""
    bundle_bytes = create_sample_apigee_bundle(
        target_server="orders-backend-prod",
        spike_arrest_rate="100ps",
        base_path="/orders",
    )

    result = parse_proxy_bundle_bytes(bundle_bytes, api_name="ata-orders-v1", revision="3")

    # 1. Target server extraction
    assert len(result["target_endpoints"]) == 1
    target = result["target_endpoints"][0]
    assert target["name"] == "default"
    assert "orders-backend-prod" in target["servers"]
    assert target["has_google_id_token"] is True

    # 2. SpikeArrest extraction
    assert len(result["spike_arrest_policies"]) == 1
    spike = result["spike_arrest_policies"][0]
    assert spike["name"] == "Spike-Arrest-Protection"
    assert spike["type"] == "SpikeArrest"
    assert spike["rate"] == "100ps"

    # 3. Base path extraction
    assert result["base_paths"] == ["/orders"]


def test_get_proxy_bundle_fastmcp_tool_integration():
    """Assert get_proxy_bundle tool functions properly with bundle_bytes."""
    bundle_bytes = create_sample_apigee_bundle(
        target_server="ata-invalid-backend:443",
        spike_arrest_rate="1pm",
    )

    tool_result = get_proxy_bundle(
        api_name="ata-orders-v1",
        revision="2",
        bundle_bytes=bundle_bytes,
    )

    assert tool_result["api_name"] == "ata-orders-v1"
    assert tool_result["revision"] == "2"
    assert tool_result["target_endpoints"][0]["servers"] == ["ata-invalid-backend:443"]
    assert tool_result["spike_arrest_policies"][0]["rate"] == "1pm"


def test_get_proxy_bundle_corrupted_bytes_returns_structured_error():
    """Assert get_proxy_bundle handles corrupted/non-zip bytes returning structured diagnostic error dict."""
    corrupted_bytes = b"CORRUPTED_NON_ZIP_DATA_STREAM"

    result = get_proxy_bundle(
        api_name="ata-orders-v1",
        revision="2",
        bundle_bytes=corrupted_bytes,
    )

    assert result["api_name"] == "ata-orders-v1"
    assert result["revision"] == "2"
    assert "error" in result
    assert "File is not a zip file" in result["error"] or len(result["error"]) > 0
    assert result["target_endpoints"] == []
    assert result["spike_arrest_policies"] == []
    assert result["policies"] == []
    assert result["base_paths"] == []
    assert result["proxy_endpoints"] == []


def test_get_proxy_bundle_without_apigee_org_returns_error_and_makes_no_call(monkeypatch):
    """No APIGEE_ORG and no org argument: a structured error naming APIGEE_ORG, never PROJECT_ID."""
    from tools.app.config import get_tools_config
    monkeypatch.setattr(get_tools_config(), "apigee_org", None)
    with patch("requests.get") as mock_get:
        result = get_proxy_bundle(api_name="ata-orders-v1", revision="1", bundle_bytes=None)
    mock_get.assert_not_called()
    assert "APIGEE_ORG not set" in result["error"]
    assert result["target_endpoints"] == []


def test_get_proxy_bundle_download_http_error_returns_structured_error():
    """Assert get_proxy_bundle handles HTTP failure when downloading bundle from Apigee API."""
    with patch("requests.get") as mock_get:
        mock_resp = mock_get.return_value
        mock_resp.raise_for_status.side_effect = requests.HTTPError("404 Not Found")

        result = get_proxy_bundle(
            api_name="ata-nonexistent-api",
            revision="1",
            org="test-org",
            bundle_bytes=None,
        )

        assert result["api_name"] == "ata-nonexistent-api"
        assert result["revision"] == "1"
        assert "404 Not Found" in result["error"]
        assert result["target_endpoints"] == []
        assert result["spike_arrest_policies"] == []
        assert result["policies"] == []
        assert result["base_paths"] == []
        assert result["proxy_endpoints"] == []
