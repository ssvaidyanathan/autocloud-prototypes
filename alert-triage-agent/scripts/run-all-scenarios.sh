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
LOG_FILE="/tmp/${NAME_PREFIX}-scenarios-$(date +%s).log"

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

# Bash 3.2 (macOS /bin/bash) has no associative arrays or ${x,,}/${x^^}: names come
# from a case statement, case conversion from to_lower/to_upper (common.sh), and
# results are indexed arrays parallel to SCENARIOS_TO_RUN.

# Print the description of scenario $1 (lowercase id), or $2 if unknown.
scenario_name() {
    case "$1" in
        s1) echo "Backend 5xx Error Spike (Cloud Run ${SVC_INVENTORY})" ;;
        s2) echo "Backend Latency Degradation (>5s timeout)" ;;
        s3) echo "Concurrent Disjoint Incidents (${PROXY_ORDERS} & ${SVC_INVENTORY})" ;;
        s4) echo "Database Connection Pool Exhaustion (Cloud SQL max_conn)" ;;
        s5) echo "Bad Deploy / Config Failure (Cloud Run ${SVC_INVENTORY})" ;;
        *) echo "$2" ;;
    esac
}

# Results tracking, indexed like SCENARIOS_TO_RUN
SCENARIO_STATUS=()
SCENARIO_DURATION=()

# Ctrl-C stops the whole run. A terminal Ctrl-C sends SIGINT to the foreground process
# group: this driver, the running scenario and tee. The scenario's own INT trap stops its
# traffic and it finishes normally (next steps; the fault stays injected for inspection).
# Bash runs this trap once that pipeline has finished. The driver then runs no further
# scenario and no reset, marks the rest SKIPPED, prints the summary with the reset command
# and exits 130. tee_log ignores SIGINT so the scenario's output after the Ctrl-C is kept.
INTERRUPTED=false
trap 'INTERRUPTED=true' INT TERM

tee_log() {
    ( trap '' INT; exec tee -a "${LOG_FILE}" )
}

# Initial Environment Cleanup
log_info "Performing pre-run baseline reset..."
"${SCRIPT_DIR}/reset-scenarios.sh" >> "${LOG_FILE}" 2>&1 || true

# Preflight Health Verification (skipped after a Ctrl-C in the baseline reset: the run stops)
if [ "${SKIP_PREFLIGHT:-false}" != "true" ] && [ "${INTERRUPTED}" != "true" ]; then
    log_info "Executing preflight health check before scenario execution..."
    "${SCRIPT_DIR}/scenarios/preflight.sh" 2>&1 | tee_log || {
        if [ "${INTERRUPTED}" != "true" ]; then
            log_error "FATAL: Preflight health checks failed! Halting scenario driver."
            exit 1
        fi
    }
fi

idx=-1
for sc in ${SCENARIOS_TO_RUN[@]+"${SCENARIOS_TO_RUN[@]}"}; do
    idx=$((idx + 1))
    if [ "${INTERRUPTED}" = "true" ]; then
        SCENARIO_STATUS[idx]="SKIPPED"
        continue
    fi
    sc_clean="$(to_lower "${sc}")"
    sc_upper="$(to_upper "${sc}")"
    sc_name="$(scenario_name "${sc_clean}" "Unknown Scenario")"
    script_path="${SCRIPT_DIR}/scenarios/${sc_clean}_*.sh"
    resolved_script=$(ls ${script_path} 2>/dev/null | head -n 1 || echo "")

    if [ -z "${resolved_script}" ] || [ ! -f "${resolved_script}" ]; then
        log_error "Scenario script for ${sc} not found: ${script_path}"
        SCENARIO_STATUS[idx]="NOT_FOUND"
        continue
    fi

    echo ""
    log_info "---------------------------------------------------------------"
    log_info "Executing Scenario: ${sc_upper} - ${sc_name}"
    log_info "Script: ${resolved_script}"
    log_info "---------------------------------------------------------------"

    START_T=$(date +%s)
    set +e
    "${resolved_script}" 2>&1 | tee_log
    EXIT_CODE=$?
    set -e
    END_T=$(date +%s)
    DUR=$((END_T - START_T))
    SCENARIO_DURATION[idx]="${DUR}s"

    if [ "${INTERRUPTED}" = "true" ]; then
        log_warn "Scenario ${sc_upper} interrupted by Ctrl-C (exit code ${EXIT_CODE}, ${DUR}s). No further scenario and no reset will run."
        SCENARIO_STATUS[idx]="INTERRUPTED"
        continue
    fi

    if [ ${EXIT_CODE} -eq 0 ]; then
        log_success "Scenario ${sc_upper} completed successfully (${DUR}s)."
        SCENARIO_STATUS[idx]="PASSED"
    else
        log_error "Scenario ${sc_upper} failed with exit code ${EXIT_CODE} (${DUR}s)."
        SCENARIO_STATUS[idx]="FAILED"
    fi

    # Reset environment between scenarios
    log_info "Resetting environment faults before next scenario..."
    if ! "${SCRIPT_DIR}/reset-scenarios.sh" >> "${LOG_FILE}" 2>&1; then
        # Do not run further scenarios (or leave S5's bad revision serving) silently.
        log_error "FATAL: reset after ${sc_upper} failed; environment is NOT on the baseline. See ${LOG_FILE}. Halting."
        SCENARIO_STATUS[idx]="RESET_FAILED"
        break
    fi
done

# Summary Table
echo ""
echo "==================================================================="
echo "                   SCENARIO EXECUTION SUMMARY                      "
echo "==================================================================="
printf "%-8s %-45s %-12s %-10s\n" "ID" "Scenario Description" "Status" "Duration"
echo "-------------------------------------------------------------------"

TOTAL_FAILED=0
idx=-1
for sc in ${SCENARIOS_TO_RUN[@]+"${SCENARIOS_TO_RUN[@]}"}; do
    idx=$((idx + 1))
    sc_clean="$(to_lower "${sc}")"
    desc="$(scenario_name "${sc_clean}" "Unknown")"
    status="${SCENARIO_STATUS[idx]:-UNKNOWN}"
    if [ "${status}" = "UNKNOWN" ] && [ "${INTERRUPTED}" = "true" ]; then
        status="SKIPPED"  # not reached: the run stopped (Ctrl-C) before it
    fi
    dur="${SCENARIO_DURATION[idx]:-0s}"
    printf "%-8s %-45s %-12s %-10s\n" "$(to_upper "${sc}")" "${desc:0:43}" "${status}" "${dur}"
    if [ "${status}" != "PASSED" ]; then
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
    fi
done
echo "==================================================================="

if [ "${INTERRUPTED}" = "true" ]; then
    log_warn "Run interrupted by Ctrl-C. The environment was NOT reset: the interrupted scenario's fault may still be injected."
    log_warn "Reset it before running another scenario: ./scripts/reset-scenarios.sh"
    log_warn "Log: ${LOG_FILE}"
    exit 130
fi

if [ ${TOTAL_FAILED} -eq 0 ]; then
    log_success "All executed scenarios passed successfully!"
    exit 0
else
    log_error "${TOTAL_FAILED} scenario(s) failed. Consult ${LOG_FILE} for details."
    exit 1
fi
