"""Suite-wide test settings.

- NAME_PREFIX, PROJECT_ID and REGION are set explicitly. The services have no default prefix (no literal 'ata' in code),
  and the topology loader reads NAME_PREFIX when it is imported, so this must happen before the
  test modules import the services. The suite uses 'ata' as its test prefix; prefix-portability
  tests switch to another prefix explicitly. Project and region are neutral test values: the
  services have no live project/region defaults.
- Live Vertex AI (Gemini) calls are blocked: with google-genai installed, the gray-zone path
  would otherwise call the real API with whatever ADC the machine has. Tests that exercise
  Gemini replace google.genai.Client with a fake.
- All non-loopback network is blocked for the test process (test review M1): on a GCE or Cloud
  Shell host, google.auth.default() would otherwise find the metadata server and the specialists'
  Logging/Run/Monitoring clients would make real read calls. A DNS lookup of a non-loopback name
  or a connect to a non-loopback address raises NetworkBlocked (an OSError, so code sees an
  ordinary connection failure) and is recorded in BLOCKED_NETWORK_ATTEMPTS. Scripts run as
  subprocesses use stub binaries and are not affected. ATA_TESTS_ALLOW_NETWORK=1 turns the
  guard off for a local debugging run; CI never sets it.
- ATA_STACK_DIR (where the scripts keep .<prefix>-api-key, .<prefix>-apigee.env, .deploy-state/)
  is an empty per-session directory, so a checkout that deployed a stack gives the same results
  as a clean one. A session fixture fails the run if the checkout's stack-local files change.
"""

import atexit
import glob
import hashlib
import ipaddress
import os
import shutil
import socket
import tempfile

import pytest


class NetworkBlocked(OSError):
    """A test tried to reach a non-loopback host."""


BLOCKED_NETWORK_ATTEMPTS: list[str] = []
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_loopback(host) -> bool:
    if host is None:
        return True  # getaddrinfo(None, port): the local wildcard/loopback
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]")
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _blocked(what: str) -> NetworkBlocked:
    BLOCKED_NETWORK_ATTEMPTS.append(what)
    return NetworkBlocked(f"network access blocked in tests (tests/conftest.py): {what}")


def _install_network_guard() -> None:
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise _blocked(f"getaddrinfo({host!r})")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            if not _is_loopback(address[0]):
                raise _blocked(f"connect({address[0]!r}, {address[1] if len(address) > 1 else ''})")

    def connect(self, address):
        _check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(self, address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


if os.environ.get("ATA_TESTS_ALLOW_NETWORK") != "1":
    _install_network_guard()
    # google.auth would otherwise still conclude "on GCE" from /sys/class/dmi/id/product_name
    # after the blocked metadata ping, hand out VM credentials, and retry the metadata server
    # with backoff on every client creation (slow). NO_GCE_CHECK skips GCE detection; it is read
    # when google.auth.compute_engine._metadata is imported, so set it now and patch it if that
    # module is already loaded.
    os.environ["NO_GCE_CHECK"] = "true"
    try:
        import sys as _sys

        _md = _sys.modules.get("google.auth.compute_engine._metadata")
        if _md is not None:
            _md._NO_GCE_CHECK = True
    except Exception:  # pragma: no cover
        pass

TEST_NAME_PREFIX = "ata"
TEST_PROJECT_ID = "test-project"
TEST_REGION = "test-region1"
os.environ["NAME_PREFIX"] = TEST_NAME_PREFIX
os.environ["PROJECT_ID"] = TEST_PROJECT_ID
os.environ["REGION"] = TEST_REGION

# Stack-local files (.<prefix>-api-key, .<prefix>-apigee.env, .deploy-state/): the scripts read
# and write them in ATA_STACK_DIR (default: the checkout's alert-triage-agent/). A checkout that
# deployed a stack has real ones; the tests must never read or overwrite them, so every script
# the suite starts gets an empty per-session directory instead. Helpers that scrub ATA_* from a
# script's environment keep ATA_STACK_DIR; only tests of the default location drop it, and they
# run a copy of scripts/ under tmp_path.
TEST_STACK_DIR = tempfile.mkdtemp(prefix="ata-tests-stack-")
atexit.register(shutil.rmtree, TEST_STACK_DIR, True)
os.environ["ATA_STACK_DIR"] = TEST_STACK_DIR

# The inventory pin (scripts/scenarios/inventory-pin.<prefix>.env) is committed stack data: it
# changes with every live re-pin of that stack, so tests must not use it as a fixture. Every
# script the suite starts reads this per-session test pin instead (ATA_INVENTORY_PIN_FILE).
# Helpers that scrub ATA_* set their own pin file; tests of the default (committed) location
# drop the variable and parse the committed file rather than hard-coding its values.
TEST_PIN_REVISION = "ata-inventory-api-00073-9vb"
TEST_PIN_DIGEST = "sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce"
TEST_PIN_FILE = os.path.join(TEST_STACK_DIR, "inventory-pin.test.env")
with open(TEST_PIN_FILE, "w") as _f:
    _f.write(f"# test pin (tests/conftest.py)\nPIN_INVENTORY_REVISION={TEST_PIN_REVISION}\n"
             f"PIN_INVENTORY_DIGEST={TEST_PIN_DIGEST}\nPIN_INVENTORY_PREVIOUS_REVISION=\nPIN_INVENTORY_PREVIOUS_DIGEST=\n")
os.environ["ATA_INVENTORY_PIN_FILE"] = TEST_PIN_FILE


class _LiveGeminiBlocked:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("live Vertex AI calls are disabled in tests (tests/conftest.py)")


@pytest.fixture(autouse=True)
def _block_live_gemini(monkeypatch):
    try:
        from google import genai
    except ImportError:
        yield
        return
    monkeypatch.setattr(genai, "Client", _LiveGeminiBlocked)
    yield


_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _checkout_stack_files() -> dict:
    """Stack-local files in the checkout (a deployed stack's), with their content."""
    paths = glob.glob(os.path.join(_CHECKOUT, ".*-api-key")) + glob.glob(os.path.join(_CHECKOUT, ".*-apigee.env"))
    paths += glob.glob(os.path.join(_CHECKOUT, "scripts", "scenarios", "inventory-pin.*.env"))
    for root, _dirs, files in os.walk(os.path.join(_CHECKOUT, ".deploy-state")):
        paths += [os.path.join(root, f) for f in files]
    out = {}
    for p in paths:
        with open(p, "rb") as f:
            out[p] = hashlib.sha256(f.read()).hexdigest()
    return out


@pytest.fixture(scope="session", autouse=True)
def _checkout_stack_files_untouched():
    """No test creates, changes or deletes the checkout's stack-local files (see ATA_STACK_DIR above).
    Tests that plant such files (tests/test_stack_dir_isolation.py) remove them again."""
    before = _checkout_stack_files()
    yield
    after = _checkout_stack_files()
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    assert not changed, f"the test run changed stack-local files in the checkout: {changed}"
