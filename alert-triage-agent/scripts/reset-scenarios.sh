#!/usr/bin/env bash
# ==============================================================================
# Reset Scenarios: Clear All Injected Faults and Restore Healthy Baseline
#
# IMPORTANT OPERATIONAL CONTRACT:
# This script must ONLY be executed strictly BETWEEN scenarios, NEVER mid-scenario
# or while an alert sequence or windowing settle timer is active. Mid-scenario
# invocation clears bridge in-memory state and knowledge base files, causing group
# fragmentation (defect 5). Bridge state is cleared via authenticated POST /windowing/reset.
#
# Actions:
# 1. Reset fault injection states on demo apps (POST /admin/fault/reset)
# 2. Bridge state (stateless note)
# 3. Terminate background DB sleep connections and release locks
# 4. Restore healthy Apigee proxy revisions (re-deploy base bundles)
# 5. Restore Cloud Run service configuration if S5 was deployed
# 6. Wait for open ATA alerts to close, then, concurrently, for the quiet period (Apigee
#    metric lag) and for every incident group to reach a terminal state (recovery note posted)
# 7. Archive active OKF incident files and clean leftover root storage
# 8. Verify healthy 200 responses across all services and 0 active incident groups
# 9. Admin fault injection preflight verification
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

parse_duration() {
    local val="$1"
    if [[ "${val}" =~ ^([0-9]+)[mM]$ ]]; then
        echo $((${BASH_REMATCH[1]} * 60))
    elif [[ "${val}" =~ ^([0-9]+)[sS]?$ ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "${val}"
    fi
}

# Configuration options & defaults
NO_WAIT="${ATA_RESET_NO_WAIT:-${NO_WAIT:-false}}"
# Incident groups (file names or group ids) left out of the terminal-state wait only.
IGNORE_GROUPS="${ATA_RESET_IGNORE_GROUPS:-}"
# 180s covers Apigee's 1-3 min metric lag. The agent's recovery note is not covered by
# time: step 6 also waits (concurrently) for every incident group to reach a terminal state.
QUIET_PERIOD_SECS="$(parse_duration "${ATA_RESET_QUIET_PERIOD:-${QUIET_PERIOD:-180}}")"
WAIT_TIMEOUT_SECS="$(parse_duration "${ATA_RESET_TIMEOUT:-${WAIT_TIMEOUT:-900}}")"
POLL_INTERVAL_SECS="$(parse_duration "${ATA_RESET_POLL_INTERVAL:-${POLL_INTERVAL:-15}}")"
MAX_ALERT_AGE_SECS="$(parse_duration "${ATA_RESET_MAX_ALERT_AGE:-${MAX_ALERT_AGE:-3600}}")"
# Consecutive failed Cloud Monitoring queries tolerated before the reset fails.
MAX_QUERY_ATTEMPTS="${ATA_RESET_MAX_QUERY_ATTEMPTS:-3}"
RESET_START_TIME="$(date +%s)"

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-wait)
            NO_WAIT="true"
            shift
            ;;
        --ignore-group)
            IGNORE_GROUPS="${IGNORE_GROUPS:+${IGNORE_GROUPS} }$2"
            shift 2
            ;;
        --ignore-group=*)
            IGNORE_GROUPS="${IGNORE_GROUPS:+${IGNORE_GROUPS} }${1#*=}"
            shift
            ;;
        --quiet-period)
            QUIET_PERIOD_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --quiet-period=*)
            QUIET_PERIOD_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --timeout)
            WAIT_TIMEOUT_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --timeout=*)
            WAIT_TIMEOUT_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --poll-interval)
            POLL_INTERVAL_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --poll-interval=*)
            POLL_INTERVAL_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --max-alert-age)
            MAX_ALERT_AGE_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --max-alert-age=*)
            MAX_ALERT_AGE_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --serving-revisions)
            ATA_SERVING_REVISIONS="$2"
            shift 2
            ;;
        --serving-revisions=*)
            ATA_SERVING_REVISIONS="${1#*=}"
            shift
            ;;
        --monitoring-token-source)
            ATA_MONITORING_TOKEN_SOURCE="$2"
            shift 2
            ;;
        --monitoring-token-source=*)
            ATA_MONITORING_TOKEN_SOURCE="${1#*=}"
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
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-wait                  Skip waiting for open alerts, terminal incident groups and quiet period"
            echo "                             (risky: can archive groups the agent is still working on)"
            echo "  --ignore-group <file|id>   Leave one incident group out of the terminal-state wait only (repeatable;"
            echo "                             env ATA_RESET_IGNORE_GROUPS). For a group that will never become terminal"
            echo "                             (e.g. stuck 'investigating'). The alert wait, the quiet period and the wait"
            echo "                             for every other group still run. Risky: the group is archived as it is"
            echo "  --quiet-period <duration>  Quiet period after alerts close, run alongside the wait for terminal"
            echo "                             incident groups (default: 180s / 3m)"
            echo "  --timeout <duration>       Max timeout waiting for alerts to close and groups to reach"
            echo "                             a terminal state (default: 900s / 15m)"
            echo "  --poll-interval <duration> Polling interval for alert and incident group queries (default: 15s)"
            echo "  --max-alert-age <duration> Max age of open alerts before reset start (default: 60m / 3600s)"
            echo "  --serving-revisions <list> Comma or space separated list of active Cloud Run revisions"
            echo "  --monitoring-token-source <adc|gcloud>"
            echo "                             Token for the alert check: adc = 'gcloud auth application-default print-access-token',"
            echo "                             gcloud = 'gcloud auth print-access-token'"
            echo "                             (default: adc; env ATA_MONITORING_TOKEN_SOURCE). No fallback between them."
            echo "  --dry-run                  Simulate reset without network modifications"
            echo "  --mock                     Run in mock mode without GCP cloud dependencies"
            echo "  -h, --help                 Display this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Fail before any reset step if the token source is misconfigured.
monitoring_token_command "${ATA_MONITORING_TOKEN_SOURCE}" >/dev/null || exit 1
# open_alerts.py names the source in its 401 messages.
export ATA_MONITORING_TOKEN_SOURCE

# Revisions that receive traffic now (percent > 0 in status.traffic), each listed once.
# latestReadyRevisionName is not used: it can be a retired 0%-traffic revision (after an
# S5 revert it is the bad S5 revision), and counting it would let a stale alert on it
# block the reset.
get_serving_revisions() {
    local revs=""
    add_rev() {
        case " ${revs} " in
            *" $1 "*) ;;
            *) revs="${revs:+${revs} }$1" ;;
        esac
    }
    if [ -n "${PINNED_INVENTORY_REVISION:-}" ]; then
        add_rev "${PINNED_INVENTORY_REVISION}"
    fi
    if [ -n "${ATA_SERVING_REVISIONS:-}" ]; then
        for r in ${ATA_SERVING_REVISIONS//,/ }; do
            add_rev "${r}"
        done
    fi

    if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
        for svc in "${NAME_PREFIX}-inventory-api" "${NAME_PREFIX}-orders-api"; do
            local svc_json svc_revs
            if ! svc_json="$(gcloud run services describe "${svc}" \
                --project="${PROJECT_ID}" \
                --region="${REGION}" \
                --format=json 2>/dev/null)"; then
                log_warn "    Could not describe ${svc}; its serving revisions are not scoped." >&2
                continue
            fi
            svc_revs="$(printf '%s' "${svc_json}" | python3 -c '
import json, sys
for t in (json.load(sys.stdin).get("status") or {}).get("traffic") or []:
    if (t.get("percent") or 0) > 0 and t.get("revisionName"):
        print(t["revisionName"])
' 2>/dev/null)" || {
                log_warn "    Could not parse the traffic of ${svc}; its serving revisions are not scoped." >&2
                continue
            }
            for r in ${svc_revs}; do
                add_rev "${r}"
            done
        done
    fi
    echo "${revs}"
}

# Cloud Monitoring query helper (CA bundle resolution, scoping, fail-loud errors).
OPEN_ALERTS_PY="${SCRIPT_DIR}/scenarios/open_alerts.py"

# Print {"active": [...], "ignored": [...]} for OPEN ata-* alerts. Exits non-zero, with
# the cause on stderr, when the Alerts API cannot be queried.
query_open_ata_alerts() {
    local proj="$1"
    local tok="$2"
    local reset_start="${3:-$(date +%s)}"
    local max_age_secs="${4:-3600}"
    local serving_revs="${5:-}"

    python3 "${OPEN_ALERTS_PY}" "${proj}" "${tok}" "${reset_start}" "${max_age_secs}" "${serving_revs}"
}

# Incident group statuses after which the agent does nothing more with a group (no timer,
# no work note): resolved, recovered, archived, and degraded (excluded from grouping and
# timers; needs a human). Anything else, including a missing or unknown status, is active.
# 'recovered' alone is not enough: the agent sets it when the cool-down ends, BEFORE it
# posts the recovery note, and records recovery_note_posted_at only after the note posted.
# A 'recovered' group without recovery_note_posted_at is still active (see group_state).
TERMINAL_GROUP_STATUSES="resolved recovered archived degraded"

# Print the value of top-level YAML frontmatter key $1 in OKF markdown on stdin ("" if
# none). Quotes and surrounding blanks are stripped; nested keys are ignored.
okf_field() {
    awk -v key="$1" '{ sub(/\r$/, "") }
         NR == 1 { if ($0 != "---") exit; next }
         $0 == "---" { exit }
         index($0, key ":") == 1 { s = substr($0, length(key) + 2); sub(/^[ \t]*/, "", s); gsub(/["\047]/, "", s); sub(/[ \t]+$/, "", s); print s; exit }'
}

# Print the status in the YAML frontmatter of OKF markdown on stdin ("" if none).
okf_status() {
    okf_field status
}

is_terminal_group_status() {
    case " ${TERMINAL_GROUP_STATUSES} " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

# Print "terminal" or the reason a group is still active, for OKF markdown on stdin.
group_state() {
    local content status note
    content="$(cat)"
    status="$(printf '%s\n' "${content}" | okf_field status)"
    if ! is_terminal_group_status "${status}"; then
        echo "${status:-no status}"
        return 0
    fi
    if [ "${status}" = "recovered" ]; then
        note="$(printf '%s\n' "${content}" | okf_field recovery_note_posted_at)"
        case "${note}" in
            ""|null|Null|NULL|"~")
                echo "recovered, recovery note not posted yet"
                return 0
                ;;
        esac
    fi
    echo "terminal"
}

# --ignore-group: true if incident file name $1 (incident-<id>.md) is listed in IGNORE_GROUPS
# (file names or group ids, comma or space separated).
is_ignored_group() {
    local entry
    for entry in ${IGNORE_GROUPS//,/ }; do
        if [ "$1" = "${entry}" ] || [ "$1" = "incident-${entry}.md" ]; then
            return 0
        fi
    done
    return 1
}

# Print "<file> (<status or reason>)" for every incident file in knowledge/incident-manager/
# whose group is not in a terminal state (group_state); a group named with --ignore-group is
# printed as "IGNORED <file> (<status or reason>)". Reads with gcloud storage (same login as
# step 7). Returns non-zero after gcloud's error is printed when the files cannot be read.
non_terminal_incident_groups() {
    local src="gs://${INCIDENTS_BUCKET}/${ACTIVE_INCIDENTS_URL_SUFFIX}"
    local urls url content state
    urls="$(gcs_run ls "${src}")" || return 1
    for url in ${urls}; do
        content="$(gcs_run cat "${url}")" || return 1
        # Moved or deleted since the listing: no longer an active file.
        [ -n "${content}" ] || continue
        state="$(printf '%s\n' "${content}" | group_state)"
        if [ "${state}" = "terminal" ]; then
            continue
        fi
        if is_ignored_group "${url##*/}"; then
            echo "IGNORED ${url##*/} (${state})"
        else
            echo "${url##*/} (${state})"
        fi
    done
}

# Comma-separated summary of the first 5 lines of $1.
summarize_lines() {
    printf '%s\n' "$1" | awk 'NR <= 5 { out = out (NR > 1 ? ", " : "") $0 } END { if (NR > 5) out = out ", ...+" (NR - 5) " more"; print out }'
}

wait_for_open_alerts() {
    log_info "--> 6. Checking Cloud Monitoring Alerts API for open ATA alerts and incident group states..."
    if [ "${NO_WAIT}" = "true" ]; then
        log_info "    [--no-wait] Skipping wait for open alerts, terminal incident groups and quiet period."
        log_warn "    [--no-wait] Incident files are archived even if the agent is still working on a group (e.g. a pending recovery note)."
        return 0
    fi

    if [ -n "${IGNORE_GROUPS}" ]; then
        log_warn "    [--ignore-group] RISKY: left out of the terminal-state wait and archived in whatever state they are in: ${IGNORE_GROUPS}"
    fi

    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_info "    [MOCK/DRY-RUN] Simulating wait for open alerts to close (0 open alerts)."
        log_info "    [MOCK/DRY-RUN] Simulating wait for incident groups to reach a terminal state."
        log_info "    [MOCK/DRY-RUN] Simulating quiet period (${QUIET_PERIOD_SECS}s)."
        return 0
    fi

    local start_time
    start_time=$(date +%s)
    local deadline=$((start_time + WAIT_TIMEOUT_SECS))
    local quiet_start=""
    local ignored_groups=""
    local first_poll="true"
    local query_failures=0
    local query_err_file
    query_err_file="$(mktemp)"
    # shellcheck disable=SC2064  # expand the path now; the local is gone at EXIT.
    trap "rm -f '${query_err_file}'" EXIT

    local serving_revs
    serving_revs="$(get_serving_revisions)"

    log_info "    Polling Cloud Monitoring API (read-only) for OPEN alerts on ata-* policies (label app=alert-triage-agent)..."
    log_info "    Max timeout: ${WAIT_TIMEOUT_SECS}s | Quiet period: ${QUIET_PERIOD_SECS}s | Polling interval: ${POLL_INTERVAL_SECS}s | Max alert age: ${MAX_ALERT_AGE_SECS}s | Token source: ${ATA_MONITORING_TOKEN_SOURCE}"
    if [ -n "${serving_revs}" ]; then
        log_info "    Serving revisions scoped: ${serving_revs}"
    fi

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$((now - start_time))

        # A failed query is never treated as zero open alerts. open_alerts.py exits 3 for
        # transient failures (timeout, 429, 5xx), which are retried; anything else
        # (401/403, TLS/CA bundle, no token, crash) cannot self-heal and fails at once.
        local token="" open_alerts_json="" count query_err="" query_hint="" query_rc=0 retryable="false"
        if ! token="$(resolve_monitoring_token 2>"${query_err_file}")" || [ -z "${token}" ]; then
            query_err="could not get an access token from token source '${ATA_MONITORING_TOKEN_SOURCE}'"
            query_err+=" ('$(monitoring_token_command "${ATA_MONITORING_TOKEN_SOURCE}" 2>/dev/null)' failed: $(tail -n 1 "${query_err_file}"))."
            query_err+=" Run: $(monitoring_token_login_hint "${ATA_MONITORING_TOKEN_SOURCE}")"
        else
            open_alerts_json="$(query_open_ata_alerts "${PROJECT_ID}" "${token}" "${RESET_START_TIME}" "${MAX_ALERT_AGE_SECS}" "${serving_revs}" 2>"${query_err_file}")" || query_rc=$?
            if [ "${query_rc}" -ne 0 ]; then
                query_err="$(grep -m 1 'query failed' "${query_err_file}" || tail -n 1 "${query_err_file}")"
                query_err="${query_err:-open_alerts.py exited ${query_rc} with no output}"
                query_hint="$(grep -m 1 '^Hint:' "${query_err_file}" || true)"
                if [ "${query_rc}" -eq 3 ]; then
                    retryable="true"
                fi
            elif ! count=$(echo "${open_alerts_json}" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data['active']))"); then
                query_err="unparseable alert query output: ${open_alerts_json:0:200}"
            fi
        fi
        if [ -n "${query_err}" ]; then
            query_failures=$((query_failures + 1))
            if [ "${retryable}" != "true" ] || [ "${query_failures}" -ge "${MAX_QUERY_ATTEMPTS}" ]; then
                if [ "${retryable}" = "true" ]; then
                    log_error "FATAL: Cloud Monitoring alert query failed ${query_failures} time(s) in a row; cannot confirm ATA alerts have closed." >&2
                else
                    log_error "FATAL: Cloud Monitoring alert query failed (not retryable); cannot confirm ATA alerts have closed." >&2
                fi
                log_error "Cause: ${query_err}" >&2
                if [ -n "${query_hint}" ]; then
                    log_error "${query_hint}" >&2
                fi
                log_error "To skip the alert check deliberately, re-run with --no-wait." >&2
                exit 1
            fi
            log_warn "    Cloud Monitoring alert query failed (attempt ${query_failures}/${MAX_QUERY_ATTEMPTS}): ${query_err}. Retrying in ${POLL_INTERVAL_SECS}s..."
            sleep "${POLL_INTERVAL_SECS}"
            continue
        fi
        query_failures=0

        if [ "${first_poll}" = "true" ]; then
            first_poll="false"
            local ignored_count
            ignored_count=$(echo "${open_alerts_json}" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('ignored', [])))" 2>/dev/null || echo "0")
            if [ "${ignored_count}" -gt 0 ]; then
                log_info "    Ignored ${ignored_count} stale/non-serving open alert(s):"
                python3 - "${open_alerts_json}" << 'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
for a in data.get('ignored', []):
    reasons = "; ".join(a.get('ignore_reasons', []))
    rev_info = f" (rev: {a.get('revision')})" if a.get('revision') else ""
    print(f"      - [{a.get('id')}] Policy: {a.get('policy_display')}{rev_info} | Reason: {reasons} | Open: {a.get('open_time')}")
PYEOF
            fi
        fi

        if [ "${now}" -ge "${deadline}" ] && [ "${count}" -gt 0 ]; then
            log_error "Timed out waiting for open ATA alerts to close after ${WAIT_TIMEOUT_SECS}s (${elapsed}s elapsed)!"
            log_error "Still-open alerts (${count} total):"
            python3 - "${open_alerts_json}" << 'PYEOF' >&2
import sys, json
data = json.loads(sys.argv[1])
for a in data.get('active', []):
    print(f"  - [{a.get('id')}] Policy: {a.get('policy_display')} | Open time: {a.get('open_time')} | Resource: {a.get('name')}")
PYEOF
            exit 1
        fi

        if [ "${count}" -gt 0 ]; then
            quiet_start=""
            local alert_summary
            alert_summary=$(python3 - "${open_alerts_json}" << 'PYEOF' 2>/dev/null || echo ""
import sys, json
data = json.loads(sys.argv[1])
alerts = data.get('active', [])
items = [f"{a.get('policy_display')} ({a.get('id')})" for a in alerts[:5]]
if len(alerts) > 5:
    items.append(f"...+{len(alerts)-5} more")
print(", ".join(items))
PYEOF
)

            log_info "    [Progress] Elapsed: ${elapsed}s / ${WAIT_TIMEOUT_SECS}s | Still open: ${count} alert(s) [${alert_summary}]"
            sleep "${POLL_INTERVAL_SECS}"
        else
            # The quiet period (Apigee metric lag) and the terminal-state wait run together
            # from the first poll with zero open alerts. The quiet clock restarts only when an
            # alert reopens (above). Archiving a group the agent is still working on races its
            # recovery note (posted when the 180s cool-down after the last close ends), so the
            # reset proceeds only once the quiet period is over AND every group is terminal.
            if [ -z "${quiet_start}" ]; then
                quiet_start="${now}"
                if [ "${QUIET_PERIOD_SECS}" -gt 0 ]; then
                    log_success "    All open ATA alerts have closed! Starting the ${QUIET_PERIOD_SECS}s quiet period (Apigee metric ingestion lag) while waiting for incident groups to reach a terminal state..."
                fi
            fi
            local quiet_elapsed=$((now - quiet_start))
            local quiet_status="quiet period complete"
            if [ "${quiet_elapsed}" -lt "${QUIET_PERIOD_SECS}" ]; then
                quiet_status="quiet period ${quiet_elapsed}s / ${QUIET_PERIOD_SECS}s"
            fi

            local non_terminal="" group_count
            if ! non_terminal="$(non_terminal_incident_groups)"; then
                log_error "FATAL: could not read incident group states from gs://${INCIDENTS_BUCKET}/${ACTIVE_INCIDENTS_URL_SUFFIX}; incident files were NOT archived." >&2
                gcs_error_hint
                log_error "To skip the check deliberately (risky: active groups get archived), re-run with --no-wait." >&2
                exit 1
            fi
            ignored_groups="$(printf '%s\n' "${non_terminal}" | awk 'sub(/^IGNORED /, "")')"
            non_terminal="$(printf '%s\n' "${non_terminal}" | awk 'NF && !/^IGNORED /')"
            if [ -n "${non_terminal}" ]; then
                group_count="$(count_lines "${non_terminal}")"
                if [ "${now}" -ge "${deadline}" ]; then
                    log_error "Timed out after ${WAIT_TIMEOUT_SECS}s (${elapsed}s elapsed): all ATA alerts are closed, but ${group_count} incident group(s) are not in a terminal state (${TERMINAL_GROUP_STATUSES}; recovered only once the recovery note is posted):" >&2
                    printf '%s\n' "${non_terminal}" | awk '{ print "  - " $0 }' >&2
                    log_error "Incident files were NOT archived. Check the agent (cooling timer, recovery note) and rerun, or raise --timeout." >&2
                    log_error "For a group that will never become terminal, re-run with --ignore-group <file> for that group (risky: it is archived as it is; the other waits still run), or --no-wait to skip every wait (riskier)." >&2
                    exit 1
                fi
                log_info "    [Progress] Elapsed: ${elapsed}s / ${WAIT_TIMEOUT_SECS}s | 0 open alerts | ${quiet_status} | ${group_count} incident group(s) not terminal yet [$(summarize_lines "${non_terminal}")]"
                sleep "${POLL_INTERVAL_SECS}"
                continue
            fi

            if [ "${QUIET_PERIOD_SECS}" -le 0 ] || [ "${quiet_elapsed}" -ge "${QUIET_PERIOD_SECS}" ]; then
                if [ -n "${ignored_groups}" ]; then
                    log_warn "    [--ignore-group] Archiving $(count_lines "${ignored_groups}") incident group(s) that are NOT terminal: $(summarize_lines "${ignored_groups}")"
                fi
            fi
            if [ "${QUIET_PERIOD_SECS}" -le 0 ]; then
                log_success "    Zero open ATA alerts and all incident groups terminal (quiet period disabled). Proceeding."
                return 0
            fi
            if [ "${quiet_elapsed}" -ge "${QUIET_PERIOD_SECS}" ]; then
                log_success "    Quiet period complete (${quiet_elapsed}s >= ${QUIET_PERIOD_SECS}s with zero open ATA alerts) and all incident groups terminal. Proceeding."
                return 0
            fi
            local quiet_remaining=$((QUIET_PERIOD_SECS - quiet_elapsed))
            log_info "    [Quiet Period] ${quiet_elapsed}s / ${QUIET_PERIOD_SECS}s elapsed (${quiet_remaining}s remaining, 0 open alerts, all incident groups terminal)..."
            sleep "${POLL_INTERVAL_SECS}"
        fi
    done
}

# --- Step 7/8: incident bucket housekeeping (gcloud storage via gcs_run) ----------
# Each function returns non-zero after logging the error and a hint. "No matching
# objects" is a normal empty result, never an error.

ACTIVE_INCIDENTS_URL_SUFFIX="knowledge/incident-manager/*.md"

count_lines() { printf '%s\n' "$1" | grep -c .; }

# Move every active incident file (knowledge/incident-manager/*.md) to archive/.
archive_active_incidents() {
    local src="gs://${INCIDENTS_BUCKET}/${ACTIVE_INCIDENTS_URL_SUFFIX}"
    local dst="gs://${INCIDENTS_BUCKET}/archive/"
    local active
    if ! active="$(gcs_run ls "${src}")"; then
        log_error "FATAL: could not list ${src}; incident files were NOT archived." >&2
        gcs_error_hint
        return 1
    fi
    if [ -z "${active}" ]; then
        log_info "    No active incident files found in knowledge/incident-manager/."
        return 0
    fi
    log_info "    Moving $(count_lines "${active}") active incident file(s) to ${dst}..."
    if ! gcs_run mv "${src}" "${dst}" > /dev/null; then
        log_error "FATAL: gcloud storage mv ${src} ${dst} failed; incident files were NOT archived." >&2
        gcs_error_hint
        return 1
    fi
    log_success "    Active incident files moved from knowledge/incident-manager/ to archive/."
}

# Delete leftover root copies (gs://BUCKET/*.md) from older agent versions.
clean_root_copies() {
    local src="gs://${INCIDENTS_BUCKET}/*.md"
    local copies
    if ! copies="$(gcs_run ls "${src}")"; then
        log_error "FATAL: could not list ${src}; leftover root copies were NOT removed." >&2
        gcs_error_hint
        return 1
    fi
    if [ -z "${copies}" ]; then
        log_info "    No leftover root incident copies in gs://${INCIDENTS_BUCKET}/."
        return 0
    fi
    log_info "    Deleting $(count_lines "${copies}") leftover root incident copies in gs://${INCIDENTS_BUCKET}/..."
    if ! gcs_run rm "${src}" > /dev/null; then
        log_error "FATAL: gcloud storage rm ${src} failed; leftover root copies were NOT removed." >&2
        gcs_error_hint
        return 1
    fi
    log_success "    Leftover root copies removed."
}

# Remove reports/verify_highlighting_001.html if present.
remove_verify_report() {
    local report="gs://${INCIDENTS_BUCKET}/reports/verify_highlighting_001.html"
    if ! gcs_run rm "${report}" > /dev/null; then
        log_error "FATAL: gcloud storage rm ${report} failed." >&2
        gcs_error_hint
        return 1
    fi
}

# Incident groups live in knowledge/incident-manager/ (the bridge is stateless), so
# "0 active incident groups" means that prefix holds no incident files after step 7.
verify_no_active_incident_groups() {
    local src="gs://${INCIDENTS_BUCKET}/${ACTIVE_INCIDENTS_URL_SUFFIX}"
    local active
    if ! active="$(gcs_run ls "${src}")"; then
        log_error "FATAL: could not list ${src} to verify 0 active incident groups." >&2
        gcs_error_hint
        return 1
    fi
    if [ -n "${active}" ]; then
        log_error "FATAL: $(count_lines "${active}") incident file(s) still active after archiving:" >&2
        printf '%s\n' "${active}" >&2
        log_error "Move them by hand with: gcloud storage mv '${src}' gs://${INCIDENTS_BUCKET}/archive/" >&2
        return 1
    fi
    log_success "    Verified 0 active incident groups (knowledge/incident-manager/ is empty)."
}

# --- Steps 1 and 9: authenticated demo admin calls ----------------------------
# A fresh Cloud Run ID token is minted for every call: a reset with a long quiet period
# outlives a token minted at start (QA: 401 about 10 min in).

# Print the HTTP status of METHOD BASE_URL+PATH ("000" when the request itself failed).
admin_http_status() {
    local method="$1" base="$2" path="$3"
    local token status
    local auth=()
    token="$(resolve_id_token "${base}")"
    if [ -n "${token}" ] && [ "${token}" != "mock-id-token" ]; then
        auth=(-H "Authorization: Bearer ${token}")
    fi
    status="$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 30 -X "${method}" \
        ${auth[@]+"${auth[@]}"} "${base}${path}" 2>/dev/null)" || true
    echo "${status:-000}"
}

# Explain a non-2xx admin status on stderr.
admin_status_error() {
    local what="$1" status="$2"
    case "${status}" in
        401|403)
            log_error "FATAL: ${what} was rejected with HTTP ${status} (authentication/authorization)." >&2
            log_error "The ID token from 'gcloud auth print-identity-token' was refused. Run 'gcloud auth login' and make sure the account has roles/run.invoker on the demo services in project ${PROJECT_ID}." >&2
            ;;
        000)
            log_error "FATAL: ${what} failed: no HTTP response (connection error or timeout)." >&2
            ;;
        *)
            log_error "FATAL: ${what} returned HTTP ${status}." >&2
            ;;
    esac
}

# Step 1: POST /admin/fault/reset on one demo app; non-2xx is fatal.
reset_app_faults() {
    local name="$1" base="$2" status
    status="$(admin_http_status POST "${base}" /admin/fault/reset)"
    case "${status}" in
        2??)
            log_success "    ${name}: POST ${base}/admin/fault/reset -> HTTP ${status}"
            ;;
        *)
            admin_status_error "POST ${base}/admin/fault/reset (${name})" "${status}"
            log_error "The ${name} fault state was NOT cleared." >&2
            return 1
            ;;
    esac
}

# Step 3: local pg_sleep helpers (started by s4_db_connection_pool.sh on this machine).
terminate_local_pg_sleep() {
    local rc=0
    pkill -f "pg_sleep" 2>/dev/null || rc=$?
    case "${rc}" in
        0) log_success "    Terminated local pg_sleep helper process(es)." ;;
        1) log_info "    No local pg_sleep helper processes were running." ;;
        *) log_warn "    pkill -f pg_sleep failed (exit ${rc}); local pg_sleep helpers may still be running." ;;
    esac
}

# Step 9: GET probes of the admin endpoints on the pinned inventory code (each returns 200).
verify_admin_endpoints() {
    local base="$1" path status failed=0
    for path in /admin/leak-connections /admin/fault/inject; do
        status="$(admin_http_status GET "${base}" "${path}")"
        case "${status}" in
            2??)
                log_success "    Verified ${path} endpoint exists (HTTP ${status})"
                ;;
            404)
                log_error "Admin endpoint ${path} is MISSING on ${base} (HTTP 404)!" >&2
                failed=$((failed + 1))
                ;;
            *)
                admin_status_error "GET ${base}${path}" "${status}"
                failed=$((failed + 1))
                ;;
        esac
    done
    if [ "${failed}" -gt 0 ]; then
        log_error "FATAL: ${failed} admin fault injection endpoint check(s) failed on ${base}." >&2
        return 1
    fi
}

# Sourced (tests): define the functions above without running the reset.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 0
fi

log_info "=== Starting Scenario Fault Cleanup & Baseline Restoration ==="

# Refuse to restore traffic to a pinned revision that does not run the pinned image.
verify_pinned_inventory_revision || exit 1

INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ORD_URL="$(resolve_service_url "${NAME_PREFIX}-orders-api" "https://ata-orders-api-4klvlqnmpa-uw.a.run.app")"

# 1. Reset Faults on Cloud Run Demo Apps
log_info "--> 1. Resetting demo applications via /admin/fault/reset..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    reset_app_faults "${NAME_PREFIX}-inventory-api" "${INV_URL}" || exit 1
    reset_app_faults "${NAME_PREFIX}-orders-api" "${ORD_URL}" || exit 1
    log_success "    Demo application fault states cleared."
else
    log_info "    [MOCK/DRY-RUN] Simulating demo application fault reset."
fi

# 2. Bridge State (Stateless; incident state managed in GCS OKFs by Agent)
log_info "--> 2. Bridge is stateless (incident state managed via GCS OKF objects)..."
log_success "    Bridge windowing reset skipped (stateless bridge)."

# 3. Terminate background DB sleep connections and close leaks
log_info "--> 3. Terminating background database sleep connections..."
terminate_local_pg_sleep

# 4. Restore Healthy Apigee Proxy Revisions
log_info "--> 4. Restoring healthy Apigee proxy revisions..."
if [ -f "${SCRIPT_DIR}/deploy-apigee-proxies.sh" ] && [ "${MOCK_MODE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
    APIGEE_LOG="$(mktemp)"
    if ! "${SCRIPT_DIR}/deploy-apigee-proxies.sh" > "${APIGEE_LOG}" 2>&1; then
        log_error "FATAL: deploy-apigee-proxies.sh failed; the Apigee proxy baselines were NOT restored. Last output:" >&2
        tail -n 20 "${APIGEE_LOG}" >&2
        rm -f "${APIGEE_LOG}"
        exit 1
    fi
    rm -f "${APIGEE_LOG}"
    log_success "    Apigee proxy baselines restored."
else
    log_info "    Apigee restore simulation completed."
fi

# 5. Restore Cloud Run Configuration if altered by S5
# Writes only what differs from the pinned baseline (see restore_inventory_pinned_baseline).
log_info "--> 5. Restoring Cloud Run configuration for ata-inventory-api to pinned revision ${PINNED_INVENTORY_REVISION}..."
if ! restore_inventory_pinned_baseline; then
    log_error "FATAL: could not restore ata-inventory-api to the pinned baseline." >&2
    exit 1
fi
log_success "    Cloud Run configuration restored to pinned baseline."

# 6. Wait for Open ATA Alerts to Close, Terminal Incident Groups & Quiet Period
wait_for_open_alerts

# 7. Archive Active Incident Files & Clean Leftover Root Storage
log_info "--> 7. Archiving OKF incident files from knowledge/incident-manager/ to archive/..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    archive_active_incidents || exit 1
    clean_root_copies || exit 1
    remove_verify_report || exit 1
else
    log_info "    Archive and cleanup simulation completed."
fi

# 8. Final Verification of Healthy State & Zero Active Groups
log_info "--> 8. Verifying healthy baseline state..."
API_KEY="$(resolve_api_key)"
KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    # curl -w prints 000 when the request itself fails.
    HEALTH_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/health" || true)
    HEALTH_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/health" || true)
    log_info "    Inventory Health: HTTP ${HEALTH_INV:-000}"
    log_info "    Orders Health:    HTTP ${HEALTH_ORD:-000}"
    if [ "${HEALTH_INV:-000}" != "200" ] || [ "${HEALTH_ORD:-000}" != "200" ]; then
        log_warn "    A health endpoint did not return HTTP 200 (see above)."
    fi

    verify_no_active_incident_groups || exit 1
fi

# 9. Admin Fault Injection Preflight Verification
log_info "--> 9. Verifying admin fault injection endpoints exist on deployed inventory service..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    # The GET probes below are only harmless on the pinned code; never send them elsewhere.
    verify_inventory_serving_pinned || exit 1
    verify_admin_endpoints "${INV_URL}" || exit 1
fi

log_success "=== All Scenarios Reset Successfully: Environment Healthy ==="

