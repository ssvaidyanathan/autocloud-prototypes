"""reset-scenarios.sh alert wait: CA bundle resolution and failing loudly on query errors.

The Monitoring query lives in scripts/scenarios/open_alerts.py. It is tested here with
a stubbed urlopen. The bash poll loop is tested by sourcing reset-scenarios.sh
(sourcing defines its functions without running the reset) with the query stubbed.
"""

import importlib.util
import io
import json
import os
import ssl
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
RESET_SCRIPT = SCRIPTS_DIR / "reset-scenarios.sh"
OPEN_ALERTS_PY = SCRIPTS_DIR / "scenarios" / "open_alerts.py"
HINT = "export SSL_CERT_FILE=/etc/ssl/cert.pem (macOS)"

_spec = importlib.util.spec_from_file_location("open_alerts", OPEN_ALERTS_PY)
open_alerts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(open_alerts)


@pytest.fixture
def no_ca_env(monkeypatch):
    """No CA env vars and no certifi, so resolution falls through to the platform."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi -> ImportError


# --- CA bundle resolution ------------------------------------------------------


def test_ssl_cert_file_wins_and_is_stripped(no_ca_env, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "  /custom/ca.pem \n")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/other/ca.pem")
    assert open_alerts.resolve_ca_bundle() == "/custom/ca.pem"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_ssl_cert_file_falls_through_to_requests_ca_bundle(no_ca_env, monkeypatch, blank):
    monkeypatch.setenv("SSL_CERT_FILE", blank)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/req/ca.pem")
    assert open_alerts.resolve_ca_bundle() == "/req/ca.pem"


def test_macos_falls_back_to_system_cert_pem(no_ca_env, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "  ")
    monkeypatch.setattr(open_alerts.sys, "platform", "darwin")
    monkeypatch.setattr(open_alerts.os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")
    assert open_alerts.resolve_ca_bundle() == "/etc/ssl/cert.pem"


def test_nothing_resolves_on_linux_uses_default_context(no_ca_env, monkeypatch):
    monkeypatch.setattr(open_alerts.sys, "platform", "linux")
    captured = {}
    monkeypatch.setattr(open_alerts.ssl, "create_default_context",
                        lambda cafile=None: captured.setdefault("cafile", cafile) or "ctx")
    assert open_alerts.resolve_ca_bundle() is None
    open_alerts.create_ssl_context()
    assert captured == {"cafile": None}


def test_certifi_used_when_no_env(no_ca_env, monkeypatch):
    fake = type(sys)("certifi")
    fake.where = lambda: "/certifi/cacert.pem"
    monkeypatch.setitem(sys.modules, "certifi", fake)
    assert open_alerts.resolve_ca_bundle() == "/certifi/cacert.pem"


def test_resolved_bundle_builds_the_ssl_context(no_ca_env, monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    captured = {}
    monkeypatch.setattr(open_alerts.ssl, "create_default_context",
                        lambda cafile=None: captured.setdefault("cafile", cafile))
    open_alerts.create_ssl_context()
    assert captured["cafile"] == str(bundle)


def test_missing_bundle_file_is_a_loud_error(no_ca_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nope.pem"))
    with pytest.raises(open_alerts.AlertsQueryError, match="CA bundle file not found"):
        open_alerts.create_ssl_context()


# --- Query with a stubbed urlopen ----------------------------------------------


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_query_passes_ssl_context_and_follows_pages(no_ca_env, monkeypatch):
    monkeypatch.setattr(open_alerts, "create_ssl_context", lambda: "CTX")
    pages = [
        {"alerts": [{"name": "projects/p/alerts/0.a", "policy": {"displayName": "ata-x"}}], "nextPageToken": "t/2"},
        {"alerts": [{"name": "projects/p/alerts/0.b", "policy": {"displayName": "other"}}]},
    ]
    seen = []

    def urlopen(req, timeout, context):
        seen.append((req.full_url, context, req.get_header("Authorization")))
        return FakeResp(json.dumps(pages[len(seen) - 1]).encode())

    alerts = open_alerts.fetch_open_alerts("proj", "tok", urlopen=urlopen)

    assert [a["name"] for a in alerts] == ["projects/p/alerts/0.a", "projects/p/alerts/0.b"]
    assert all(ctx == "CTX" and auth == "Bearer tok" for _, ctx, auth in seen)
    assert "filter=state%3D%22OPEN%22" in seen[0][0]
    assert "pageToken=t%2F2" in seen[1][0]


def test_main_prints_only_ata_alerts(no_ca_env, monkeypatch, capsys):
    monkeypatch.setattr(open_alerts, "create_ssl_context", lambda: None)
    page = {"alerts": [
        {"name": "projects/p/alerts/0.a", "policy": {"displayName": "ata-orders-5xx"}},
        {"name": "projects/p/alerts/0.f", "policy": {"displayName": "IMA Alert: foreign"}},
    ]}
    rc = open_alerts.main(["proj", "tok", "0", "3600", ""],
                          urlopen=lambda req, timeout, context: FakeResp(json.dumps(page).encode()))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [a["id"] for a in out["active"]] == ["0.a"]


def http_error(code, reason, body=None):
    fp = io.BytesIO(body.encode()) if body is not None else None
    return urllib.error.HTTPError("u", code, reason, {}, fp)


# OAuth-style error bodies carry "error" as a string, not a Google API error object.
OAUTH_401_BODY = json.dumps({"error": "invalid_token", "error_description": "Invalid Credentials"})
STRING_503_BODY = json.dumps({"error": "backend"})


@pytest.mark.parametrize(
    "exc, cause, rc, hint",
    [
        (urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")),
         "TLS certificate verification failed", open_alerts.QUERY_REJECTED_EXIT, True),
        (http_error(401, "Unauthorized"),
         "Cloud Monitoring rejected your access token from token source 'adc' (401)."
         " Run: gcloud auth application-default login",
         open_alerts.QUERY_REJECTED_EXIT, False),
        (http_error(403, "Forbidden"),
         "permission denied (403), needs roles/monitoring.viewer on proj", open_alerts.QUERY_REJECTED_EXIT, False),
        (http_error(404, "Not Found"), "HTTP 404 Not Found", open_alerts.QUERY_REJECTED_EXIT, False),
        (http_error(429, "Too Many Requests"), "HTTP 429", open_alerts.QUERY_FAILED_EXIT, False),
        (http_error(503, "Service Unavailable"), "HTTP 503", open_alerts.QUERY_FAILED_EXIT, False),
        (http_error(401, "Unauthorized", OAUTH_401_BODY),
         "Cloud Monitoring rejected your access token from token source 'adc' (401)."
         " Run: gcloud auth application-default login",
         open_alerts.QUERY_REJECTED_EXIT, False),
        (http_error(503, "Service Unavailable", STRING_503_BODY), "HTTP 503", open_alerts.QUERY_FAILED_EXIT, False),
        (urllib.error.URLError("timed out"), "Alerts API query failed", open_alerts.QUERY_FAILED_EXIT, True),
        (TimeoutError("The read operation timed out"), "Alerts API query failed", open_alerts.QUERY_FAILED_EXIT, True),
    ],
)
def test_main_fails_loudly_and_never_reports_zero_alerts(no_ca_env, monkeypatch, capsys, exc, cause, rc, hint):
    """401/403/4xx/TLS are not retryable (exit 4); 429/5xx/timeouts are transient (exit 3)."""
    monkeypatch.delenv("ATA_MONITORING_TOKEN_SOURCE", raising=False)
    monkeypatch.setattr(open_alerts, "create_ssl_context", lambda: None)

    def urlopen(req, timeout, context):
        raise exc

    assert open_alerts.main(["proj", "tok"], urlopen=urlopen) == rc

    out, err = capsys.readouterr()
    assert out == ""
    assert cause in err
    assert (HINT in err) is hint


UNSUPPORTED_BODY = json.dumps({"error": {
    "code": 401, "status": "UNAUTHENTICATED",
    "message": "Request had invalid authentication credentials.",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_TYPE_UNSUPPORTED",
                 "metadata": {"service": "monitoring.googleapis.com",
                              "method": "google.monitoring.v3.AlertService.ListAlerts"}}],
}})


def run_main_with_http_error(monkeypatch, capsys, code, reason, body, source):
    monkeypatch.setenv("ATA_MONITORING_TOKEN_SOURCE", source)
    monkeypatch.setattr(open_alerts, "create_ssl_context", lambda: None)

    def urlopen(req, timeout, context):
        raise urllib.error.HTTPError("u", code, reason, {}, io.BytesIO(body.encode()))

    rc = open_alerts.main(["proj", "tok"], urlopen=urlopen)
    out, err = capsys.readouterr()
    assert out == ""
    return rc, err


@pytest.mark.parametrize(
    "source, fix",
    [
        ("gcloud", "gcloud auth application-default login, then ATA_MONITORING_TOKEN_SOURCE=adc"),
        ("adc", "--monitoring-token-source gcloud"),
    ],
)
def test_401_access_token_type_unsupported_is_explained(monkeypatch, capsys, source, fix):
    rc, err = run_main_with_http_error(monkeypatch, capsys, 401, "Unauthorized", UNSUPPORTED_BODY, source)

    assert rc == open_alerts.QUERY_REJECTED_EXIT
    assert "401 ACCESS_TOKEN_TYPE_UNSUPPORTED" in err
    assert f"token source '{source}'" in err
    assert fix in err
    assert "--no-wait" in err
    assert HINT not in err


def test_401_unsupported_detected_in_non_json_body(monkeypatch, capsys):
    rc, err = run_main_with_http_error(monkeypatch, capsys, 401, "Unauthorized",
                                       "reason: ACCESS_TOKEN_TYPE_UNSUPPORTED", "gcloud")
    assert rc == open_alerts.QUERY_REJECTED_EXIT
    assert "ACCESS_TOKEN_TYPE_UNSUPPORTED" in err


@pytest.mark.parametrize("source, login", [("adc", "gcloud auth application-default login"),
                                           ("gcloud", "gcloud auth login")])
def test_plain_401_names_the_login_for_the_source(monkeypatch, capsys, source, login):
    body = json.dumps({"error": {"code": 401, "message": "Token expired"}})
    rc, err = run_main_with_http_error(monkeypatch, capsys, 401, "Unauthorized", body, source)
    assert rc == open_alerts.QUERY_REJECTED_EXIT
    assert f"token source '{source}' (401). Run: {login}" in err
    assert "Token expired" in err


def test_403_includes_role_project_and_api_message(monkeypatch, capsys):
    body = json.dumps({"error": {"code": 403, "message": "Permission monitoring.alerts.list denied"}})
    rc, err = run_main_with_http_error(monkeypatch, capsys, 403, "Forbidden", body, "adc")
    assert rc == open_alerts.QUERY_REJECTED_EXIT
    assert "needs roles/monitoring.viewer on proj" in err
    assert "Permission monitoring.alerts.list denied" in err


def test_main_rejects_empty_token(capsys):
    assert open_alerts.main(["proj", ""]) == open_alerts.QUERY_REJECTED_EXIT
    assert "empty access token" in capsys.readouterr().err


def test_cli_reports_missing_ca_bundle_without_network(tmp_path):
    env = dict(os.environ, SSL_CERT_FILE=str(tmp_path / "missing.pem"))
    res = subprocess.run([sys.executable, str(OPEN_ALERTS_PY), "proj", "tok"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == open_alerts.QUERY_REJECTED_EXIT
    assert res.stdout == ""
    assert "CA bundle file not found" in res.stderr
    assert HINT in res.stderr


# --- reset-scenarios.sh poll loop ----------------------------------------------


def run_wait(tmp_path, body, extra_env=None, stub_token=True):
    """Source reset-scenarios.sh in live mode, apply stubs in body, call wait_for_open_alerts."""
    script = tmp_path / "wait.sh"
    token_stub = "resolve_monitoring_token() { echo tok; }\n" if stub_token else ""
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'source "{RESET_SCRIPT}"\n'
        "POLL_INTERVAL_SECS=0\nQUIET_PERIOD_SECS=0\n"
        "get_serving_revisions() { echo ''; }\n"
        # The incident group state check is tested in test_reset_terminal_groups.py.
        "non_terminal_incident_groups() { :; }\n"
        f"{token_stub}"
        f"{body}\n"
        "wait_for_open_alerts\n"
        "echo WAIT_RETURNED\n"
    )
    script.chmod(0o755)
    env = os.environ.copy()
    env.update(MOCK_MODE="false", DRY_RUN="false", PROJECT_ID="proj")
    for var in ("ATA_RESET_NO_WAIT", "NO_WAIT", "ATA_MONITORING_TOKEN_SOURCE"):
        env.pop(var, None)
    env.update(extra_env or {})
    return subprocess.run([str(script)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=env, timeout=60)


def failing_query(message, rc, hint=True):
    hint_line = f'echo "Hint: if this is a CA certificate problem, {HINT}" >&2; ' if hint else ""
    return (f'query_open_ata_alerts() {{ echo x >> "$PWD/calls"; '
            f'echo "Cloud Monitoring query failed: {message}" >&2; {hint_line}return {rc}; }}')


FAILING_QUERY = failing_query("Cloud Monitoring Alerts API query failed: timed out", 3)


def calls(tmp_path):
    return (tmp_path / "calls").read_text().count("x")


def test_reset_wait_retries_transient_failures_then_exits_nonzero_with_cause_and_hint(tmp_path):
    res = run_wait(tmp_path, FAILING_QUERY)

    assert res.returncode == 1, res.stdout + res.stderr
    assert "WAIT_RETURNED" not in res.stdout
    assert calls(tmp_path) == 3
    assert "attempt 1/3" in res.stdout and "attempt 2/3" in res.stdout
    assert "failed 3 time(s) in a row" in res.stderr
    assert "timed out" in res.stderr
    assert HINT in res.stderr
    assert "Zero open ATA alerts" not in res.stdout


@pytest.mark.parametrize(
    "message, hint",
    [
        ("Cloud Monitoring rejected your gcloud access token (401). Run: gcloud auth login", False),
        ("Cloud Monitoring permission denied (403), needs roles/monitoring.viewer on proj", False),
        ("TLS certificate verification failed: CERTIFICATE_VERIFY_FAILED", True),
    ],
)
def test_reset_wait_does_not_retry_non_transient_failures(tmp_path, message, hint):
    res = run_wait(tmp_path, failing_query(message, 4, hint=hint))

    assert res.returncode == 1, res.stdout + res.stderr
    assert calls(tmp_path) == 1
    assert "attempt 1/3" not in res.stdout
    assert "not retryable" in res.stderr
    assert message in res.stderr
    assert (HINT in res.stderr) is hint
    assert "WAIT_RETURNED" not in res.stdout


def test_reset_wait_recovers_from_a_transient_failure(tmp_path):
    body = (
        'query_open_ata_alerts() { echo x >> "$PWD/calls";'
        ' if [ "$(wc -l < "$PWD/calls")" -lt 2 ]; then echo "Cloud Monitoring query failed: timed out" >&2; return 3; fi;'
        ' echo \'{"active": [], "ignored": []}\'; }'
    )
    res = run_wait(tmp_path, body)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "attempt 1/3" in res.stdout
    assert "Zero open ATA alerts" in res.stdout
    assert "WAIT_RETURNED" in res.stdout


def test_reset_wait_fails_loudly_without_a_token(tmp_path):
    res = run_wait(tmp_path, "resolve_monitoring_token() { echo ''; }")

    assert res.returncode == 1
    assert "could not get an access token" in res.stderr
    assert "gcloud auth application-default login" in res.stderr
    assert "attempt 1/3" not in res.stdout


# --- Token source (ADC default, gcloud opt-in, no fallback) ---------------------

FAKE_GCLOUD = """#!/usr/bin/env bash
echo "$*" >> "$PWD/gcloud_calls"
case "$*" in
  "auth application-default print-access-token")
    [ -n "${FAKE_ADC_FAIL:-}" ] && { echo "ERROR: (gcloud.auth.application-default.print-access-token) Your default credentials were not found." >&2; exit 1; }
    echo adc-token ;;
  "auth print-access-token") echo gcloud-token ;;
  *) echo "fake gcloud: unexpected $*" >&2; exit 1 ;;
esac
"""

RECORD_TOKEN_QUERY = (
    'query_open_ata_alerts() { echo "$2" > "$PWD/token_used"; echo \'{"active": [], "ignored": []}\'; }'
)


def with_fake_gcloud(tmp_path, **env):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", **env}


@pytest.mark.parametrize(
    "env, token, command",
    [
        ({}, "adc-token", "auth application-default print-access-token"),
        ({"ATA_MONITORING_TOKEN_SOURCE": "adc"}, "adc-token", "auth application-default print-access-token"),
        ({"ATA_MONITORING_TOKEN_SOURCE": "gcloud"}, "gcloud-token", "auth print-access-token"),
        ({"ATA_TOKEN_SOURCE": "adc"}, "adc-token", "auth application-default print-access-token"),
        ({"ATA_TOKEN_SOURCE": "gcloud"}, "gcloud-token", "auth print-access-token"),
    ],
)
def test_token_source_selects_the_gcloud_command(tmp_path, env, token, command):
    res = run_wait(tmp_path, RECORD_TOKEN_QUERY, extra_env=with_fake_gcloud(tmp_path, **env), stub_token=False)

    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "token_used").read_text().strip() == token
    assert (tmp_path / "gcloud_calls").read_text().splitlines() == [command]


def test_adc_failure_is_loud_and_does_not_fall_back_to_gcloud(tmp_path):
    res = run_wait(tmp_path, RECORD_TOKEN_QUERY, extra_env=with_fake_gcloud(tmp_path, FAKE_ADC_FAIL="1"),
                   stub_token=False)

    assert res.returncode == 1, res.stdout + res.stderr
    assert (tmp_path / "gcloud_calls").read_text().splitlines() == ["auth application-default print-access-token"]
    assert not (tmp_path / "token_used").exists()
    assert "token source 'adc'" in res.stderr
    assert "default credentials were not found" in res.stderr
    assert "Run: gcloud auth application-default login" in res.stderr
    assert "--no-wait" in res.stderr
    assert "attempt 1/3" not in res.stdout


@pytest.mark.parametrize("argv, env", [
    (["--monitoring-token-source", "bogus"], {}),
    (["--monitoring-token-source=bogus"], {}),
    ([], {"ATA_MONITORING_TOKEN_SOURCE": "bogus"}),
])
def test_invalid_token_source_fails_before_any_reset_step(tmp_path, argv, env):
    res = subprocess.run([str(RESET_SCRIPT), "--mock", *argv], capture_output=True, text=True,
                         env={**os.environ, **env}, timeout=30)

    assert res.returncode == 1
    assert "Invalid ATA_MONITORING_TOKEN_SOURCE 'bogus'" in res.stderr
    assert "Starting Scenario Fault Cleanup" not in res.stdout


@pytest.mark.parametrize("argv, env", [
    (["--token-source", "bogus"], {}),
    (["--token-source=bogus"], {}),
    ([], {"ATA_TOKEN_SOURCE": "bogus"}),
])
def test_invalid_general_token_source_fails_before_any_reset_step(tmp_path, argv, env):
    res = subprocess.run([str(RESET_SCRIPT), "--mock", *argv], capture_output=True, text=True,
                         env={**os.environ, **env}, timeout=30)

    assert res.returncode == 1
    assert "Invalid ATA_TOKEN_SOURCE 'bogus'" in res.stderr
    assert "Starting Scenario Fault Cleanup" not in res.stdout


def test_token_source_flag_overrides_env(tmp_path):
    script = tmp_path / "src.sh"
    script.write_text(f'#!/usr/bin/env bash\nsource "{RESET_SCRIPT}" --monitoring-token-source gcloud\n'
                      'echo "SRC=${ATA_MONITORING_TOKEN_SOURCE}"\n')
    script.chmod(0o755)
    res = subprocess.run([str(script)], capture_output=True, text=True,
                         env={**os.environ, "ATA_MONITORING_TOKEN_SOURCE": "adc"}, timeout=30)
    assert "SRC=gcloud" in res.stdout


def test_general_token_source_flag_overrides_env(tmp_path):
    script = tmp_path / "src.sh"
    script.write_text(f'#!/usr/bin/env bash\nsource "{RESET_SCRIPT}" --token-source gcloud\n'
                      'echo "SRC=${ATA_TOKEN_SOURCE}"\n')
    script.chmod(0o755)
    res = subprocess.run([str(script)], capture_output=True, text=True,
                         env={**os.environ, "ATA_TOKEN_SOURCE": "adc"}, timeout=30)
    assert "SRC=gcloud" in res.stdout


COMMON_SH = SCRIPTS_DIR / "scenarios" / "common.sh"


VALID_PIN = """PIN_INVENTORY_REVISION=ata-inventory-api-pin-001
PIN_INVENTORY_DIGEST=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
PIN_INVENTORY_PREVIOUS_REVISION=
PIN_INVENTORY_PREVIOUS_DIGEST=
"""


def test_resolve_apigee_token_defaults_to_adc(tmp_path):
    pin = tmp_path / "pin.env"
    pin.write_text(VALID_PIN)
    cmd = f'source "{COMMON_SH}"; resolve_apigee_token'
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path,
                         env=with_fake_gcloud(tmp_path, PROJECT_ID="proj-x", REGION="region-x", NAME_PREFIX="ata", ATA_INVENTORY_PIN_FILE=str(pin), MOCK_MODE="false", DRY_RUN="false"))
    assert res.returncode == 0
    assert res.stdout.strip() == "adc-token"
    assert (tmp_path / "gcloud_calls").read_text().splitlines() == ["auth application-default print-access-token"]


def test_resolve_apigee_token_uses_token_source_override(tmp_path):
    pin = tmp_path / "pin.env"
    pin.write_text(VALID_PIN)
    cmd = f'source "{COMMON_SH}"; resolve_apigee_token'
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path,
                         env=with_fake_gcloud(tmp_path, PROJECT_ID="proj-x", REGION="region-x", NAME_PREFIX="ata", ATA_INVENTORY_PIN_FILE=str(pin), ATA_TOKEN_SOURCE="gcloud", MOCK_MODE="false", DRY_RUN="false"))
    assert res.returncode == 0
    assert res.stdout.strip() == "gcloud-token"
    assert (tmp_path / "gcloud_calls").read_text().splitlines() == ["auth print-access-token"]


def test_resolve_apigee_token_fails_loudly_on_adc_error(tmp_path):
    pin = tmp_path / "pin.env"
    pin.write_text(VALID_PIN)
    cmd = f'source "{COMMON_SH}"; resolve_apigee_token'
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path,
                         env=with_fake_gcloud(tmp_path, PROJECT_ID="proj-x", REGION="region-x", NAME_PREFIX="ata", ATA_INVENTORY_PIN_FILE=str(pin), FAKE_ADC_FAIL="1", MOCK_MODE="false", DRY_RUN="false"))
    assert res.returncode == 1
    assert "gcloud auth application-default login" in res.stderr
    assert "Your default credentials were not found" in res.stderr


def test_resolve_id_token_warns_on_cloudsdk_auth_access_token(tmp_path):
    stub = """#!/usr/bin/env bash
echo "print-identity-token cannot be used with an access token" >&2
exit 1
"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(stub)
    (bindir / "gcloud").chmod(0o755)
    env = {**os.environ, "PROJECT_ID": "proj-x", "REGION": "region-x", "NAME_PREFIX": "ata", "INVENTORY_PIN_OPTIONAL": "true", "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "CLOUDSDK_AUTH_ACCESS_TOKEN": "some-token", "MOCK_MODE": "false", "DRY_RUN": "false"}
    cmd = f'source "{COMMON_SH}"; resolve_id_token'
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert "CLOUDSDK_AUTH_ACCESS_TOKEN is set in the environment" in res.stderr
    assert "unset CLOUDSDK_AUTH_ACCESS_TOKEN" in res.stderr
    assert "print-identity-token cannot be used with an access token" in res.stderr


def test_token_resolution_ignores_gcloud_stderr_warnings(tmp_path):
    """Stderr warnings during gcloud token resolution must never pollute the resolved token."""
    gcloud_stub = """#!/usr/bin/env bash
echo "WARNING: Quota project 'test-proj' does not match default" >&2
case "$*" in
  *"application-default"*) echo "clean-adc-token" ;;
  *) echo "clean-gcloud-token" ;;
esac
exit 0
"""
    curl_stub = """#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$prev" = "--config" ]; then
        grep 'header = "Authorization: Bearer ' "$arg" | sed 's/^header = "Authorization: Bearer //; s/"$//' >> "$CAPTURED_TOKEN_FILE"
    fi
    prev="$arg"
done
exit 1
"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(gcloud_stub)
    (bindir / "gcloud").chmod(0o755)
    (bindir / "curl").write_text(curl_stub)
    (bindir / "curl").chmod(0o755)

    captured_file = tmp_path / "captured_token.txt"
    pin = tmp_path / "pin.env"
    pin.write_text(VALID_PIN)
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()

    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "CAPTURED_TOKEN_FILE": str(captured_file),
        "PROJECT_ID": "proj-x",
        "REGION": "region-x",
        "NAME_PREFIX": "ata",
        "APIGEE_ORG": "proj-x",
        "APIGEE_ENV": "env-x",
        "APIGEE_HOST": "host.example.com",
        "ATA_STACK_DIR": str(stack_dir),
        "ATA_INVENTORY_PIN_FILE": str(pin),
        "MOCK_MODE": "false",
        "DRY_RUN": "false",
    }

    # 1. resolve_access_token (defaults to adc, captures clean token, ignores stderr warning)
    cmd = f'source "{COMMON_SH}"; resolve_access_token'
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert res.returncode == 0
    assert res.stdout.strip() == "clean-adc-token"
    assert "WARNING" not in res.stdout
    assert "WARNING" not in res.stderr

    # resolve_access_token with gcloud source override
    cmd_gcloud = f'source "{COMMON_SH}"; resolve_access_token gcloud'
    res_gcloud = subprocess.run(["bash", "-c", cmd_gcloud], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert res_gcloud.returncode == 0
    assert res_gcloud.stdout.strip() == "clean-gcloud-token"
    assert "WARNING" not in res_gcloud.stdout
    assert "WARNING" not in res_gcloud.stderr

    # 2. deploy-apigee-proxies.sh
    if captured_file.exists():
        captured_file.unlink()
    res_deploy = subprocess.run(["bash", str(SCRIPTS_DIR / "deploy-apigee-proxies.sh")],
                                capture_output=True, text=True, cwd=tmp_path, env=env)
    assert captured_file.exists(), f"curl was not called: {res_deploy.stdout} {res_deploy.stderr}"
    assert captured_file.read_text().splitlines()[0] == "clean-adc-token"

    # 3. teardown-apigee.sh
    if captured_file.exists():
        captured_file.unlink()
    res_teardown = subprocess.run(["bash", str(SCRIPTS_DIR / "teardown-apigee.sh")],
                                  capture_output=True, text=True, cwd=tmp_path, env=env)
    assert captured_file.exists(), f"curl was not called: {res_teardown.stdout} {res_teardown.stderr}"
    assert captured_file.read_text().splitlines()[0] == "clean-adc-token"


def test_reset_wait_with_real_query_fails_on_missing_ca_bundle(tmp_path):
    """The real open_alerts.py, failing before any network call, makes the reset fail."""
    res = run_wait(tmp_path, "", extra_env={"SSL_CERT_FILE": str(tmp_path / "missing.pem")})

    assert res.returncode == 1, res.stdout + res.stderr
    assert "CA bundle file not found" in res.stderr
    assert HINT in res.stderr
    assert "attempt 1/3" not in res.stdout


def test_reset_no_wait_skips_the_query_entirely(tmp_path):
    res = run_wait(tmp_path, FAILING_QUERY, extra_env={"ATA_RESET_NO_WAIT": "true"})

    assert res.returncode == 0, res.stdout + res.stderr
    assert "[--no-wait]" in res.stdout
    assert not (tmp_path / "calls").exists()


def test_reset_help_states_180s_default_quiet_period():
    res = subprocess.run([str(RESET_SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert "(default: 180s / 3m)" in res.stdout
    assert "(default: adc; env ATA_MONITORING_TOKEN_SOURCE)" in res.stdout


def test_reset_default_quiet_period_is_180s(tmp_path):
    script = tmp_path / "q.sh"
    script.write_text(f'#!/usr/bin/env bash\nsource "{RESET_SCRIPT}"\necho "QUIET=${{QUIET_PERIOD_SECS}}"\n')
    script.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("ATA_RESET_QUIET_PERIOD", "QUIET_PERIOD")}
    res = subprocess.run([str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert "QUIET=180" in res.stdout
