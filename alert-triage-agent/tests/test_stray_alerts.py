"""Stray latency tickets from single cold-start requests (QA round 2, INC0010327/INC0010328).

The latency policies (p99 > 2000 ms per service, 60s alignment) used a 60s retest window,
so one cold-start burst on an idle revision (one 60s window with p99 ~4.9 s at 4-12 rpm)
opened a P2 ticket ~6 min later, after the reset had finished. Live per-minute data
(Cloud Monitoring, 2026-10-27): the strays violated exactly 1 window each, the S5 deploy
cold start 2 windows (09:26 at 4 rpm, 09:27 at 47 rpm), S2 8 consecutive windows
(inventory-api 87-89 rpm, orders-api 30 rpm). A request-volume AND gate would not stop the
S5 cold start (47 rpm) and, per Cloud Monitoring, an AND policy opens one alert per met
condition (a second, request_count alert per firing). So the fix is the retest window.
"""

import re
from pathlib import Path

OBS = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "observability"


def _policy_block(main_tf: str, name: str) -> str:
    start = main_tf.index(f'resource "google_monitoring_alert_policy" "{name}"')
    nxt = main_tf.find('resource "google_monitoring_alert_policy"', start + 1)
    return main_tf[start: nxt if nxt != -1 else len(main_tf)]


def _variable_block(name: str) -> str:
    text = (OBS / "variables.tf").read_text()
    start = text.index(f'variable "{name}"')
    nxt = text.find("\nvariable ", start + 1)
    return text[start: nxt if nxt != -1 else len(text)]


def _default_duration() -> int:
    m = re.search(r"default\s*=\s*(\d+)", _variable_block("latency_duration_secs"))
    assert m
    return int(m.group(1))


def test_latency_policies_take_the_retest_window_from_a_variable():
    main_tf = (OBS / "main.tf").read_text()
    for name in ("orders_latency", "inventory_latency"):
        block = _policy_block(main_tf, name)
        assert 'duration                = "${var.latency_duration_secs}s"' in block, name
        assert 'alignment_period     = "60s"' in block, name
        # One condition, OR combiner: no request_count AND gate (it would open a second alert).
        assert block.count("conditions {") == 1, name
        assert 'combiner              = "OR"' in block, name
        assert "request_count" not in block, name


def test_retest_window_default_outlasts_a_cold_start_but_not_s2():
    alignment = 60
    duration = _default_duration()
    # A cold start (one ~5 s burst) raised the p99 of 1 or 2 consecutive 60s windows live; two
    # adjacent windows can keep the sliding-window condition true for just under 180s.
    assert duration > 3 * alignment
    # S2 held p99 > 6500 ms for 8 consecutive windows (7 min hold); the alert must still open
    # with room to spare (at least two windows more than the retest window needs).
    s2_violating_windows = 8
    assert duration // alignment + 2 <= s2_violating_windows


def test_retest_window_variable_is_validated():
    block = _variable_block("latency_duration_secs")
    assert "type        = number" in block
    assert "validation {" in block
    assert "var.latency_duration_secs >= 60" in block
    assert "var.latency_duration_secs % 60 == 0" in block


def test_other_policies_keep_their_literal_60s_retest_window():
    main_tf = (OBS / "main.tf").read_text()
    for name in ("orders_5xx", "inventory_5xx", "apigee_orders_5xx", "apigee_orders_429",
                 "cloudsql_connections", "cloudsql_cpu"):
        assert re.search(r'duration\s*=\s*"60s"', _policy_block(main_tf, name)), name


# =======================================================================================
# reset-scenarios.sh: timed demo requests, step 5b warm-up, step-6 alert window
# =======================================================================================

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from agent.app.grouping.models import IncidentGroupStatus  # noqa: E402
from tests.test_reset_terminal_groups import done_at, gcs, okf, out, put, run_step6  # noqa: E402,F401

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
RESET_SCRIPT = SCRIPTS_DIR / "reset-scenarios.sh"
VERIFY_SCRIPT = SCRIPTS_DIR / "verify-demo-infra.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
BASE = "https://inv.example.run.app"

# curl stub: logs "METHOD URL" to $PWD/curl_calls. $PWD/statuses has "<url suffix> <code>
# [<seconds>]" lines (first suffix match wins; default 200 in 0.012 s; code "fail" = no
# response, exit 7, as curl prints "000 <time>" for -w then). Writes a JSON item list to
# the -o file for .../items and prints "<code> <time_total>" like -w "%{http_code} %{time_total}".
FAKE_CURL = r"""#!/bin/sh
method=GET; url=""; outfile=""
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -o) outfile="$2"; shift 2 ;;
    -H|-d|-w|--connect-timeout|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$method $url" >> "$PWD/curl_calls"
line=$(awk -v u="$url" '{ n = length($1); if (n <= length(u) && substr(u, length(u) - n + 1) == $1) { print; exit } }' "$PWD/statuses" 2>/dev/null)
code=$(echo "$line" | awk '{print $2}')
secs=$(echo "$line" | awk '{print $3}')
if [ "$code" = "fail" ]; then printf '000 %s' "${secs:-0.001}"; exit 7; fi
case "$url" in */items) body='[{"id": "item-7", "stock": 3}]' ;; *) body='{}' ;; esac
if [ -n "$outfile" ] && [ "$outfile" != "/dev/null" ]; then printf '%s' "$body" > "$outfile"; fi
printf '%s %s' "${code:-200}" "${secs:-0.012}"
"""

FAKE_GCLOUD = r"""#!/bin/sh
case "$*" in
  "auth print-identity-token"*) echo "token" ;;
  # verify-demo-infra.sh looks the demo services up by exact name (no fallback URL).
  "run services list"*"metadata.name=ata-inventory-api"*) printf 'ata-inventory-api\thttps://ata-inventory-api-test123-uc.a.run.app\n' ;;
  "run services list"*"metadata.name=ata-orders-api"*) printf 'ata-orders-api\thttps://ata-orders-api-test123-uc.a.run.app\n' ;;
  *) exit 1 ;;
esac
"""


def _stub_env(tmp_path, statuses="", **extra):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    if not (bindir / "bash").exists():
        (bindir / "bash").symlink_to(TEST_BASH)
    (tmp_path / "statuses").write_text(statuses)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ATA_RESET_") and k not in ("ATA_SERVING_REVISIONS", "ATA_MONITORING_TOKEN_SOURCE",
                                                           "ATA_VERIFY_SLOW_REQUEST_MS", "NO_WAIT")}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", MOCK_MODE="false", DRY_RUN="false",
               PROJECT_ID="proj", REGION="us-west1", NAME_PREFIX="ata", PINNED_INVENTORY_REVISION="",
               APIGEE_HOST="apigee.example")
    env.update(extra)
    return env


def _source_reset(tmp_path, body, statuses="", **extra):
    script = tmp_path / "run.sh"
    script.write_text(f'set -euo pipefail\nsource "{RESET_SCRIPT}"\n{body}\n')
    return subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=_stub_env(tmp_path, statuses, **extra), timeout=60)


# Seconds left in the alert window after the body ran ("W=0" when none is armed).
WINDOW_LEFT = 'w=$((ALERT_WINDOW_UNTIL - $(date +%s))); [ "${w}" -lt 0 ] && w=0; echo "W=${w}"'


def _window_left(res):
    m = re.search(r"^W=(-?\d+)$", res.stdout, re.M)
    assert m, res.stdout + res.stderr
    return int(m.group(1))


def test_fast_demo_request_arms_no_alert_window(tmp_path):
    res = _source_reset(tmp_path, f'demo_request "GET {BASE}/health" "{BASE}/health"\n'
                                  'echo "S=${LAST_HTTP_STATUS} MS=${LAST_HTTP_MS}"\n' + WINDOW_LEFT)
    assert res.returncode == 0, out(res)
    assert "S=200 MS=12" in res.stdout
    assert _window_left(res) == 0
    assert "can open up to" not in out(res)


@pytest.mark.parametrize("statuses, reason", [
    ("/health 200 4.957", f"GET {BASE}/health took 4957 ms (>= 2000 ms, the latency alert threshold; a cold start?)"),
    ("/health 503", f"GET {BASE}/health -> HTTP 503"),
    ("/health fail", f"GET {BASE}/health -> HTTP 000"),
])
def test_slow_5xx_or_failed_demo_request_arms_the_alert_window(tmp_path, statuses, reason):
    res = _source_reset(tmp_path, f'demo_request "GET {BASE}/health" "{BASE}/health"\n' + WINDOW_LEFT,
                        statuses=statuses + "\n")
    assert res.returncode == 0, out(res)
    assert reason in res.stdout
    assert "can open up to 600s later; step 6 keeps watching for alerts until then before archiving" in res.stdout
    assert 570 <= _window_left(res) <= 600


def test_request_without_a_duration_is_not_assumed_fast(tmp_path):
    env = _stub_env(tmp_path)
    (tmp_path / "bin" / "curl").write_text("#!/bin/sh\nprintf 200\n")  # -w without time_total
    script = tmp_path / "run.sh"
    script.write_text(f'source "{RESET_SCRIPT}"\ndemo_request "GET {BASE}/health" "{BASE}/health"\n{WINDOW_LEFT}\n')
    res = subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True, env=env, timeout=60)
    assert "curl reported no request duration ('200')" in res.stdout, out(res)
    assert _window_left(res) > 0


def test_slow_threshold_and_lag_are_configurable(tmp_path):
    res = _source_reset(tmp_path, f'demo_request "GET {BASE}/health" "{BASE}/health"\n' + WINDOW_LEFT,
                        statuses="/health 200 1.500\n", ATA_RESET_SLOW_REQUEST_MS="1000", ATA_RESET_ALERT_LAG="2m")
    assert "took 1500 ms (>= 1000 ms" in res.stdout, out(res)
    assert 90 <= _window_left(res) <= 120


def test_window_survives_the_admin_call_sites_and_keeps_the_latest_deadline(tmp_path):
    """reset_app_faults/warm_up_service run in the main shell, so the window they arm is kept."""
    res = _source_reset(tmp_path, f'reset_app_faults ata-inventory-api "{BASE}"\n'
                                  f'warm_up_service ata-inventory-api "{BASE}"\n' + WINDOW_LEFT,
                        statuses="/admin/fault/reset 200 5.100\n/health 200 0.020\n")
    assert res.returncode == 0, out(res)
    assert f"ata-inventory-api: GET {BASE}/health -> HTTP 200 in 20 ms" in res.stdout
    assert f"POST {BASE}/admin/fault/reset took 5100 ms" in res.stdout
    assert 570 <= _window_left(res) <= 600
    assert (tmp_path / "curl_calls").read_text().splitlines() == [
        f"POST {BASE}/admin/fault/reset", f"GET {BASE}/health"]


@pytest.mark.parametrize("code", ["401", "503"])
def test_warm_up_non_2xx_is_fatal(tmp_path, code):
    res = _source_reset(tmp_path, f'warm_up_service ata-orders-api "{BASE}" || echo "RC=$?"',
                        statuses=f"/health {code}\n")
    assert "RC=1" in res.stdout
    assert f"GET {BASE}/health (ata-orders-api)" in res.stderr


def test_apigee_health_is_timed_and_warns_on_non_200(tmp_path):
    res = _source_reset(tmp_path, "check_apigee_health\n" + WINDOW_LEFT,
                        statuses="/ata/inventory/v1/health 200 0.050\n/ata/orders/v1/health 502 0.100\n")
    assert res.returncode == 0, out(res)
    assert "Apigee inventory health: HTTP 200 in 50 ms" in res.stdout
    assert "Apigee orders health: HTTP 502 in 100 ms" in res.stdout
    assert "Apigee orders /health did not return HTTP 200." in res.stdout
    assert _window_left(res) > 0
    assert (tmp_path / "curl_calls").read_text().splitlines() == [
        "GET https://apigee.example/ata/inventory/v1/health", "GET https://apigee.example/ata/orders/v1/health"]


# --- step 6 waits out the alert window --------------------------------------------------


def test_step6_without_a_window_proceeds_at_once(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15)
    assert res.returncode == 0, out(res)
    assert done_at(res) == 0
    assert "[Alert window]" not in res.stdout


def test_step6_keeps_watching_for_alerts_until_the_window_ends(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, pre="ALERT_WINDOW_UNTIL=1120\n")
    assert res.returncode == 0, out(res)
    assert ("Alert window: watching for alerts from slow demo requests for another 120s "
            "(max timeout counted from its end: 1020s from now).") in res.stdout
    assert "[Alert window] 120s left for alerts from slow demo requests" in res.stdout
    assert done_at(res) == 120


def test_alert_from_a_slow_request_is_waited_for_with_its_group_before_archiving(tmp_path, gcs):
    """The warm-up's own alert opens inside the window: step 6 waits for it to close, then
    for the quiet period and for the incident group to become terminal, then archives."""
    _, root = gcs
    put(root, "incident-late.md", okf("late", IncidentGroupStatus.RESOLVED),
        sequence=["resolved", "resolved", "resolved", "cooling_down", "recovered-claimed", "recovered"])
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, quiet=30, alerts=[0, 0, 0, 1, 1, 0],
                    pre="ALERT_WINDOW_UNTIL=1060\n", after="archive_active_incidents || exit 1")
    assert res.returncode == 0, out(res)
    assert "Still open: 1 alert(s)" in res.stdout
    assert "incident-late.md (cooling_down)" in res.stdout
    assert "incident-late.md (recovered, recovery note not posted yet)" in res.stdout
    # Alert seen at t=45 and 60, closed at t=75; group terminal at t=105, quiet 30s from 75.
    assert done_at(res) == 105
    assert not (root / "proj-ata-incidents" / "knowledge" / "incident-manager" / "incident-late.md").exists()


def test_timeout_counts_from_the_end_of_the_alert_window(tmp_path, gcs):
    """An alert that opens at the end of the window still gets the full --timeout to close."""
    res = run_step6(tmp_path, gcs, fake_clock=True, poll=15, timeout=60, alerts=[0, 0, 1],
                    pre="ALERT_WINDOW_UNTIL=1120\n")
    assert res.returncode == 1
    assert "Timed out waiting for open ATA alerts to close after 60s (180s elapsed)" in out(res)


def test_no_wait_warns_about_an_armed_window(tmp_path, gcs):
    res = run_step6(tmp_path, gcs, pre='NO_WAIT=true\nALERT_WINDOW_UNTIL=$(( $(date +%s) + 300 ))\n')
    assert res.returncode == 0, out(res)
    assert "[--no-wait] Not waiting for alerts from slow demo requests" in res.stdout


# --- wiring ---------------------------------------------------------------------------


def _main_flow():
    """The reset's main flow: everything after the sourced-for-tests guard."""
    text = RESET_SCRIPT.read_text()
    return text.split('if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then', 1)[1]


def test_every_demo_request_runs_before_the_alert_wait():
    main = _main_flow()
    wait = main.index("\nwait_for_open_alerts\n")
    before, after = main[:wait], main[wait:]
    for call in ("reset_app_faults", "warm_up_service \"${NAME_PREFIX}-inventory-api\"",
                 "warm_up_service \"${NAME_PREFIX}-orders-api\"", "check_apigee_health",
                 "verify_admin_endpoints \"${INV_URL}\""):
        assert call in before, call
    for forbidden in ("curl", "demo_request", "admin_request", "warm_up_service", "check_apigee_health",
                      "verify_admin_endpoints", "reset_app_faults"):
        assert forbidden not in after, forbidden
    # Warm-up after the Cloud Run restore (it can move traffic to an idle revision).
    assert before.index("restore_inventory_pinned_baseline") < before.index("warm_up_service")
    assert main.index("validate_alert_window_settings || exit 1") < main.index("verify_pinned_inventory_revision")


@pytest.mark.parametrize("var, value, msg", [
    ("ATA_RESET_SLOW_REQUEST_MS", "abc", "Invalid ATA_RESET_SLOW_REQUEST_MS 'abc'"),
    ("ATA_RESET_SLOW_REQUEST_MS", "0", "Invalid ATA_RESET_SLOW_REQUEST_MS '0'"),
    ("ATA_RESET_ALERT_LAG", "5x", "Invalid --alert-lag / ATA_RESET_ALERT_LAG '5x'"),
    ("ATA_RESET_ALERT_WINDOW_FROM_START", "yes", "Invalid ATA_RESET_ALERT_WINDOW_FROM_START 'yes'"),
])
def test_invalid_alert_window_settings_fail_before_any_step(tmp_path, var, value, msg):
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT), "--mock"], cwd=str(tmp_path), capture_output=True,
                         text=True, env=_stub_env(tmp_path, **{var: value}), timeout=60)
    assert res.returncode == 1
    assert msg in res.stderr
    assert "--> 1." not in res.stdout


def test_alert_window_from_start_is_armed_and_reported(tmp_path):
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT), "--mock", "--alert-window-from-start", "--alert-lag", "5m"],
                         cwd=str(tmp_path), capture_output=True, text=True, env=_stub_env(tmp_path), timeout=60)
    assert res.returncode == 0, out(res)
    assert "--alert-window-from-start: requests made before this reset may have been slow" in res.stdout
    assert "would keep watching for alerts for 300s in step 6 (simulated here)" in res.stdout
    assert "[MOCK/DRY-RUN] Alert window armed (" in res.stdout
    assert "--> 5b. Warming up the demo services" in res.stdout


def test_mock_reset_without_flag_arms_no_window(tmp_path):
    res = subprocess.run([TEST_BASH, str(RESET_SCRIPT), "--mock"], cwd=str(tmp_path), capture_output=True,
                         text=True, env=_stub_env(tmp_path), timeout=60)
    assert res.returncode == 0, out(res)
    assert "No slow, 5xx or failed demo request: no alert window needed." in res.stdout
    assert "--> 9." not in res.stdout


def test_help_documents_alert_window_options():
    res = subprocess.run([str(RESET_SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert "--alert-lag <duration>" in res.stdout and "default: 600s / 10m" in res.stdout
    assert "--alert-window-from-start" in res.stdout


# =======================================================================================
# verify-demo-infra.sh: reports requests that can raise an alert
# =======================================================================================

INV_DIRECT = "ata-inventory-api-test123-uc.a.run.app/health"
ORD_DIRECT = "ata-orders-api-test123-uc.a.run.app/health"
VERIFY_OK = ("/ata/orders/v1/orders/test 401\n/ata/inventory/v1/admin/reset 403\n"
             "/ata/orders/v1/admin/reset 403\n")


def _verify(tmp_path, statuses, **extra):
    return subprocess.run([TEST_BASH, str(VERIFY_SCRIPT)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=_stub_env(tmp_path, statuses, ATA_API_KEY="k", **extra), timeout=60)


def test_verify_all_fast_says_it_cannot_raise_an_alert(tmp_path):
    res = _verify(tmp_path, VERIFY_OK)
    assert res.returncode == 0, out(res)
    assert "All Checks Passed" in res.stdout
    assert "Every request completed in under 2000 ms without HTTP 5xx" in res.stdout
    assert "[WARN]" not in res.stdout
    assert "order placed for item-7" in res.stdout  # the body of GET /items is still read


def test_verify_cold_start_is_reported_with_the_next_step(tmp_path):
    res = _verify(tmp_path, f"{INV_DIRECT} 200 4.957\n" + VERIFY_OK)
    assert res.returncode == 0, out(res)  # a slow 200 is still a passing check
    assert f"- GET https://{INV_DIRECT}: 4957 ms (>= 2000 ms)" in res.stdout
    assert "./scripts/reset-scenarios.sh --alert-window-from-start" in res.stdout
    assert "latency_duration_secs" in res.stdout


def test_verify_5xx_is_reported_and_fails(tmp_path):
    res = _verify(tmp_path, f"{ORD_DIRECT} 503\n" + VERIFY_OK)
    assert res.returncode == 1
    assert f"- GET https://{ORD_DIRECT}: HTTP 503" in res.stdout
    assert "ata-orders-api /health returned 503" in res.stderr


def test_verify_rejects_an_invalid_threshold(tmp_path):
    res = _verify(tmp_path, VERIFY_OK, ATA_VERIFY_SLOW_REQUEST_MS="2s")
    assert res.returncode == 1
    assert "Invalid ATA_VERIFY_SLOW_REQUEST_MS '2s'" in res.stderr


# =======================================================================================
# Agent: a latency report on trivial traffic says so (INC0010327: 1 rpm, cold start)
# =======================================================================================

from agent.app.config import reset_agent_settings  # noqa: E402
from agent.app.contracts.alert import AlertEvent  # noqa: E402
from agent.app.specialists.base import EvidenceDossier  # noqa: E402
from agent.app.specialists.synthesizer import SynthesizerAgent, low_traffic_facts  # noqa: E402

STRAY_BLAST = ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]


def _cr(rpm, total, minutes=17.2, latency=4957.0, service="ata-inventory-api"):
    return EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource=service, findings=[],
        metrics={"latency_p95_ms": latency, "error_rate_5xx": "0.0%", "request_count_rpm": rpm,
                 "request_count_total": total, "request_window_minutes": minutes},
        evidence_items=[], queries_executed={}, anomaly_detected=True, confidence=0.9,
    )


def _alert(policy="ata-inventory-latency"):
    return AlertEvent(incident_id="0.oeb7xdv4q346", policy_name=policy, resource_name="ata-inventory-api",
                      started_at="2026-10-27T10:10:58Z")


@pytest.fixture(autouse=False)
def default_settings(monkeypatch):
    monkeypatch.delenv("LOW_TRAFFIC_RPM", raising=False)
    reset_agent_settings()
    yield
    reset_agent_settings()


def test_low_traffic_facts_only_from_measured_values_below_the_threshold():
    assert low_traffic_facts(_cr(1, 32), 5.0) == {
        "service": "ata-inventory-api", "volume": "32 requests in 17.2 min (1.9 requests/min)"}
    assert low_traffic_facts(_cr(1, 1, minutes=1.0), 5.0)["volume"] == "1 request in 1 min (1.0 requests/min)"
    # The truncated request_count_rpm (0) is not used: the rate comes from total / window.
    assert low_traffic_facts(_cr(0, 3), 5.0)["volume"] == "3 requests in 17.2 min (0.2 requests/min)"
    assert low_traffic_facts(_cr(35, 627, minutes=17.7), 5.0) is None  # S2's origin
    assert low_traffic_facts(_cr(5, 86, minutes=17.2), 5.0) is None  # exactly 5.0/min: not low
    for missing in ({"request_count_total": "N/A (not captured)"}, {"request_window_minutes": None},
                    {"request_window_minutes": 0}):
        d = _cr(1, 32)
        d.metrics.update(missing)
        assert low_traffic_facts(d, 5.0) is None, missing


def test_stray_latency_problem_states_low_traffic_not_service_wide_degradation(default_settings):
    problem = SynthesizerAgent()._synthesize_problem(_alert(), [_cr(1, 32)], "ata-inventory-api", STRAY_BLAST)
    assert problem == (
        "Elevated latency (4957ms) on ata-inventory-api since 2026-10-27T10:10:58Z under low traffic: "
        "32 requests in 17.2 min (1.9 requests/min) on ata-inventory-api, so the latency may come from a few slow "
        "requests (such as a cold start) rather than a service-wide degradation"
    )
    assert ";" not in problem


def test_stray_latency_root_cause_and_observation_state_low_traffic(default_settings):
    synth = SynthesizerAgent()
    text, _ = synth._determine_root_cause([_cr(1, 32)], "ata-inventory-api", _alert(), blast_radius=STRAY_BLAST)
    assert text.startswith("Elevated latency on `ata-inventory-api` (peak observed latency: 4957ms) under low traffic: "
                           "32 requests in 17.2 min (1.9 requests/min)")
    assert "Service degradation" not in text
    obs = synth._synthesize_observations([_cr(1, 32)], _alert())
    assert "Low traffic on ata-inventory-api: 32 requests in 17.2 min (1.9 requests/min) in the investigation window" in obs


def test_upstream_origin_root_cause_states_low_traffic_like_the_problem(default_settings):
    """Alert on orders-api, origin inventory-api (upstream): both texts use the origin's volume."""
    synth = SynthesizerAgent()
    alert = AlertEvent(incident_id="i-1", policy_name="ata-orders-latency", resource_name="ata-orders-api",
                       started_at="2026-10-27T10:10:58Z")
    dossiers = [_cr(1, 32), _cr(40, 700, service="ata-orders-api")]
    problem = synth._synthesize_problem(alert, dossiers, "ata-inventory-api", STRAY_BLAST)
    text, _ = synth._determine_root_cause(dossiers, "ata-inventory-api", alert, blast_radius=STRAY_BLAST)
    assert "under low traffic: 32 requests in 17.2 min (1.9 requests/min) on ata-inventory-api" in problem
    assert "under low traffic: 32 requests in 17.2 min (1.9 requests/min) on ata-inventory-api" in text
    assert "cascading" not in text and text.endswith("on caller `ata-orders-api`.")


def test_root_cause_never_states_another_services_volume_for_the_origin(default_settings):
    """No Cloud Run dossier for the origin: no low-traffic claim from a fallback dossier."""
    synth = SynthesizerAgent()
    text, _ = synth._determine_root_cause([_cr(1, 32, service="ata-orders-api")], "ata-inventory-api", _alert(),
                                          blast_radius=STRAY_BLAST)
    assert "low traffic" not in text


def test_s2_traffic_keeps_the_degradation_wording(default_settings):
    synth = SynthesizerAgent()
    problem = synth._synthesize_problem(_alert(), [_cr(35, 627, minutes=17.7, latency=6546.0)], "ata-inventory-api", STRAY_BLAST)
    assert problem.startswith("Elevated service latency and request timeouts (6546ms) on ata-inventory-api impacting")
    text, _ = synth._determine_root_cause([_cr(35, 627, minutes=17.7, latency=6546.0)], "ata-inventory-api", _alert(),
                                          blast_radius=STRAY_BLAST)
    assert text.startswith("Service degradation on `ata-inventory-api`")
    assert "low traffic" not in problem.lower() + text.lower()
    assert "Low traffic" not in synth._synthesize_observations([_cr(35, 627, minutes=17.7)], _alert())


def test_low_traffic_is_not_stated_for_5xx_alerts(default_settings):
    synth = SynthesizerAgent()
    obs = synth._synthesize_observations([_cr(1, 32)], _alert("ata-inventory-5xx"))
    assert "Low traffic" not in obs


def test_low_traffic_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("LOW_TRAFFIC_RPM", "40")
    reset_agent_settings()
    try:
        problem = SynthesizerAgent()._synthesize_problem(_alert(), [_cr(35, 627, minutes=17.7, latency=6546.0)],
                                                         "ata-inventory-api", STRAY_BLAST)
        assert "under low traffic: 627 requests in 17.7 min (35.4 requests/min)" in problem
    finally:
        monkeypatch.delenv("LOW_TRAFFIC_RPM")
        reset_agent_settings()


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_low_traffic_threshold_is_validated(monkeypatch, value):
    from pydantic import ValidationError

    from agent.app.config import AgentConfig

    monkeypatch.setenv("LOW_TRAFFIC_RPM", value)
    with pytest.raises(ValidationError):
        AgentConfig()


@pytest.mark.asyncio
async def test_cloud_run_specialist_records_the_request_total_and_window():
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    from agent.app.config import AgentConfig
    from agent.app.specialists.cloud_run import CloudRunSpecialist

    def series(code, counts):
        ts = MagicMock()
        ts.metric.labels = {"response_code": code, "response_code_class": code[0] + "xx"}
        pts = []
        for c in counts:
            pt = MagicMock()
            pt.value.int64_value = c
            pts.append(pt)
        ts.points = pts
        return ts

    alert = AlertEvent(incident_id="0.oeb7xdv4q346", policy_name="ata-inventory-latency",
                       resource_name="ata-inventory-api", started_at="2026-10-27T10:10:58Z")
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    fixed_now = datetime(2026, 10, 27, 10, 13, 10, tzinfo=timezone.utc)
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as metric_cls, \
         patch("agent.app.specialists.cloud_run.datetime") as dt:
        dt.now.return_value = fixed_now
        dt.fromisoformat.side_effect = datetime.fromisoformat
        log_cls.return_value.list_entries.return_value = []
        metric_cls.return_value.list_time_series.side_effect = [[series("200", [12, 1, 8])], []]
        dossier = await CloudRunSpecialist().investigate(alert, config)
    # Window: alert start -15 min .. now (10:13:10) = 17.2 min; 21 requests -> 1 rpm.
    assert dossier.metrics["request_count_total"] == 21
    assert dossier.metrics["request_window_minutes"] == 17.2
    assert dossier.metrics["request_count_rpm"] == 1
