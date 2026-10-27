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
# 4. Restore healthy Apigee proxy revisions (re-deploy base bundles that differ from the deployed revision)
# 5. Restore Cloud Run service configuration if S5 was deployed
# 5b. Warm up the demo services, check health (direct and via Apigee) and the admin fault
#    injection endpoints, and restock the demo items to INVENTORY_RESTOCK_BASELINE
#    (POST /admin/restock, then verified with GET /items). Every request the reset sends
#    to the demo services runs before step 6, and each is timed (see "Alert window" below).
#    --restock-only runs just the pinned-baseline checks and the restock.
# 5c. Verify on the database side (Cloud SQL num_backends gauge, read-only) that no leaked
#    connections remain: the fault reset frees only the Cloud Run instance it reaches.
# 6. Wait for open ATA alerts to close, then, concurrently, for the quiet period (Apigee
#    metric lag), for every incident group to reach a terminal state (recovery note posted)
#    and for the alert window to pass
# 7. Archive active OKF incident files and clean leftover root storage
# 8. Verify 0 active incident groups
#
# Alert window: the first request to an idle revision is a cold start (~5 s). Cloud Run's
# latency metric reaches the alert policies 5.5-7 min later, after a 3 min quiet period
# would have ended, so an alert caused by the reset's own requests used to open after the
# reset had archived and finished (stray ticket INC0010327). A demo request that is slower
# than the latency alert threshold, returns HTTP 5xx or gets no response therefore keeps
# step 6 watching for alerts for --alert-lag after it. An alert that opens in that time
# (the agent still tickets it while the latency policy has a 60s retest window) is waited
# for like any other before archiving: it must close and its incident group must become
# terminal. Only the Terraform retest window (latency_duration_secs) stops the ticket itself.
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
RESTOCK_ONLY=false
NO_RESTOCK=false
# Incident groups (file names or group ids) left out of the terminal-state wait only.
IGNORE_GROUPS="${ATA_RESET_IGNORE_GROUPS:-}"
# 180s covers Apigee's 1-3 min metric lag. The agent's recovery note is not covered by
# time: step 6 also waits (concurrently) for every incident group to reach a terminal state.
QUIET_PERIOD_SECS="$(parse_duration "${ATA_RESET_QUIET_PERIOD:-${QUIET_PERIOD:-180}}")"
# 30 min: QA's S4 reset took 896s (the connections alert closed ~7 min after the leak was
# released, the recovery note ~4 min after that). When the fault reset reaches a Cloud Run
# instance other than the one holding the leak, the leak lasts until that instance scales in
# (up to ~15 min idle), so the S4 worst case is ~15 + 7.5 + 4 = ~26.5 min. See DEMO.md, S4.
WAIT_TIMEOUT_SECS="$(parse_duration "${ATA_RESET_TIMEOUT:-${WAIT_TIMEOUT:-1800}}")"
POLL_INTERVAL_SECS="$(parse_duration "${ATA_RESET_POLL_INTERVAL:-${POLL_INTERVAL:-15}}")"
# Step 5c: the leak is released only when num_backends of the inventory database, sampled
# after the fault reset, is at most this many (baseline 0-2; S4 leaks showed 10-22).
DB_MAX_BACKENDS="${ATA_RESET_DB_MAX_BACKENDS:-5}"
# How long step 5c waits for such a sample: up to ~15 min until Cloud Run scales in an
# instance whose leak the fault reset did not reach, plus up to ~4.5 min of Cloud SQL
# sampling (60 s) and visibility (210 s) lag.
DB_RELEASE_TIMEOUT_SECS="$(parse_duration "${ATA_RESET_DB_RELEASE_TIMEOUT:-1200}")"
MAX_ALERT_AGE_SECS="$(parse_duration "${ATA_RESET_MAX_ALERT_AGE:-${MAX_ALERT_AGE:-3600}}")"
# Consecutive failed Cloud Monitoring queries tolerated before the reset fails.
MAX_QUERY_ATTEMPTS="${ATA_RESET_MAX_QUERY_ATTEMPTS:-3}"
# A demo request at least this slow (ms) can raise a <prefix>-*-latency alert: keep it equal to
# threshold_value of the latency policies (infra/terraform/observability/main.tf, 2000).
SLOW_REQUEST_MS="${ATA_RESET_SLOW_REQUEST_MS:-2000}"
# Time from a request to the alert it can open. Live 2026-10-27, with the 60s retest
# window: 332 s (cold /health 10:05:26Z, alert 10:10:58Z), 356 s (S2: first slow minute
# 07:34:53Z, alert 07:40:49Z) and 411 s (verify cold start 11:47:48Z, alert 11:54:39Z).
# 600 s covers 411 s plus 3 min, the extra time a 240s retest window can add.
ALERT_LAG_SECS="$(parse_duration "${ATA_RESET_ALERT_LAG:-600}")"
# Arm the alert window from the reset start, for requests made before the reset (e.g. by
# verify-demo-infra.sh, which tells you when to use this).
ALERT_WINDOW_FROM_START="${ATA_RESET_ALERT_WINDOW_FROM_START:-false}"
# Epoch until which step 6 keeps watching for alerts (0 = no window armed).
ALERT_WINDOW_UNTIL=0
RESET_START_TIME="$(date +%s)"

# Usage error for an option given without its value ($1 = option, $2 = args left incl. it),
# instead of set -u's '$2: unbound variable'.
require_option_value() {
    if [ "$2" -lt 2 ]; then
        log_error "Option $1 requires a value (see --help)." >&2
        exit 1
    fi
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-wait)
            NO_WAIT="true"
            shift
            ;;
        --restock-only)
            RESTOCK_ONLY="true"
            shift
            ;;
        --no-restock)
            NO_RESTOCK="true"
            shift
            ;;
        --ignore-group)
            require_option_value "$1" "$#"
            IGNORE_GROUPS="${IGNORE_GROUPS:+${IGNORE_GROUPS} }$2"
            shift 2
            ;;
        --ignore-group=*)
            IGNORE_GROUPS="${IGNORE_GROUPS:+${IGNORE_GROUPS} }${1#*=}"
            shift
            ;;
        --quiet-period)
            require_option_value "$1" "$#"
            QUIET_PERIOD_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --quiet-period=*)
            QUIET_PERIOD_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --timeout)
            require_option_value "$1" "$#"
            WAIT_TIMEOUT_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --timeout=*)
            WAIT_TIMEOUT_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --poll-interval)
            require_option_value "$1" "$#"
            POLL_INTERVAL_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --poll-interval=*)
            POLL_INTERVAL_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --max-alert-age)
            require_option_value "$1" "$#"
            MAX_ALERT_AGE_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --max-alert-age=*)
            MAX_ALERT_AGE_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --alert-lag)
            require_option_value "$1" "$#"
            ALERT_LAG_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --alert-lag=*)
            ALERT_LAG_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --alert-window-from-start)
            ALERT_WINDOW_FROM_START="true"
            shift
            ;;
        --serving-revisions)
            require_option_value "$1" "$#"
            ATA_SERVING_REVISIONS="$2"
            shift 2
            ;;
        --serving-revisions=*)
            ATA_SERVING_REVISIONS="${1#*=}"
            shift
            ;;
        --monitoring-token-source)
            require_option_value "$1" "$#"
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
            echo "                             a terminal state (default: 1800s / 30m)"
            echo "  --poll-interval <duration> Polling interval for alert and incident group queries (default: 15s)"
            echo "  --max-alert-age <duration> Max age of open alerts before reset start (default: 60m / 3600s)"
            echo "  --alert-lag <duration>     How long after a slow (>= ATA_RESET_SLOW_REQUEST_MS, default 2000 ms), HTTP 5xx"
            echo "                             or failed demo request step 6 keeps watching for the alert it can open"
            echo "                             (Cloud Run metric lag; default: 600s / 10m; env ATA_RESET_ALERT_LAG)."
            echo "                             --timeout then counts from the end of this window."
            echo "  --alert-window-from-start  Watch for alerts for --alert-lag from the reset start even if no request of the"
            echo "                             reset was slow: for slow requests made just before it (verify-demo-infra.sh"
            echo "                             says when; env ATA_RESET_ALERT_WINDOW_FROM_START=true)"
            echo "  --serving-revisions <list> Comma or space separated list of active Cloud Run revisions"
            echo "  --monitoring-token-source <adc|gcloud>"
            echo "                             Token for the alert check: adc = 'gcloud auth application-default print-access-token',"
            echo "                             gcloud = 'gcloud auth print-access-token'"
            echo "                             (default: adc; env ATA_MONITORING_TOKEN_SOURCE). No fallback between them."
            echo "  --restock-only             Only restock the demo items (POST /admin/restock on the pinned revision, then"
            echo "                             verify with GET /items) to ATA_INVENTORY_RESTOCK_BASELINE"
            echo "                             (default: item-101..item-105 = 1000 each); every full reset does this too"
            echo "  --no-restock               Skip the restock (e.g. after a re-pin rollback to code without /admin/restock);"
            echo "                             warns that demo stock is NOT restocked"
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

# Fail before any reset step on an invalid alert-window setting (no silent default).
validate_alert_window_settings() {
    case "${SLOW_REQUEST_MS}" in
        ""|*[!0-9]*|0)
            log_error "Invalid ATA_RESET_SLOW_REQUEST_MS '${SLOW_REQUEST_MS}' (expected a positive number of milliseconds, the latency alert threshold)." >&2
            return 1
            ;;
    esac
    case "${ALERT_LAG_SECS}" in
        ""|*[!0-9]*)
            log_error "Invalid --alert-lag / ATA_RESET_ALERT_LAG '${ALERT_LAG_SECS}' (expected e.g. 600, 600s or 10m)." >&2
            return 1
            ;;
    esac
    case "${ALERT_WINDOW_FROM_START}" in
        true|false) ;;
        *)
            log_error "Invalid ATA_RESET_ALERT_WINDOW_FROM_START '${ALERT_WINDOW_FROM_START}' (expected true or false)." >&2
            return 1
            ;;
    esac
}
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
        for svc in "${SVC_INVENTORY}" "${SVC_ORDERS}"; do
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
DB_BACKENDS_PY="${SCRIPT_DIR}/scenarios/db_backends.py"

# Step 5c: confirm on the database side that the fault reset released S4's leaked
# connections. POST /admin/fault/reset frees only the connections of the Cloud Run instance
# it reaches (QA round 2: it reached another instance, and the leak lasted until the leaking
# instance scaled in). Polls Cloud Monitoring (read-only) for the num_backends gauge of the
# inventory database until a sample taken at or after $1 (epoch of the fault reset) is at
# most DB_MAX_BACKENDS. Fails loudly, naming what remains, after DB_RELEASE_TIMEOUT_SECS.
verify_db_backends_released() {
    local since="$1" deadline now token json rc parsed value at epoch last="" err_file failures=0
    local db="${DB_INSTANCE}"
    deadline=$(( $(date +%s) + DB_RELEASE_TIMEOUT_SECS ))
    err_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-db-backends.XXXXXX")"
    while true; do
        rc=0
        if ! token="$(resolve_monitoring_token 2>"${err_file}")" || [ -z "${token}" ]; then
            log_error "FATAL: could not get a Cloud Monitoring token from source '${ATA_MONITORING_TOKEN_SOURCE}' to check ${db} connections. Run: $(monitoring_token_login_hint "${ATA_MONITORING_TOKEN_SOURCE}")" >&2
            rm -f "${err_file}"
            return 1
        fi
        json="$(python3 "${DB_BACKENDS_PY}" "${PROJECT_ID}" "${token}" "${db}" inventory 2>"${err_file}")" || rc=$?
        now="$(date +%s)"
        if [ "${rc}" -ne 0 ]; then
            failures=$((failures + 1))
            if [ "${rc}" -ne 3 ] || [ "${failures}" -ge "${MAX_QUERY_ATTEMPTS}" ] || [ "${now}" -ge "${deadline}" ]; then
                log_error "FATAL: cannot read the num_backends gauge of ${db} (Cloud Monitoring); the release of S4's leaked connections is NOT verified." >&2
                log_error "Cause: $(tail -n 2 "${err_file}" | tr '\n' ' ')" >&2
                rm -f "${err_file}"
                return 1
            fi
            log_warn "    num_backends query failed (attempt ${failures}, retrying): $(tail -n 1 "${err_file}")" >&2
            sleep "${POLL_INTERVAL_SECS}"
            continue
        fi
        failures=0
        if ! parsed="$(printf '%s' "${json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("" if d.get("value") is None else "%s %s %s" % (d["value"], d["epoch"], d["at"]))
')"; then
            log_error "FATAL: unparseable num_backends output: ${json:0:200}" >&2
            rm -f "${err_file}"
            return 1
        fi
        if [ -n "${parsed}" ]; then
            # shellcheck disable=SC2086
            set -- ${parsed}
            value="$1" epoch="$2" at="$3"
            last="${value} at ${at}"
            if [ "${epoch}" -ge "${since}" ] && [ "${value}" -le "${DB_MAX_BACKENDS}" ]; then
                log_success "    ${db}: ${value} backend(s) on database inventory at ${at} (<= ${DB_MAX_BACKENDS}); leaked connections released."
                rm -f "${err_file}"
                return 0
            fi
        fi
        if [ "${now}" -ge "${deadline}" ]; then
            log_error "FATAL: ${db} still has leaked connections ${DB_RELEASE_TIMEOUT_SECS}s after the fault reset: num_backends on database inventory is ${last:-not reported} (released means <= ${DB_MAX_BACKENDS} in a sample taken after the reset)." >&2
            log_error "STILL LEFT: S4's leaked connections, held by an ${SVC_INVENTORY} instance that POST /admin/fault/reset did not reach; ${POLICY_CLOUDSQL_CONNECTIONS} stays open until they are released." >&2
            log_error "Rerun ./scripts/reset-scenarios.sh once Cloud Run has scaled that instance in, or raise ATA_RESET_DB_RELEASE_TIMEOUT. With max_instance_count = 1 (Terraform demo_max_instance_count) the reset always reaches it." >&2
            rm -f "${err_file}"
            return 1
        fi
        log_info "    [Progress] ${db} num_backends (database inventory): ${last:-no sample yet}; waiting for a sample taken after the fault reset that is <= ${DB_MAX_BACKENDS} ($((deadline - now))s left)."
        sleep "${POLL_INTERVAL_SECS}"
    done
}

# Print {"active": [...], "ignored": [...]} for OPEN alerts of this stack (<prefix>-* policies). Exits non-zero, with
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
# Entries are compared literally: noglob is on while the list is word-split (a '*' entry must
# not expand to the files in the cwd), then restored to the caller's setting.
is_ignored_group() {
    local entry found=1 had_noglob=false
    case "$-" in *f*) had_noglob=true ;; esac
    set -f
    for entry in ${IGNORE_GROUPS//,/ }; do
        if [ "$1" = "${entry}" ] || [ "$1" = "incident-${entry}.md" ]; then
            found=0
            break
        fi
    done
    if [ "${had_noglob}" = "false" ]; then
        set +f
    fi
    return "${found}"
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
        if [ "${ALERT_WINDOW_UNTIL}" -gt "$(date +%s)" ]; then
            log_warn "    [--no-wait] Not waiting for alerts from slow demo requests (alert window $((ALERT_WINDOW_UNTIL - $(date +%s)))s left): such an alert can open after this reset and create a ticket."
        fi
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
    # An alert from a slow demo request can open as late as the end of the alert window; it
    # then needs to close and its group to become terminal, so --timeout counts from there.
    if [ $((ALERT_WINDOW_UNTIL + WAIT_TIMEOUT_SECS)) -gt "${deadline}" ]; then
        deadline=$((ALERT_WINDOW_UNTIL + WAIT_TIMEOUT_SECS))
    fi
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

    log_info "    Polling Cloud Monitoring API (read-only) for OPEN alerts on ${NAME_PREFIX}-* policies (stack ${NAME_PREFIX})..."
    log_info "    Max timeout: ${WAIT_TIMEOUT_SECS}s | Quiet period: ${QUIET_PERIOD_SECS}s | Polling interval: ${POLL_INTERVAL_SECS}s | Max alert age: ${MAX_ALERT_AGE_SECS}s | Token source: ${ATA_MONITORING_TOKEN_SOURCE}"
    if [ "${ALERT_WINDOW_UNTIL}" -gt "${start_time}" ]; then
        log_info "    Alert window: watching for alerts from slow demo requests for another $((ALERT_WINDOW_UNTIL - start_time))s (max timeout counted from its end: $((deadline - start_time))s from now)."
    fi
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

            # Alerts caused by the reset's own slow requests open up to ALERT_LAG_SECS later
            # (Cloud Run metric lag): keep watching until then. An alert that opens restarts
            # the quiet period above and its group is waited for like any other.
            if [ "${now}" -lt "${ALERT_WINDOW_UNTIL}" ]; then
                log_info "    [Alert window] $((ALERT_WINDOW_UNTIL - now))s left for alerts from slow demo requests | 0 open alerts | ${quiet_status} | all incident groups terminal"
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

# --- Steps 1 and 5b: timed demo requests ---------------------------------------

# Arm or extend the step-6 alert window for a request sent at epoch $1 (reason $2).
extend_alert_window() {
    local at="$1" reason="$2" deadline
    deadline=$((at + ALERT_LAG_SECS))
    if [ "${deadline}" -gt "${ALERT_WINDOW_UNTIL}" ]; then
        ALERT_WINDOW_UNTIL="${deadline}"
    fi
    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_warn "    [MOCK/DRY-RUN] ${reason}. A live reset would keep watching for alerts for ${ALERT_LAG_SECS}s in step 6 (simulated here)."
        return 0
    fi
    log_warn "    ${reason}. A latency or 5xx alert from it can open up to ${ALERT_LAG_SECS}s later; step 6 keeps watching for alerts until then before archiving."
}

# demo_request <label> <curl args...>: send one timed request to a demo service. Sets
# LAST_HTTP_STATUS ("000" without a response) and LAST_HTTP_MS ("" when curl reported no
# duration). A request that can raise an alert (>= SLOW_REQUEST_MS, HTTP 5xx, no response
# or unknown duration) arms the alert window. Call it in the current shell, not in $(...),
# or the window is lost.
demo_request() {
    demo_request_to /dev/null "$@"
}

# demo_request_to <body file> <label> <curl args...>: demo_request, keeping the response body.
demo_request_to() {
    local body_file="$1" label="$2" out status secs="" ms at reason=""
    shift 2
    at="$(date +%s)"
    # LC_ALL=C: time_total and awk's number parsing use a '.' decimal point.
    out="$(LC_ALL=C curl -s -o "${body_file}" -w "%{http_code} %{time_total}" "$@" 2>/dev/null)" || true
    status="${out%% *}"
    case "${out}" in
        *" "*) secs="${out#* }" ;;
    esac
    case "${status}" in
        [0-9][0-9][0-9]) ;;
        *) status="000" ;;
    esac
    ms="$(LC_ALL=C awk -v s="${secs}" 'BEGIN { if (s ~ /^[0-9]+([.][0-9]+)?$/) printf "%d", s * 1000 }')"
    LAST_HTTP_STATUS="${status}"
    LAST_HTTP_MS="${ms}"
    case "${status}" in
        5??|000) reason="${label} -> HTTP ${status}" ;;
        *)
            if [ -z "${ms}" ]; then
                reason="${label}: curl reported no request duration ('${out}')"
            elif [ "${ms}" -ge "${SLOW_REQUEST_MS}" ]; then
                reason="${label} took ${ms} ms (>= ${SLOW_REQUEST_MS} ms, the latency alert threshold; a cold start?)"
            fi
            ;;
    esac
    if [ -n "${reason}" ]; then
        extend_alert_window "${at}" "${reason}"
    fi
}

# Authenticated request METHOD BASE_URL+PATH to a demo service (demo_request: sets
# LAST_HTTP_STATUS/LAST_HTTP_MS). A fresh Cloud Run ID token is minted for every call: a
# reset with a long quiet period outlives a token minted at start (QA: 401 about 10 min in).
admin_request() {
    admin_request_to /dev/null "$@"
}

# admin_request_to <body file> METHOD BASE_URL PATH [curl args...]: admin_request, keeping
# the response body; extra curl args (e.g. -H/-d for a JSON body) go before the URL.
admin_request_to() {
    local body_file="$1" method="$2" base="$3" path="$4"
    local token
    local auth=()
    shift 4
    token="$(resolve_id_token "${base}")"
    if [ -n "${token}" ] && [ "${token}" != "mock-id-token" ]; then
        auth=(-H "Authorization: Bearer ${token}")
    fi
    demo_request_to "${body_file}" "${method} ${base}${path}" --connect-timeout 5 --max-time 30 -X "${method}" \
        ${auth[@]+"${auth[@]}"} "$@" "${base}${path}"
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
    admin_request POST "${base}" /admin/fault/reset
    status="${LAST_HTTP_STATUS}"
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

# Step 3: local pg_sleep helpers. s4_db_connection_pool.sh no longer starts any (its local
# Cloud SQL helper was removed); this only clears ones left by an older checkout's S4.
terminate_local_pg_sleep() {
    local rc=0
    pkill -f "pg_sleep" 2>/dev/null || rc=$?
    case "${rc}" in
        0) log_success "    Terminated local pg_sleep helper process(es)." ;;
        1) log_info "    No local pg_sleep helper processes were running." ;;
        *) log_warn "    pkill -f pg_sleep failed (exit ${rc}); local pg_sleep helpers may still be running." ;;
    esac
}

# Step 5b: GET /health on one demo app with its ID token; the first request after idling
# is the cold start. Non-2xx is fatal.
warm_up_service() {
    local name="$1" base="$2" status
    admin_request GET "${base}" /health
    status="${LAST_HTTP_STATUS}"
    case "${status}" in
        2??)
            log_success "    ${name}: GET ${base}/health -> HTTP ${status} in ${LAST_HTTP_MS:-?} ms"
            ;;
        *)
            admin_status_error "GET ${base}/health (${name})" "${status}"
            return 1
            ;;
    esac
}

# Step 5b: unauthenticated health checks through Apigee (reach the backends too). A
# non-200 is logged as a warning, as before.
check_apigee_health() {
    local name status base
    for name in inventory orders; do
        case "${name}" in
            inventory) base="${BASEPATH_INVENTORY}" ;;
            *) base="${BASEPATH_ORDERS}" ;;
        esac
        demo_request "GET https://${APIGEE_HOST}${base}/health" -k --connect-timeout 5 --max-time 10 \
            "https://${APIGEE_HOST}${base}/health"
        status="${LAST_HTTP_STATUS}"
        log_info "    Apigee ${name} health: HTTP ${status} in ${LAST_HTTP_MS:-?} ms"
        if [ "${status}" != "200" ]; then
            log_warn "    Apigee ${name} /health did not return HTTP 200."
        fi
    done
}

# Step 5b: GET probes of the admin endpoints on the pinned inventory code (each returns 200).
verify_admin_endpoints() {
    local base="$1" path status failed=0
    for path in /admin/leak-connections /admin/fault/inject; do
        admin_request GET "${base}" "${path}"
        status="${LAST_HTTP_STATUS}"
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

# Step 5b (and --restock-only): set the demo items' stock to INVENTORY_RESTOCK_BASELINE via
# POST /admin/restock on the pinned revision (absolute values: idempotent), check the
# response, then read it back with GET /items. Fails loudly, naming what is wrong; a missing
# endpoint means the serving code predates the restock (re-pin: scripts/repin-inventory.sh).
restock_inventory() {
    local base="$1" payload body_file status
    payload="$(python3 -c '
import json, sys
print(json.dumps({"items": {k: int(v) for k, v in (e.split("=", 1) for e in sys.argv[1].split(","))}}))
' "${INVENTORY_RESTOCK_BASELINE}")" || { log_error "FATAL: cannot build the restock request from ATA_INVENTORY_RESTOCK_BASELINE." >&2; return 1; }
    body_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-restock.XXXXXX")"
    admin_request_to "${body_file}" POST "${base}" /admin/restock -H "Content-Type: application/json" -d "${payload}"
    status="${LAST_HTTP_STATUS}"
    case "${status}" in
        2??) ;;
        404)
            log_error "FATAL: POST ${base}/admin/restock -> HTTP 404: the serving inventory revision has no restock endpoint (code older than the restock change)." >&2
            log_error "Re-pin ${SVC_INVENTORY} to current code first: scripts/repin-inventory.sh (DEPLOY.md, 'Re-pinning the inventory baseline'), or, deliberately on old code, reset with --no-restock. Demo stock was NOT restocked." >&2
            rm -f "${body_file}"
            return 1
            ;;
        400)
            log_error "FATAL: POST ${base}/admin/restock rejected the baseline (HTTP 400): $(head -c 300 "${body_file}"). Fix ATA_INVENTORY_RESTOCK_BASELINE. Demo stock was NOT restocked." >&2
            rm -f "${body_file}"
            return 1
            ;;
        *)
            admin_status_error "POST ${base}/admin/restock" "${status}"
            log_error "Response: $(head -c 300 "${body_file}" 2>/dev/null). Demo stock was NOT restocked." >&2
            rm -f "${body_file}"
            return 1
            ;;
    esac
    if ! python3 "${SCRIPT_DIR}/scenarios/inventory_baseline.py" stock --expect="${INVENTORY_RESTOCK_BASELINE}" < "${body_file}"; then
        log_error "FATAL: POST ${base}/admin/restock answered HTTP ${status} but not with the baseline stock (above)." >&2
        rm -f "${body_file}"
        return 1
    fi
    admin_request_to "${body_file}" GET "${base}" /items
    status="${LAST_HTTP_STATUS}"
    if [ "${status}" != "200" ]; then
        admin_status_error "GET ${base}/items (restock check)" "${status}"
        rm -f "${body_file}"
        return 1
    fi
    if ! python3 "${SCRIPT_DIR}/scenarios/inventory_baseline.py" stock --expect="${INVENTORY_RESTOCK_BASELINE}" < "${body_file}"; then
        log_error "FATAL: GET ${base}/items after the restock does not show the baseline stock (above); were orders sent during the reset?" >&2
        rm -f "${body_file}"
        return 1
    fi
    rm -f "${body_file}"
    log_success "    Restocked and verified: ${INVENTORY_RESTOCK_BASELINE}"
}

# Step 4: deploy-apigee-proxies.sh imports and deploys a proxy only when its rendered bundle
# differs from the deployed revision, so an unchanged proxy adds no deployment to the change
# window. Its per-proxy verdicts (and any WARNING that forced a redeploy) are echoed here.
restore_apigee_proxies() {
    local log line
    log="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-apigee-deploy.XXXXXX")"
    if ! "${SCRIPT_DIR}/deploy-apigee-proxies.sh" > "${log}" 2>&1; then
        log_error "FATAL: deploy-apigee-proxies.sh failed; the Apigee proxy baselines were NOT restored. Last output:" >&2
        tail -n 20 "${log}" >&2
        rm -f "${log}"
        return 1
    fi
    while IFS= read -r line; do
        case "${line}" in
            WARNING:*) log_warn "    ${line}" >&2 ;;
            *"not redeployed."*|*"differs from the rendered bundle"*|*"Imported revision"*|*"is READY"*) log_info "    ${line#"${line%%[! ]*}"}" ;;
        esac
    done < "${log}"
    rm -f "${log}"
    log_success "    Apigee proxy baselines restored."
}

# Sourced (tests): define the functions above without running the reset.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 0
fi

validate_alert_window_settings || exit 1
validate_restock_baseline || exit 1
# Apigee inputs before any step: step 4 runs deploy-apigee-proxies.sh (it needs APIGEE_HOST
# and APIGEE_ENV; common.sh exports them from .${NAME_PREFIX}-apigee.env), step 5b probes
# ${BASEPATH_INVENTORY}/health and ${BASEPATH_ORDERS}/health on APIGEE_HOST.
if [ "${RESTOCK_ONLY}" != "true" ]; then
    require_apigee_host || exit 1
    require_apigee_mgmt || exit 1
fi
if [ "${RESTOCK_ONLY}" = "true" ] && [ "${NO_RESTOCK}" = "true" ]; then
    log_error "--restock-only and --no-restock contradict each other." >&2
    exit 2
fi

if [ "${RESTOCK_ONLY}" = "true" ]; then
    log_info "=== Restocking demo inventory (--restock-only) ==="
    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_info "    [MOCK/DRY-RUN] Would POST /admin/restock ${INVENTORY_RESTOCK_BASELINE} and verify with GET /items."
        exit 0
    fi
    # Admin calls only on the pinned code (as in step 5b).
    verify_pinned_inventory_revision || exit 1
    verify_inventory_serving_pinned || exit 1
    INV_URL="$(require_service_url "${SVC_INVENTORY}")" || exit 1
    restock_inventory "${INV_URL}" || exit 1
    if [ "${ALERT_WINDOW_UNTIL}" -gt "$(date +%s)" ]; then
        log_warn "A restock request was slow or failed (above): a ${NAME_PREFIX}-inventory latency/5xx alert can open within ${ALERT_LAG_SECS}s. Run ./scripts/reset-scenarios.sh before the next scenario." >&2
    fi
    log_success "=== Demo inventory restocked ==="
    exit 0
fi

log_info "=== Starting Scenario Fault Cleanup & Baseline Restoration ==="
if [ "${ALERT_WINDOW_FROM_START}" = "true" ]; then
    extend_alert_window "${RESET_START_TIME}" "--alert-window-from-start: requests made before this reset may have been slow"
fi

# Refuse to restore traffic to a pinned revision that does not run the pinned image.
verify_pinned_inventory_revision || exit 1

INV_URL="$(require_service_url "${SVC_INVENTORY}")" || exit 1
ORD_URL="$(require_service_url "${SVC_ORDERS}")" || exit 1

# 1. Reset Faults on Cloud Run Demo Apps
log_info "--> 1. Resetting demo applications via /admin/fault/reset..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    reset_app_faults "${SVC_INVENTORY}" "${INV_URL}" || exit 1
    reset_app_faults "${SVC_ORDERS}" "${ORD_URL}" || exit 1
    FAULT_RESET_EPOCH="$(date +%s)"
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
if [ "${MOCK_MODE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
    if [ ! -x "${SCRIPT_DIR}/deploy-apigee-proxies.sh" ]; then
        log_error "FATAL: ${SCRIPT_DIR}/deploy-apigee-proxies.sh is missing or not executable; the Apigee proxy baselines were NOT restored." >&2
        exit 1
    fi
    restore_apigee_proxies || exit 1
else
    log_info "    [MOCK/DRY-RUN] Apigee restore simulation completed (deploy-apigee-proxies.sh not run)."
fi

# 5. Restore Cloud Run Configuration if altered by S5
# Writes only what differs from the pinned baseline (see restore_inventory_pinned_baseline).
log_info "--> 5. Restoring Cloud Run configuration for ${SVC_INVENTORY} to pinned revision ${PINNED_INVENTORY_REVISION}..."
if ! restore_inventory_pinned_baseline; then
    log_error "FATAL: could not restore ${SVC_INVENTORY} to the pinned baseline." >&2
    exit 1
fi
log_success "    Cloud Run configuration restored to pinned baseline."

# 5b. Warm Up the Demo Services (every demo request of the reset runs before step 6)
# The direct /health calls take the cold start of an idle revision (e.g. the pinned
# revision after an S5 revert); a slow one arms the alert window that step 6 waits out.
log_info "--> 5b. Warming up the demo services and checking health and admin endpoints before the alert wait..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    warm_up_service "${SVC_INVENTORY}" "${INV_URL}" || exit 1
    warm_up_service "${SVC_ORDERS}" "${ORD_URL}" || exit 1
    check_apigee_health
    # The admin GET probes are only harmless on the pinned code; never send them elsewhere.
    verify_inventory_serving_pinned || exit 1
    verify_admin_endpoints "${INV_URL}" || exit 1
    if [ "${NO_RESTOCK}" = "true" ]; then
        log_warn "    --no-restock: demo stock is NOT restocked; orders return HTTP 400 once an item runs out."
    else
        restock_inventory "${INV_URL}" || exit 1
    fi
else
    log_info "    [MOCK/DRY-RUN] Simulating warm-up, health and admin endpoint checks."
    log_info "    [MOCK/DRY-RUN] Would restock ${INVENTORY_RESTOCK_BASELINE} via POST /admin/restock and verify with GET /items."
fi
if [ "${ALERT_WINDOW_UNTIL}" -gt "$(date +%s)" ]; then
    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_warn "    [MOCK/DRY-RUN] Alert window armed ($((ALERT_WINDOW_UNTIL - $(date +%s)))s); step 6 is simulated and does not wait."
    else
        log_warn "    Alert window armed: step 6 watches for alerts for another $((ALERT_WINDOW_UNTIL - $(date +%s)))s before archiving."
    fi
else
    log_info "    No slow, 5xx or failed demo request: no alert window needed."
fi

# 5c. The fault reset clears only the instance it reached: confirm on the database side.
log_info "--> 5c. Verifying on ${NAME_PREFIX}-inventory-db (num_backends gauge) that no leaked connections remain..."
if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
    log_info "    [MOCK/DRY-RUN] Simulating the num_backends check."
elif [ "${NO_WAIT}" = "true" ]; then
    log_warn "    --no-wait: the release of leaked DB connections is NOT verified."
else
    verify_db_backends_released "${FAULT_RESET_EPOCH}" || exit 1
fi

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

# 8. Final Verification of Zero Active Groups
# No demo request after step 6: the health and admin checks ran in step 5b, so nothing the
# reset sends can open an alert after it has archived.
log_info "--> 8. Verifying 0 active incident groups..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    verify_no_active_incident_groups || exit 1
fi

log_success "=== All Scenarios Reset Successfully: Environment Healthy ==="

