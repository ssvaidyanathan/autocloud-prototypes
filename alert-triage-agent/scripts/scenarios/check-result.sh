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
#   --bucket <bucket-name>    Override GCS incidents bucket (default: ${PROJECT_ID}-${NAME_PREFIX}-incidents)
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
# Reads the incidents bucket only: it does not need the inventory pin.
INVENTORY_PIN_OPTIONAL=true
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
python3 - "${BUCKET_NAME}" "${SINCE_TIME}" "${FILTER_SCENARIO}" "${DRY_RUN}" "${MOCK_MODE}" "${NAME_PREFIX}" << 'PYEOF'
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
# Every expected name is derived from the stack's NAME_PREFIX (common.sh validated it).
P = sys.argv[6]
INV_API, ORD_API, INV_DB = f"{P}-inventory-api", f"{P}-orders-api", f"{P}-inventory-db"
INV_PROXY, ORD_PROXY = f"{P}-inventory-v1", f"{P}-orders-v1"

DEMO_EXPECTATIONS = {
    "s1": {
        "title": "S1: Backend 5xx Error Spike",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": INV_API,
        "expected_alerts": [f"{P}-inventory-5xx", f"{P}-orders-5xx"],
        "blast_radius": [INV_API, INV_PROXY, ORD_API, ORD_PROXY],
        "root_cause": f"HTTP 5xx errors on {INV_API} (injected 85% fault)",
    },
    "s2": {
        "title": "S2: Backend Latency Degradation",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": INV_API,
        "expected_alerts": [f"{P}-inventory-latency", f"{P}-orders-latency"],
        "blast_radius": [INV_API, INV_PROXY, ORD_API, ORD_PROXY],
        "root_cause": f"Latency on {INV_API}; Apigee {INV_PROXY} returns HTTP 504 GatewayTimeout",
    },
    "s3": {
        "title": "S3: Concurrent Disjoint Incidents",
        "expected_groups": 2,
        "expected_tickets": 2,
        "expected_origins": [INV_API, ORD_PROXY],
        "expected_alerts": [f"{P}-inventory-5xx", f"{P}-apigee-orders-429"],
        "notes": "Correlation engine opens a second group; its 'Correlation:' observation states the best score and the 35 pts new-group threshold",
    },
    "s4": {
        "title": "S4: Database Connection Pool Exhaustion",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin": INV_DB,
        "expected_alerts": [f"{P}-cloudsql-connections-critical"],
        "blast_radius": [INV_DB, INV_API, INV_PROXY, ORD_API, ORD_PROXY],
        "root_cause": f"Connection pool saturation on {INV_DB} (alert stays open until reset-scenarios.sh)",
    },
    "s5": {
        "title": "S5: Bad Deploy / Config Failure",
        "expected_groups": 1,
        "expected_tickets": 1,
        "expected_origin_pattern": re.escape(INV_API) + r"(-[a-z0-9\-]+)?",
        # <prefix>-inventory-latency opened in QA (2026-10-27) only from the new revision's cold start
        # (2 windows); the 240s retest window (latency_duration_secs) no longer fires on that.
        "expected_alerts": [f"{P}-inventory-5xx", f"{P}-orders-5xx"],
        "blast_radius": [INV_API, INV_PROXY, ORD_API, ORD_PROXY],
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

# Standard-library parser for the block-style YAML subset the agent writes (yaml.safe_dump):
# nested mappings, lists of scalars and of mappings, quoted and wrapped scalars. Used when
# PyYAML is not installed (the operator scripts need only the standard library), so the
# alerts, their labels and the blast radius are still read instead of silently dropped.
_FM_KEY = re.compile(r"""^[^\s:'"-][^:]*:(\s|$)""")


def _fm_scalar(v):
    v = v.strip()
    if v in ("null", "~"):
        return None
    if v in ("true", "false"):
        return v == "true"
    if v == "[]":
        return []
    if v == "{}":
        return {}
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1].replace("''", "'") if v[0] == "'" else v[1:-1]
    if re.fullmatch(r"[-+]?[0-9]+", v):
        return int(v)
    if re.fullmatch(r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?", v):
        return float(v)
    return v


def _fm_tokens(fm_text):
    # [indent, text, continuation_column, has_inline_value]: a deeper-indented line after an
    # inline value continues it (a wrapped scalar). For "key: value" the value belongs to the
    # key's column; for a scalar list item "- value" to the dash's column.
    tokens = []
    for raw in fm_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text = raw.strip()
        if tokens and tokens[-1][3] and indent > tokens[-1][2]:
            tokens[-1][1] += " " + text
            continue
        if text.startswith("- ") and not _FM_KEY.match(text[2:]):
            tokens.append([indent, text, indent, True])
            continue
        body, col = (text[2:], indent + 2) if text.startswith("- ") else (text, indent)
        value = body.partition(":")[2].strip()
        tokens.append([indent, text, col, value != "" and not re.fullmatch(r"&\S+", value)])
    return tokens


def _fm_parse(tokens, i, indent):
    if tokens[i][1].startswith("- ") or tokens[i][1] == "-":
        items = []
        while i < len(tokens) and tokens[i][0] == indent and (tokens[i][1].startswith("- ") or tokens[i][1] == "-"):
            body = tokens[i][1][2:]
            if not body:
                i += 1
                value, i = _fm_parse(tokens, i, tokens[i][0])
                items.append(value)
            elif _FM_KEY.match(body):
                tokens[i] = [indent + 2, body, indent + 2, tokens[i][3]]
                value, i = _fm_mapping(tokens, i, indent + 2)
                items.append(value)
            else:
                items.append(_fm_scalar(body))
                i += 1
        return items, i
    return _fm_mapping(tokens, i, indent)


_FM_ANCHORS = {}


def _fm_mapping(tokens, i, indent):
    data = {}
    while i < len(tokens) and tokens[i][0] == indent and not tokens[i][1].startswith("- "):
        key, _, value = tokens[i][1].partition(":")
        key, value = key.strip().strip("'\""), value.strip()
        anchor = None
        if value.startswith("&"):  # safe_dump anchors a repeated object: "key: &id001"
            anchor, _, value = value[1:].partition(" ")
            value = value.strip()
        i += 1
        if value.startswith("*"):  # ... and refers to it again: "key: *id001"
            if value[1:] not in _FM_ANCHORS:
                raise ValueError(f"unknown YAML alias {value!r}")
            data[key] = _FM_ANCHORS[value[1:]]
        elif value:
            data[key] = _fm_scalar(value)
        elif i < len(tokens) and (tokens[i][0] > indent or (tokens[i][0] == indent and tokens[i][1].startswith("- "))):
            data[key], i = _fm_parse(tokens, i, tokens[i][0])
        else:
            data[key] = None
        if anchor:
            _FM_ANCHORS[anchor] = data[key]
    return data, i


def parse_frontmatter_stdlib(fm_text):
    _FM_ANCHORS.clear()
    tokens = _fm_tokens(fm_text)
    if not tokens:
        return {}
    data, end = _fm_parse(tokens, 0, tokens[0][0])
    if end != len(tokens) or not isinstance(data, dict):
        raise ValueError(f"unsupported YAML near: {tokens[min(end, len(tokens) - 1)][1][:80]!r}")
    return data


_YAML_WARNED = []


def extract_frontmatter(content):
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    fm_text = parts[1]
    try:
        import yaml
    except ImportError:
        if not _YAML_WARNED:
            print("[INFO] PyYAML is not installed: reading the incident frontmatter with the built-in parser.",
                  file=sys.stderr)
            _YAML_WARNED.append(True)
        return parse_frontmatter_stdlib(fm_text)
    return yaml.safe_load(fm_text) or {}

def get_incident_files_mock():
    return [
        {
            "uri": f"gs://{bucket_name}/knowledge/incident-manager/incident-{P}-inventory-5xx-20261026t220500z.md",
            "group_id": f"grp_20261026t220500z_{INV_API}",
            "timestamp": "2026-10-26T22:05:00Z",
            "origin": INV_API if target_scenario != "s4" else INV_DB,
            "servicenow_number": "INC0010482",
            "servicenow_url": "https://mock-instance.service-now.invalid/nav_to.do?uri=incident.do?sys_id=mock-sys-id",
            "status": "ticketed",
            "alert_count": 2,
            "alerts": [
                {
                    "policy_name": f"{P}-inventory-5xx" if target_scenario != "s4" else f"{P}-cloudsql-connections-critical",
                    "resource_name": INV_API if target_scenario != "s4" else INV_DB,
                    "signal": "5xx" if target_scenario != "s4" else "connections",
                    "labels": {"app": "alert-triage-agent", "component": INV_API, "severity": "p1", "signal": "5xx", "tier": "service"}
                },
                {
                    "policy_name": f"{P}-inventory-latency",
                    "resource_name": INV_API,
                    "signal": "latency",
                    "labels": {"app": "alert-triage-agent", "component": INV_API, "severity": "p2", "signal": "latency", "tier": "service"}
                }
            ],
            "blast_radius": [INV_API, INV_PROXY, ORD_API, ORD_PROXY]
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
        except Exception as exc:
            print(f"[WARN] could not read {uri}, skipping it: {exc}", file=sys.stderr)
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
