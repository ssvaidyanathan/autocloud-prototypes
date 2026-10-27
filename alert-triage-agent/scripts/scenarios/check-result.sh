#!/usr/bin/env bash
# ==============================================================================
# Scenario Result Checker: check-result.sh
#
# Inspects GCS incident groups and OKF files generated during scenario execution.
# Extracts: Origin, Ticket Number, Alert Count, Alert Labels, and Blast Radius.
# Compares observed incident triage results against expected values from DEMO.md.
#
# Usage:
#   ./scripts/scenarios/check-result.sh [OPTIONS]
#
# Options:
#   --since <ISO-8601-time>   Filter incidents created or updated on or after <time>
#   --scenario <s1..s5>       Highlight expected results for a specific scenario
#   --bucket <bucket-name>    Override GCS incidents bucket (default: ${PROJECT_ID}-ata-incidents)
#   --dry-run                 Simulate output using mock incident data
#   --mock                    Alias for --dry-run
#   -h, --help                Display this help message
#
# Safety:
#   Strictly read-only. Uses active user gcloud credentials.
#   Never reads secrets; never mints tokens for service accounts.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

SINCE_TIME=""
FILTER_SCENARIO=""
BUCKET_NAME="${INCIDENTS_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-incidents}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --since)
            SINCE_TIME="$2"
            shift 2
            ;;
        --since=*)
            SINCE_TIME="${1#*=}"
            shift
            ;;
        --scenario)
            FILTER_SCENARIO="$(to_lower "$2")"
            shift 2
            ;;
        --scenario=*)
            FILTER_SCENARIO="${1#*=}"
            FILTER_SCENARIO="$(to_lower "${FILTER_SCENARIO}")"
            shift
            ;;
        --bucket)
            BUCKET_NAME="$2"
            shift 2
            ;;
        --bucket=*)
            BUCKET_NAME="${1#*=}"
            shift
            ;;
        --dry-run)
            export DRY_RUN="true"
            shift
            ;;
        --mock)
            export MOCK_MODE="true"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--since <ISO-8601-time>] [--scenario <s1..s5>] [--bucket <name>] [--dry-run|--mock]"
            echo ""
            echo "Options:"
            echo "  --since <time>       Filter incident files with timestamps >= <time> (e.g. 2026-10-26T22:00:00Z)"
            echo "  --scenario <id>      Compare against a specific scenario (s1, s2, s3, s4, s5)"
            echo "  --bucket <name>      Override incident GCS bucket (default: ${BUCKET_NAME})"
            echo "  --dry-run, --mock    Simulate output without network calls or cloud dependencies"
            echo "  -h, --help           Display this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "==================================================================="
echo "  Alert Triage Agent: Scenario Result Checker"
echo "  Project: ${PROJECT_ID} | Bucket: gs://${BUCKET_NAME}"
if [ -n "${SINCE_TIME}" ]; then
    echo "  Filter Since: ${SINCE_TIME}"
fi
if [ -n "${FILTER_SCENARIO}" ]; then
    echo "  Target Scenario: $(to_upper "${FILTER_SCENARIO}")"
fi
echo "==================================================================="

# Python helper to parse OKF markdown files and compare against DEMO.md expectations
python3 - "${BUCKET_NAME}" "${SINCE_TIME}" "${FILTER_SCENARIO}" "${DRY_RUN}" "${MOCK_MODE}" << 'PYEOF'
import sys
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

bucket_name = sys.argv[1]
since_str = sys.argv[2] if len(sys.argv) > 2 else ""
target_scenario = sys.argv[3].lower() if len(sys.argv) > 3 else ""
is_dry_run = sys.argv[4].lower() == "true" or (len(sys.argv) > 5 and sys.argv[5].lower() == "true")

DEMO_EXPECTATIONS = {
    "s1": {
        "title": "S1: Backend 5xx Error Spike",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": "ata-inventory-api",
        "expected_alerts": ["ata-inventory-5xx", "ata-orders-5xx"],
        "blast_radius": ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"],
        "root_cause": "HTTP 5xx errors on ata-inventory-api (injected 85% fault)",
    },
    "s2": {
        "title": "S2: Backend Latency Degradation",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": "ata-inventory-api",
        "expected_alerts": ["ata-inventory-latency", "ata-orders-latency"],
        "blast_radius": ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"],
        "root_cause": "Latency on ata-inventory-api; Apigee ata-inventory-v1 returns HTTP 504 GatewayTimeout",
    },
    "s3": {
        "title": "S3: Concurrent Disjoint Incidents",
        "expected_groups": 2,
        "expected_tickets": 2,
        "expected_origins": ["ata-inventory-api", "ata-orders-v1"],
        "expected_alerts": ["ata-inventory-5xx", "ata-apigee-orders-429"],
        "notes": "Correlation engine opens a second group; its 'Correlation:' observation states the best score and the 35 pts new-group threshold",
    },
    "s4": {
        "title": "S4: Database Connection Pool Exhaustion",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": "ata-inventory-db",
        "expected_alerts": ["ata-cloudsql-connections-critical"],
        "blast_radius": ["ata-inventory-db", "ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"],
        "root_cause": "Connection pool saturation on ata-inventory-db (alert stays open until reset-scenarios.sh)",
    },
    "s5": {
        "title": "S5: Bad Deploy / Config Failure",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin_pattern": r"ata-inventory-api(-[a-z0-9\-]+)?",
        # ata-inventory-latency opened in QA (2026-10-27) only from the new revision's cold start
        # (2 windows); the 180s retest window (latency_duration_secs) no longer fires on that.
        "expected_alerts": ["ata-inventory-5xx", "ata-orders-5xx"],
        "blast_radius": ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"],
        "root_cause": "Change-induced database authentication failure; rollback to the revision that served before (traffic history)",
    },
}

def parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        # Normalize trailing Z
        clean = ts_str.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None

since_dt = parse_iso(since_str) if since_str else None

def extract_frontmatter(content):
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    fm_text = parts[1]
    data = {}
    try:
        import yaml
        data = yaml.safe_load(fm_text) or {}
    except Exception:
        # Fallback simple line parsing
        for line in fm_text.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("-"):
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip().strip("'\"")
    return data

def get_incident_files_mock():
    return [
        {
            "uri": f"gs://{bucket_name}/knowledge/incident-manager/incident-ata-inventory-5xx-20261026t220500z.md",
            "group_id": "grp_20261026t220500z_ata-inventory-api",
            "timestamp": "2026-10-26T22:05:00Z",
            "origin": "ata-inventory-api" if target_scenario != "s4" else "ata-inventory-db",
            "servicenow_number": "INC0010482",
            "servicenow_url": "https://dev377204.service-now.com/nav_to.do?uri=incident.do?sys_id=mock-sys-id",
            "status": "ticketed",
            "alert_count": 2,
            "alerts": [
                {
                    "policy_name": "ata-inventory-5xx" if target_scenario != "s4" else "ata-cloudsql-connections-critical",
                    "resource_name": "ata-inventory-api" if target_scenario != "s4" else "ata-inventory-db",
                    "signal": "5xx" if target_scenario != "s4" else "connections",
                    "labels": {"app": "alert-triage-agent", "component": "ata-inventory-api", "severity": "p1", "signal": "5xx", "tier": "service"}
                },
                {
                    "policy_name": "ata-inventory-latency",
                    "resource_name": "ata-inventory-api",
                    "signal": "latency",
                    "labels": {"app": "alert-triage-agent", "component": "ata-inventory-api", "severity": "p2", "signal": "latency", "tier": "service"}
                }
            ],
            "blast_radius": ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]
        }
    ]

incidents = []

if is_dry_run:
    print("[MOCK/DRY-RUN] Simulating GCS incident retrieval...")
    incidents = get_incident_files_mock()
else:
    # 1. Fetch file lists from active and archive paths
    search_paths = [
        f"gs://{bucket_name}/knowledge/incident-manager/*.md",
        f"gs://{bucket_name}/archive/*.md",
    ]
    file_uris = []
    for sp in search_paths:
        # gcloud storage (gcloud CLI credentials). "matched no objects" is an empty prefix,
        # any other failure is fatal: never report 0 incidents because the listing failed.
        try:
            res = subprocess.run(["gcloud", "storage", "ls", sp], capture_output=True, text=True)
        except OSError as exc:
            print(f"[ERROR] could not run gcloud storage ls {sp}: {exc}", file=sys.stderr)
            sys.exit(1)
        if res.returncode != 0:
            if "matched no objects" in res.stderr:
                continue
            print(f"[ERROR] gcloud storage ls {sp} failed (exit {res.returncode}):\n{res.stderr.strip()}", file=sys.stderr)
            print("[ERROR] Check the active account with 'gcloud auth list' and its access to the bucket.", file=sys.stderr)
            sys.exit(1)
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.endswith(".md"):
                file_uris.append(line)

    for uri in file_uris:
        try:
            res = subprocess.run(["gcloud", "storage", "cat", uri], capture_output=True, text=True)
            if res.returncode != 0:
                # e.g. moved to archive/ by a reset between ls and cat
                print(f"[WARN] gcloud storage cat {uri} failed, skipping it: {res.stderr.strip()}", file=sys.stderr)
                continue
            content = res.stdout
            data = extract_frontmatter(content)
            if not data or data.get("type") != "incident":
                continue

            ts_raw = data.get("timestamp") or data.get("updated_at") or data.get("created_at")
            doc_dt = parse_iso(ts_raw)
            if since_dt and doc_dt and doc_dt < since_dt:
                continue

            alerts_raw = data.get("alerts") or []
            incidents.append({
                "uri": uri,
                "group_id": data.get("group_id", "N/A"),
                "timestamp": ts_raw or "N/A",
                "origin": data.get("origin", "Unknown"),
                "servicenow_number": data.get("servicenow_number", "N/A"),
                "servicenow_url": data.get("servicenow_url", "N/A"),
                "status": data.get("status", "N/A"),
                "alert_count": len(alerts_raw),
                "alerts": alerts_raw,
                "blast_radius": data.get("blast_radius", []),
                "labels": data.get("labels", {})
            })
        except Exception:
            continue

print(f"\nFound {len(incidents)} incident group(s) in GCS:\n")

if not incidents:
    print("  (No matching incident groups found in GCS for the specified timeframe)")
    print(f"  Check GCS bucket: gs://{bucket_name}/knowledge/incident-manager/")
    print(f"  Or archive path:  gs://{bucket_name}/archive/")

for idx, inc in enumerate(incidents, 1):
    print(f"--- Incident Group {idx} ---")
    print(f"  GCS File:       {inc['uri']}")
    print(f"  Group ID:       {inc['group_id']}")
    print(f"  Timestamp:      {inc['timestamp']}")
    print(f"  Status:         {inc['status']}")
    print(f"  Origin:         {inc['origin']}")
    print(f"  Ticket Number:  {inc['servicenow_number']}")
    if inc['servicenow_url'] != "N/A":
        print(f"  ServiceNow URL: {inc['servicenow_url']}")
    print(f"  Alert Count:    {inc['alert_count']}")

    print("  Alerts & Labels:")
    for a in inc['alerts']:
        pol = a.get("policy_name") or a.get("policy", "unknown-policy")
        res = a.get("resource_name") or a.get("resource", "unknown-resource")
        sig = a.get("signal", "unknown-signal")
        lbls = a.get("labels") or a.get("policy_user_labels") or {}
        lbl_str = ", ".join(f"{k}={v}" for k, v in sorted(lbls.items())) if isinstance(lbls, dict) else str(lbls)
        print(f"    * {pol} (resource: {res}, signal: {sig})")
        if lbl_str:
            print(f"      Labels: {lbl_str}")

    if inc['blast_radius']:
        print(f"  Blast Radius:   {', '.join(inc['blast_radius'])}")
    print()

# Comparison against DEMO.md
print("===================================================================")
print("  Expected Values Comparison (from DEMO.md)")
print("===================================================================")

scenarios_to_check = [target_scenario] if target_scenario in DEMO_EXPECTATIONS else list(DEMO_EXPECTATIONS.keys())

for sc_key in scenarios_to_check:
    sc = DEMO_EXPECTATIONS[sc_key]
    print(f"\n[{sc['title']}]")

    # If user filtered to a scenario, evaluate match
    if target_scenario == sc_key or len(incidents) > 0:
        if sc_key == "s3":
            exp_grp = 2
            obs_grp = len(incidents)
            match_str = "[MATCH]" if obs_grp == exp_grp else f"[MISMATCH: expected {exp_grp}, observed {obs_grp}]"
            print(f"  - Groups / Tickets: Expected {exp_grp} distinct groups/tickets -> {match_str}")
            print(f"  - Origins:          Expected {', '.join(sc['expected_origins'])}")
        else:
            exp_grp = sc['expected_groups']
            obs_grp = len(incidents)
            match_str = "[MATCH]" if obs_grp == exp_grp else f"[NOTE: expected {exp_grp}, found {obs_grp}]"
            print(f"  - Group Count:      Expected {exp_grp} -> {match_str}")

            if incidents:
                obs_origin = incidents[0]['origin']
                if "expected_origin" in sc:
                    origin_match = "[MATCH]" if obs_origin == sc['expected_origin'] else f"[MISMATCH: observed {obs_origin}]"
                    print(f"  - Origin:           Expected '{sc['expected_origin']}' -> {origin_match}")
                elif "expected_origin_pattern" in sc:
                    origin_match = "[MATCH]" if re.match(sc['expected_origin_pattern'], obs_origin) else f"[MISMATCH: observed {obs_origin}]"
                    print(f"  - Origin:           Expected pattern '{sc['expected_origin_pattern']}' -> {origin_match}")

                obs_ticket = incidents[0]['servicenow_number']
                ticket_match = "[MATCH]" if obs_ticket != "N/A" else "[NO TICKET FOUND]"
                print(f"  - Ticket:           Expected 1 ServiceNow ticket ({obs_ticket}) -> {ticket_match}")

        print(f"  - Expected Alerts:  {', '.join(sc['expected_alerts'])}")
        if "root_cause" in sc:
            print(f"  - Expected Cause:   {sc['root_cause']}")

print("\n===================================================================")
print("  Triage Verification Complete")
print("===================================================================\n")
PYEOF
