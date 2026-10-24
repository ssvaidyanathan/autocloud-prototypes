"""In-memory Apigee proxy bundle parser.

Parses revision ZIP bundles completely in memory without local disk writes.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET
import zipfile


def parse_proxy_bundle_bytes(
    bundle_bytes: bytes,
    api_name: str = "unknown",
    revision: str = "1",
) -> Dict[str, Any]:
    """Parse Apigee revision ZIP bundle in memory into structured configuration.
    
    Args:
        bundle_bytes: Raw bytes of Apigee proxy bundle ZIP.
        api_name: Proxy API name.
        revision: Revision number string.
        
    Returns:
        Structured dictionary containing target endpoints, SpikeArrest policies,
        base paths, and all policies.
    """
    target_endpoints: List[Dict[str, Any]] = []
    spike_arrest_policies: List[Dict[str, Any]] = []
    policies: List[Dict[str, Any]] = []
    base_paths: List[str] = []
    proxy_endpoints: List[Dict[str, Any]] = []

    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
        for filename in zf.namelist():
            # Target endpoints
            if filename.startswith("apiproxy/targets/") and filename.endswith(".xml"):
                content = zf.read(filename).decode("utf-8")
                target_endpoints.append(_parse_target_endpoint(content, filename))

            # Policies (SpikeArrest, VerifyAPIKey, MessageLogging, etc.)
            elif filename.startswith("apiproxy/policies/") and filename.endswith(".xml"):
                content = zf.read(filename).decode("utf-8")
                pol_data = _parse_policy(content, filename)
                policies.append(pol_data)
                if pol_data.get("type") == "SpikeArrest":
                    spike_arrest_policies.append(pol_data)

            # Proxy endpoints (base paths, route rules)
            elif filename.startswith("apiproxy/proxies/") and filename.endswith(".xml"):
                content = zf.read(filename).decode("utf-8")
                proxy_ep = _parse_proxy_endpoint(content, filename)
                proxy_endpoints.append(proxy_ep)
                if proxy_ep.get("base_path"):
                    base_paths.append(proxy_ep["base_path"])

    return {
        "api_name": api_name,
        "revision": str(revision),
        "target_endpoints": target_endpoints,
        "spike_arrest_policies": spike_arrest_policies,
        "policies": policies,
        "base_paths": base_paths,
        "proxy_endpoints": proxy_endpoints,
    }


def _parse_target_endpoint(xml_content: str, filename: str) -> Dict[str, Any]:
    """Parse TargetEndpoint XML document."""
    try:
        root = ET.fromstring(xml_content)
        name = root.attrib.get("name") or filename.split("/")[-1].replace(".xml", "")

        url = None
        url_elem = root.find(".//HTTPTargetConnection/URL")
        if url_elem is not None and url_elem.text:
            url = url_elem.text.strip()

        servers: List[str] = []
        for srv in root.findall(".//HTTPTargetConnection/LoadBalancer/Server"):
            srv_name = srv.attrib.get("name")
            if srv_name:
                servers.append(srv_name)

        has_google_id_token = root.find(".//HTTPTargetConnection/Authentication/GoogleIDToken") is not None

        return {
            "name": name,
            "filename": filename,
            "url": url,
            "servers": servers,
            "has_google_id_token": has_google_id_token,
        }
    except Exception as e:
        return {"name": filename, "error": str(e)}


def _parse_policy(xml_content: str, filename: str) -> Dict[str, Any]:
    """Parse policy XML document."""
    try:
        root = ET.fromstring(xml_content)
        tag = root.tag
        name = root.attrib.get("name") or filename.split("/")[-1].replace(".xml", "")

        data: Dict[str, Any] = {
            "name": name,
            "type": tag,
            "filename": filename,
        }

        if tag == "SpikeArrest":
            rate_elem = root.find(".//Rate")
            data["rate"] = rate_elem.text.strip() if rate_elem is not None and rate_elem.text else None
            effective_param = root.find(".//UseEffectiveParam")
            data["use_effective_param"] = (
                effective_param.text.strip() if effective_param is not None and effective_param.text else None
            )

        return data
    except Exception as e:
        return {"name": filename, "type": "unknown", "error": str(e)}


def _parse_proxy_endpoint(xml_content: str, filename: str) -> Dict[str, Any]:
    """Parse ProxyEndpoint XML document."""
    try:
        root = ET.fromstring(xml_content)
        name = root.attrib.get("name") or filename.split("/")[-1].replace(".xml", "")

        base_path = None
        bp_elem = root.find(".//HTTPProxyConnection/BasePath")
        if bp_elem is not None and bp_elem.text:
            base_path = bp_elem.text.strip()

        route_rules: List[Dict[str, Optional[str]]] = []
        for rr in root.findall(".//RouteRule"):
            te = rr.find("TargetEndpoint")
            cond = rr.find("Condition")
            route_rules.append({
                "target_endpoint": te.text.strip() if te is not None and te.text else None,
                "condition": cond.text.strip() if cond is not None and cond.text else None,
            })

        return {
            "name": name,
            "filename": filename,
            "base_path": base_path,
            "route_rules": route_rules,
        }
    except Exception as e:
        return {"name": filename, "error": str(e)}
