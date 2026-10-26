#!/usr/bin/env bash
# ==============================================================================
# Master Scenario Driver Script: run-all-scenarios.sh
#
# Executes scenarios, captures logs, asserts triage and ticketing outcomes.
# Order prioritized as directed: S4, S1, S3, S5, then S2.
#
# Usage:
#   ./scripts/run-all-scenarios.sh [OPTIONS]
#
# Options:
#   --priority          Run prioritized core scenarios (S4, S1, S3, S5) [Default]
#   --all               Run all 5 scenarios (S1 through S5)
#   --scenario <ID>     Run a specific scenario (e.g. s1, s2, s3, s4, s5)
#   --dry-run           Simulate scenario executions without sending network traffic
#   --mock              Run in mock mode without GCP cloud dependencies
#   --help              Display help message
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

MODE="priority"
TARGET_SCENARIO=""
LOG_FILE="/tmp/ata-scenarios-$(date +%s).log"

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            MODE="all"
            shift
            ;;
        --priority)
            MODE="priority"
            shift
            ;;
        --scenario)
            MODE="single"
            TARGET_SCENARIO="$2"
            shift 2
            ;;
        --hold)
            export ATA_HOLD_DURATION="$2"
            shift 2
            ;;
        --hold=*)
            export ATA_HOLD_DURATION="${1#*=}"
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
            echo "Usage: $0 [--priority|--all|--scenario <s1..s5>|--hold <duration>|--dry-run|--mock]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "==================================================================="
echo "  Alert Triage Agent: Master Scenario Driver"
echo "  Mode: ${MODE} | Project: ${PROJECT_ID} | Dry Run: ${DRY_RUN}"
echo "  Log File: ${LOG_FILE}"
echo "==================================================================="

# List of scenarios to run
declare -a SCENARIOS_TO_RUN=()

if [ "${MODE}" = "single" ]; then
    SCENARIOS_TO_RUN+=("${TARGET_SCENARIO}")
elif [ "${MODE}" = "priority" ]; then
    # Priority order: S4, S1, S3, S5
    SCENARIOS_TO_RUN=("s4" "s1" "s3" "s5")
else
    # Full suite order
    SCENARIOS_TO_RUN=("s1" "s2" "s3" "s4" "s5")
fi

declare -A SCENARIO_NAMES=(
    ["s1"]="Backend 5xx Error Spike (Cloud Run ata-inventory-api)"
    ["s2"]="Backend Latency Degradation (>5s timeout)"
    ["s3"]="Concurrent Disjoint Incidents (ata-orders-v1 & ata-inventory-api)"
    ["s4"]="Database Connection Pool Exhaustion (Cloud SQL max_conn)"
    ["s5"]="Bad Deploy / Config Failure (Cloud Run ata-inventory-api)"
)

# Results tracking
declare -A SCENARIO_STATUS=()
declare -A SCENARIO_DURATION=()

# Initial Environment Cleanup
log_info "Performing pre-run baseline reset..."
"${SCRIPT_DIR}/reset-scenarios.sh" >> "${LOG_FILE}" 2>&1 || true

# Preflight Health Verification
if [ "${SKIP_PREFLIGHT:-false}" != "true" ]; then
    log_info "Executing preflight health check before scenario execution..."
    "${SCRIPT_DIR}/scenarios/preflight.sh" 2>&1 | tee -a "${LOG_FILE}" || {
        log_error "FATAL: Preflight health checks failed! Halting scenario driver."
        exit 1
    }
fi

for sc in "${SCENARIOS_TO_RUN[@]}"; do
    sc_clean="${sc,,}"
    sc_name="${SCENARIO_NAMES[${sc_clean}]:-Unknown Scenario}"
    script_path="${SCRIPT_DIR}/scenarios/${sc_clean}_*.sh"
    resolved_script=$(ls ${script_path} 2>/dev/null | head -n 1 || echo "")

    if [ -z "${resolved_script}" ] || [ ! -f "${resolved_script}" ]; then
        log_error "Scenario script for ${sc} not found: ${script_path}"
        SCENARIO_STATUS["${sc}"]="NOT_FOUND"
        continue
    fi

    echo ""
    log_info "---------------------------------------------------------------"
    log_info "Executing Scenario: ${sc^^} - ${sc_name}"
    log_info "Script: ${resolved_script}"
    log_info "---------------------------------------------------------------"

    START_T=$(date +%s)
    set +e
    "${resolved_script}" 2>&1 | tee -a "${LOG_FILE}"
    EXIT_CODE=$?
    set -e
    END_T=$(date +%s)
    DUR=$((END_T - START_T))
    SCENARIO_DURATION["${sc}"]="${DUR}s"

    if [ ${EXIT_CODE} -eq 0 ]; then
        log_success "Scenario ${sc^^} completed successfully (${DUR}s)."
        SCENARIO_STATUS["${sc}"]="PASSED"
    else
        log_error "Scenario ${sc^^} failed with exit code ${EXIT_CODE} (${DUR}s)."
        SCENARIO_STATUS["${sc}"]="FAILED"
    fi

    # Reset environment between scenarios
    log_info "Resetting environment faults before next scenario..."
    "${SCRIPT_DIR}/reset-scenarios.sh" >> "${LOG_FILE}" 2>&1 || true
done

# Summary Table
echo ""
echo "==================================================================="
echo "                   SCENARIO EXECUTION SUMMARY                      "
echo "==================================================================="
printf "%-8s %-45s %-12s %-10s\n" "ID" "Scenario Description" "Status" "Duration"
echo "-------------------------------------------------------------------"

TOTAL_FAILED=0
for sc in "${SCENARIOS_TO_RUN[@]}"; do
    sc_clean="${sc,,}"
    desc="${SCENARIO_NAMES[${sc_clean}]:-Unknown}"
    status="${SCENARIO_STATUS[${sc}]:-UNKNOWN}"
    dur="${SCENARIO_DURATION[${sc}]:-0s}"
    printf "%-8s %-45s %-12s %-10s\n" "${sc^^}" "${desc:0:43}" "${status}" "${dur}"
    if [ "${status}" != "PASSED" ]; then
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi
done
echo "==================================================================="

if [ ${TOTAL_FAILED} -eq 0 ]; then
    log_success "All executed scenarios passed successfully!"
    exit 0
else
    log_error "${TOTAL_FAILED} scenario(s) failed. Consult ${LOG_FILE} for details."
    exit 1
fi
