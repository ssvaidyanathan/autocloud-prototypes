"""A real terminal Ctrl-C during the traffic hold stops traffic gracefully (S1-S5).

DEMO.md: "Press Ctrl-C at any time to safely stop traffic early and view next steps".

QA round 3, S4: a real ^C in a terminal, mid-traffic, printed the "interrupted by user" warning
and then "[ERROR] S4 aborted (exit 130) ...", exited 130 without NEXT STEPS and released the
leak. A terminal ^C sends SIGINT to the whole foreground process group: the in-flight curl
dies, and so does the bash subshell of `STATUS=$(curl ... || echo "000")`, which does not have
the script's INT trap. The failed assignment tripped set -e. The 17e18a3 tests stopped only in
stubbed `sleep`/`date` children, which were guarded.

These tests run each scenario on a real pseudo-terminal and type ^C into it, so the kernel
delivers SIGINT to the foreground process group. The traffic curls are the real curl, talking
to a local HTTPS server (APIGEE_HOST) that can hold the order requests open so that the ^C
lands while curl is in flight. The other curl calls (Cloud Run admin endpoints, the Apigee
management API) and gcloud are stubbed and logged. Scripts run under ATA_TEST_BASH (default
`bash`; set it to a bash 3.2 binary to check macOS compatibility).
"""

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_s5_bad_deploy import BAD_USER, PINNED_REVISION, STACK_ENV, Env, baseline_state
from tests.test_scenario_abort_cleanup import CURL as STUB_CURL
from tests.test_scenario_abort_cleanup import GCLOUD, REDEPLOY_168

SCENARIOS_DIR = Path(__file__).parent.parent / "scripts" / "scenarios"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
REAL_CURL = shutil.which("curl")
REAL_PYTHON = shutil.which("python3")
OPENSSL = shutil.which("openssl")

pytestmark = pytest.mark.skipif(not (REAL_CURL and OPENSSL), reason="needs curl and openssl")

# Requests to the local server (127.0.0.1) run the real curl; everything else is the stub
# from test_scenario_abort_cleanup. Every call is logged.
CURL = f"""#!/bin/sh
case "$*" in
  *127.0.0.1*) echo "curl $*" >> "$ATA_TEST_CALLS"; exec {REAL_CURL} "$@" ;;
esac
""" + STUB_CURL.split("\n", 1)[1]

# S4's Cloud SQL holding-connection helper is never run for real.
PYTHON = f"""#!/bin/sh
case "$*" in
  *sql.connector*) echo "python3 cloud-sql helper (stub)"; exit 0 ;;
esac
exec {REAL_PYTHON} "$@"
"""

# The Apigee stand-in. GET /ata/inventory/v1/items answers at once (S4 reads it before the
# leak to pick the order item). Order requests (POST /ata/orders/v1/orders, GET .../orders/test)
# are held for $ATA_ORDER_DELAY seconds. Each request is logged before it is answered.
SERVER = """
import http.server, json, os, ssl, sys, time
log, delay = os.environ["ATA_SERVER_LOG"], float(os.environ["ATA_ORDER_DELAY"])

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one(self):
        with open(log, "a") as f:
            f.write(f"{self.command} {self.path}\\n")
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if "/orders" in self.path:
            time.sleep(delay)
            body, code = b'{"status":"error"}', 502
        else:
            body, code = json.dumps([{"id": "item-105", "stock": 100000}]).encode(), 500
            if self.path.endswith("/items") and self.command == "GET":
                code = 200
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    do_GET = do_POST = handle_one

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
srv.daemon_threads = True
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(srv.server_address[1], flush=True)
srv.serve_forever()
"""


@pytest.fixture
def apigee(tmp_path, request):
    """Start the local HTTPS server. Parametrized indirectly with the order delay (seconds)."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    (tmp_path / "server.py").write_text(SERVER)
    log = tmp_path / "server.log"
    env = dict(os.environ, ATA_SERVER_LOG=str(log), ATA_ORDER_DELAY=str(request.param))
    proc = subprocess.Popen([sys.executable, "-I", str(tmp_path / "server.py"), str(cert), str(key)],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        port = proc.stdout.readline().strip()
        assert port, "the local HTTPS server did not start"
        yield f"127.0.0.1:{port}", log
    finally:
        proc.kill()
        proc.wait()


def install(bin_dir, stubs):
    bin_dir.mkdir(exist_ok=True)
    for name, body in stubs:
        stub = bin_dir / name
        if stub.exists():
            stub.unlink()
        stub.write_text(body)
        stub.chmod(0o755)


def scenario_env(tmp_path, apigee_host):
    """Environment for S1-S4: stubbed gcloud/pkill/python3's Cloud SQL helper, wrapped curl."""
    bin_dir = tmp_path / "bin"
    install(bin_dir, [("curl", CURL), ("gcloud", GCLOUD), ("python3", PYTHON),
                      ("pkill", '#!/bin/sh\necho "pkill $*" >> "$ATA_TEST_CALLS"\nexit 1\n')])
    (tmp_path / "import.json").write_text(json.dumps({"name": "ata-orders-v1", "revision": "169"}))
    (tmp_path / "deployments.json").write_text(json.dumps({"deployments": [
        {"environment": "prod", "apiProxy": "ata-orders-v1", "revision": "168"}]}))
    e = {k: v for k, v in os.environ.items() if k not in ("ATA_API_KEY", "MOCK_MODE", "DRY_RUN")}
    e.update(STACK_ENV)
    e.update(PATH=f"{bin_dir}{os.pathsep}{e['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
             ATA_IMPORT_BODY=str(tmp_path / "import.json"),
             ATA_DEPLOYMENTS_BODY=str(tmp_path / "deployments.json"),
             ATA_API_KEY="test-key", MOCK_MODE="false", DRY_RUN="false", RUN_PREFLIGHT="false",
             APIGEE_HOST=apigee_host)
    return e


def run_on_pty_and_press_ctrl_c(argv, env, cwd, server_log, delay):
    """Run argv as the foreground process group of a new pseudo-terminal. Once the traffic
    loop's first order request reached the server, wait `delay` seconds and type ^C into the
    terminal. Returns (exit code, everything the terminal showed)."""
    pid, fd = pty.fork()
    if pid == 0:  # child: session leader with the pty as its controlling terminal
        try:
            os.chdir(str(cwd))
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)

    out = bytearray()
    sent = False
    deadline = time.monotonic() + 90
    ctrl_c_at = None
    status = None
    try:
        while time.monotonic() < deadline:
            if not sent:
                if ctrl_c_at is None and server_log.exists() and "/orders" in server_log.read_text():
                    ctrl_c_at = time.monotonic() + delay
                if ctrl_c_at is not None and time.monotonic() >= ctrl_c_at:
                    os.write(fd, b"\x03")  # the terminal's VINTR: SIGINT to the foreground group
                    sent = True
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:  # EIO: the child side closed
                    chunk = b""
                if not chunk:
                    break
                out += chunk
        _, status = os.waitpid(pid, 0)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    text = out.decode(errors="replace")
    assert sent, "the traffic loop never sent an order request:\n" + text
    return os.waitstatus_to_exitcode(status), text


def assert_finished_gracefully(rc, output):
    assert "interrupted by user (Ctrl-C). Finishing scenario..." in output, output
    assert "aborted (exit" not in output, output
    assert "Traffic generation completed" in output, output
    assert "NEXT STEPS" in output, output
    assert rc == 0, output


def curl_calls(tmp_path):
    log = tmp_path / "calls"
    return [c for c in log.read_text().splitlines() if c.startswith("curl ")] if log.exists() else []


# ^C while the order curl is in flight (the server holds it for 8 s), or about 0.3 s after an
# order request reached the server (the request has been answered, so usually in the sleep).
WHERE = [pytest.param(8, 0.5, id="during-curl"), pytest.param(0, 0.3, id="after-curl")]


@pytest.mark.parametrize("apigee, delay", WHERE, indirect=["apigee"])
@pytest.mark.parametrize("script", ["s1_backend_5xx.sh", "s3_concurrent_disjoint.sh",
                                    "s4_db_connection_pool.sh"])
def test_ctrl_c_on_a_terminal_finishes_and_keeps_the_fault(tmp_path, apigee, delay, script):
    host, server_log = apigee
    env = scenario_env(tmp_path, host)
    rc, output = run_on_pty_and_press_ctrl_c([TEST_BASH, str(SCENARIOS_DIR / script), "--hold", "5m"],
                                             env, tmp_path, server_log, delay)
    assert_finished_gracefully(rc, output)
    calls = curl_calls(tmp_path)
    assert any("/admin/fault/inject" in c or "/admin/leak-connections" in c for c in calls)
    assert not any("/admin/fault/reset" in c for c in calls)
    assert not any(REDEPLOY_168 in c for c in calls)


# S2's traffic curls run in the background (they ignore SIGINT), so ^C lands in the sleep.
@pytest.mark.parametrize("apigee", [0], indirect=True)
def test_s2_ctrl_c_on_a_terminal_finishes_and_keeps_the_fault(tmp_path, apigee):
    host, server_log = apigee
    env = scenario_env(tmp_path, host)
    rc, output = run_on_pty_and_press_ctrl_c(
        [TEST_BASH, str(SCENARIOS_DIR / "s2_backend_latency.sh"), "--hold", "5m"],
        env, tmp_path, server_log, 0.5)
    assert_finished_gracefully(rc, output)
    calls = curl_calls(tmp_path)
    assert any("/admin/fault/inject" in c for c in calls)
    assert not any("/admin/fault/reset" in c for c in calls)


@pytest.mark.parametrize("apigee, delay", WHERE, indirect=["apigee"])
def test_s5_ctrl_c_on_a_terminal_finishes_and_keeps_the_bad_deploy(tmp_path, apigee, delay):
    host, server_log = apigee
    s5 = Env(tmp_path, baseline_state())
    install(s5.dir, [("curl", CURL)])
    e = os.environ.copy()
    for var in ("PINNED_INVENTORY_DIGEST", "PINNED_INVENTORY_REVISION", "PINNED_INVENTORY_IMAGE",
                "PROJECT_ID", "GCP_PROJECT", "REGION", "GCP_REGION", "NAME_PREFIX"):
        e.pop(var, None)
    e.update(STACK_ENV)
    e.update(PATH=f"{s5.dir}{os.pathsep}{e['PATH']}", MOCK_MODE="false", DRY_RUN="false",
             ATA_API_KEY="test-key", APIGEE_HOST=host, ATA_TEST_CALLS=str(tmp_path / "calls"))
    rc, output = run_on_pty_and_press_ctrl_c(
        [TEST_BASH, str(SCENARIOS_DIR / "s5_bad_deploy.sh"), "--hold", "5m"], e, tmp_path, server_log, delay)
    assert_finished_gracefully(rc, output)
    assert s5.state["env"]["ATA_INVENTORY_DB_USER"] == BAD_USER
    assert s5.state["traffic"] != [[PINNED_REVISION, 100]]
