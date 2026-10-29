#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [create_incident]: %(message)s",
)
logger = logging.getLogger("create_incident")

SCENARIO_DEFINITIONS: Dict[int, Dict[str, Any]] = {
    1: {
        "title": "Customers getting errors at checkout on shop-api",
        "description": (
            "Elevated HTTP 500 error rates detected on shop-api /checkout endpoint "
            "(~40% failures). Customers reporting failed orders with internal server errors."
        ),
        "severity": "P1",
        "service_hint": "shop-api",
    },
    2: {
        "title": "shop-api very slow, checkout timing out",
        "description": (
            "Significant latency spike observed on shop-api. P95 latency degraded to "
            "4.5s-8.0s on /catalog and /checkout endpoints exceeding 500ms SLO threshold."
        ),
        "severity": "P2",
        "service_hint": "shop-api",
    },
    3: {
        "title": "shop-api broken after this morning's release",
        "description": (
            "shop-api returning HTTP 503 Service Unavailable following latest deployment revision. "
            "Readiness probe failing due to missing required configuration PAYMENT_GATEWAY_KEY."
        ),
        "severity": "P1",
        "service_hint": "shop-api",
    },
    4: {
        "title": "shop-api intermittently unavailable (503s)",
        "description": (
            "Container instances of shop-api experiencing progressive memory saturation "
            "and crashing with Memory limit exceeded (OOM) leading to intermittent 503s."
        ),
        "severity": "P2",
        "service_hint": "shop-api",
    },
    5: {
        "title": "Product availability not loading on shop-api",
        "description": (
            "Downstream inventory verification failing on shop-api with HTTP 403 Forbidden errors "
            "when contacting inventory-svc. IAM permission roles/run.invoker appears revoked."
        ),
        "severity": "P2",
        "service_hint": "shop-api",
    },
}


def build_incident_payload(
    target: str,
    scenario_id: int,
    token: Optional[str] = None,
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """Construct webhook path, headers, and payload according to target platform schema."""
    target_norm = target.lower().strip()
    if target_norm in ("snow", "servicenow"):
        source = "servicenow"
    elif target_norm == "jira":
        source = "jira"
    elif target_norm == "generic":
        source = "generic"
    else:
        raise ValueError(f"Unsupported target: {target}. Must be snow, jira, or generic.")

    scenario = SCENARIO_DEFINITIONS.get(scenario_id)
    if not scenario:
        raise ValueError(f"Unknown scenario ID: {scenario_id}. Must be 1 to 5.")

    title = scenario["title"]
    description = scenario["description"]
    severity = scenario["severity"]
    service_hint = scenario["service_hint"]
    now_iso = datetime.now(timezone.utc).isoformat()
    unique_id = uuid4().hex[:8]

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    payload: Dict[str, Any] = {}

    if source == "servicenow":
        sys_id = uuid4().hex
        number = f"INC{int(unique_id, 16) % 900000 + 100000}"
        priority_num = "1" if severity == "P1" else "2"
        payload = {
            "sys_id": sys_id,
            "number": number,
            "short_description": title,
            "description": description,
            "priority": priority_num,
            "urgency": "1" if severity == "P1" else "2",
            "impact": "1",
            "category": "Cloud Services",
            "cmdb_ci": service_hint,
            "caller": "SRE Monitoring System",
            "opened_at": now_iso,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

    elif source == "jira":
        issue_id = str(int(unique_id, 16) % 90000 + 10000)
        issue_key = f"OPS-{issue_id}"
        priority_name = "Highest" if severity == "P1" else "High"
        payload = {
            "issue": {
                "id": issue_id,
                "key": issue_key,
                "fields": {
                    "summary": title,
                    "description": description,
                    "priority": {"name": priority_name},
                    "components": [{"name": service_hint}],
                    "reporter": {"displayName": "SRE Monitoring System"},
                    "created": now_iso,
                },
            }
        }
        if token:
            headers["X-Webhook-Token"] = token
            headers["Authorization"] = f"Bearer {token}"

    elif source == "generic":
        ext_id = f"gen-{unique_id}"
        payload = {
            "incident": {
                "id": ext_id,
                "source": "generic",
                "external_id": ext_id,
                "external_key": f"GEN-{unique_id.upper()}",
                "title": title,
                "description": description,
                "severity": severity,
                "service_hint": service_hint,
                "labels": [f"scenario:{scenario_id}", f"service:{service_hint}"],
                "opened_at": now_iso,
                "reporter": "SRE Monitoring System",
            }
        }
        if token:
            body_bytes = json.dumps(payload).encode("utf-8")
            signature = hmac.new(token.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={signature}"

    return source, headers, payload


def send_incident(
    bridge_url: str,
    target: str,
    scenario_id: int,
    token: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> httpx.Response:
    """Construct and dispatch webhook request to incident-bridge."""
    source, headers, payload = build_incident_payload(target, scenario_id, token)
    url = f"{bridge_url.rstrip('/')}/webhook/{source}"
    body_json = json.dumps(payload)

    # If generic HMAC was computed on original serialization, update signature for exact body
    if source == "generic" and token:
        body_bytes = body_json.encode("utf-8")
        signature = hmac.new(token.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={signature}"

    logger.info("Posting incident webhook for scenario %d to %s", scenario_id, url)
    if client:
        return client.post(url, content=body_json, headers=headers)

    with httpx.Client(timeout=15.0) as default_client:
        return default_client.post(url, content=body_json, headers=headers)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and submit incident webhooks for demo scenarios"
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=["snow", "servicenow", "jira", "generic"],
        help="Target ticketing system (snow, jira, generic)",
    )
    parser.add_argument(
        "--scenario",
        type=int,
        required=True,
        choices=[1, 2, 3, 4, 5],
        help="Failure scenario ID (1 to 5)",
    )
    parser.add_argument(
        "--bridge-url",
        default=os.getenv("BRIDGE_URL", "http://localhost:8080"),
        help="Incident Bridge service URL (default: http://localhost:8080 or BRIDGE_URL)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Webhook authentication token or HMAC secret (or env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload without submitting webhook",
    )
    args = parser.parse_args()

    token = args.token
    target_norm = args.target.lower().strip()
    if not token:
        if target_norm in ("snow", "servicenow"):
            token = os.getenv("SERVICENOW_WEBHOOK_TOKEN", "test-snow-token")
        elif target_norm == "jira":
            token = os.getenv("JIRA_WEBHOOK_TOKEN", "test-jira-token")
        elif target_norm == "generic":
            token = os.getenv("GENERIC_HMAC_KEY", "test-generic-key")

    if args.dry_run:
        source, headers, payload = build_incident_payload(args.target, args.scenario, token)
        print(f"Target Source: {source}")
        print(f"Endpoint: {args.bridge_url.rstrip('/')}/webhook/{source}")
        print("Headers:")
        print(json.dumps(headers, indent=2))
        print("Payload:")
        print(json.dumps(payload, indent=2))
        return

    try:
        response = send_incident(
            bridge_url=args.bridge_url,
            target=args.target,
            scenario_id=args.scenario,
            token=token,
        )
        logger.info("Bridge response status: HTTP %d", response.status_code)
        try:
            resp_body = response.json()
            print(json.dumps(resp_body, indent=2))
        except Exception:
            print(response.text)

        if response.status_code not in (200, 202):
            sys.exit(1)
    except Exception as exc:
        logger.error("Failed to submit incident webhook: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
