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
TEST_PIN_REVISION = f"{TEST_NAME_PREFIX}-inventory-api-pin-tests"
TEST_PIN_DIGEST = "sha256:" + "0123456789abcdef" * 4
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


# --- The committed inventory pin files are stack data, not test data (fresh-rev-delta R1) ----
# scripts/scenarios/inventory-pin.<prefix>.env changes on every re-pin. A test that reads it for
# expected values (or runs a script that does) turns the suite red on every legitimate re-pin.
# Tests write their own pin file and point ATA_INVENTORY_PIN_FILE at it (or run a copy of
# scripts/ under tmp_path). This guard fails any test that
#   - opens a committed pin file from Python (read_text, open, shutil.copy, ...), or
#   - starts a process (subprocess.*, os.system, os.posix_spawn, os.exec*) that
#     - names a committed pin file in its argv or command string (`cat <pin>`,
#       `bash -c 'source scripts/scenarios/inventory-pin.ata.env'`, a glob that matches one),
#       relative paths resolved against the process's cwd (the given cwd, else the test's), or
#     - has ATA_INVENTORY_PIN_FILE naming a committed pin, or
#     - has ATA_INVENTORY_PIN_FILE unset/empty and runs a script of this checkout (argv, a
#       relative path under the cwd, or a script file that names this checkout's scripts/
#       directory) with a NAME_PREFIX that has a committed pin. This flags any checkout script
#       run that way, even one that never sources common.sh: the remedy is always the fixture
#       pin (set ATA_INVENTORY_PIN_FILE), not the marker.
# A test that deliberately validates the committed file's FORMAT is marked
# @pytest.mark.reads_committed_pin and must not assert its values.
#
# This is test HYGIENE, not a security boundary: it catches the ways a test plausibly reads the
# pin by accident. Deliberate evasion (a path assembled inside a shell, a child process that
# opens the file on its own initiative, ctypes, ...) is not caught and is not meant to be.
import re  # noqa: E402
import sys  # noqa: E402

_SCRIPTS_DIR = os.path.join(_CHECKOUT, "scripts")
_SCENARIOS_DIR = os.path.join(_SCRIPTS_DIR, "scenarios")
_COMMITTED_PINS = {os.path.realpath(p) for p in glob.glob(os.path.join(_SCENARIOS_DIR, "inventory-pin.*.env"))}
_COMMITTED_PIN_PREFIXES = {os.path.basename(p)[len("inventory-pin."):-len(".env")] for p in _COMMITTED_PINS}
_CHECKOUT_SCRIPT_ROOTS = tuple(os.path.realpath(os.path.join(_CHECKOUT, d)) + os.sep
                               for d in ("scripts", "demo", "agent", "bridge", "tools"))
# Path-like words of a shell command string: split on whitespace, quotes and shell operators,
# and on "=" so that VAR=<path> yields <path>.
_SHELL_WORD = re.compile(r"""[^\s'"`=;&|<>()]+""")
_pin_guard = {"active": False, "allowed": False, "busy": False, "violations": [], "last_popen_argv": None}


def _fsstr(value):
    try:
        return os.fsdecode(os.fspath(value))
    except TypeError:
        return None


def _resolve(path: str, cwd) -> str:
    return os.path.join(cwd or os.getcwd(), path) if not os.path.isabs(path) else path


def _is_committed_pin(path, cwd=None) -> bool:
    p = _fsstr(path)
    if not p or "\0" in p:
        return False
    return os.path.basename(p).startswith("inventory-pin.") and os.path.realpath(_resolve(p, cwd)) in _COMMITTED_PINS


def _names_committed_pin(arg: str, cwd) -> str:
    """The committed pin that argv element / command string `arg` names (resolved against cwd), or ""."""
    if "inventory-pin" not in arg:  # cheap pre-filter: every way to name the file contains this
        return ""
    for word in [arg, *_SHELL_WORD.findall(arg)]:
        if _is_committed_pin(word, cwd):
            return word
        if "inventory-pin" in word and any(c in word for c in "*?["):
            hits = [h for h in glob.glob(_resolve(word, cwd)) if _is_committed_pin(h)]
            if hits:
                return word
    return ""


def _names_checkout_script(arg: str, cwd=None) -> bool:
    """Whether argv element / command string `arg` runs a script of this checkout: a path (relative
    ones resolved against cwd) under one of its script roots, a script file that names its
    scripts/ directory, or a command string with such a path among its words."""
    if not arg or "\0" in arg:
        return False
    if _SCRIPTS_DIR + os.sep in arg:  # bash -c '... source <checkout>/scripts/...'
        return True
    words = [arg] + [w for w in _SHELL_WORD.findall(arg) if w != arg]
    if any(os.path.realpath(_resolve(w, cwd)).startswith(_CHECKOUT_SCRIPT_ROOTS) for w in words):
        return True
    real = os.path.realpath(_resolve(arg, cwd))
    if os.path.isfile(real) and os.path.getsize(real) < 256 * 1024:
        try:
            with open(real, "rb") as f:
                return os.fsencode(_SCRIPTS_DIR + os.sep) in f.read()
        except OSError:
            return False
    return False


def _argv_strings(args) -> list:
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or ())
    return [a for a in (_fsstr(x) for x in argv) if a is not None]


def _process_reads_committed_pin(args, env, cwd=None) -> str:
    """Why the process (argv list or command string, env, cwd) would read a committed pin, or ""."""
    cwd = _fsstr(cwd) if cwd is not None else None
    argv = _argv_strings(args)
    for a in argv:
        named = _names_committed_pin(a, cwd)
        if named:
            return f"names the committed pin {named} in its command ({a[:120]})"
    env = os.environ if env is None else {os.fsdecode(k): os.fsdecode(v) for k, v in dict(env).items()}
    pin_file = env.get("ATA_INVENTORY_PIN_FILE", "")
    if pin_file:
        return f"ATA_INVENTORY_PIN_FILE={pin_file}" if _is_committed_pin(pin_file, cwd) else ""
    if env.get("NAME_PREFIX", "") not in _COMMITTED_PIN_PREFIXES:
        return ""
    for a in argv:
        if _names_checkout_script(a, cwd):
            return f"runs {a} with NAME_PREFIX={env['NAME_PREFIX']} and no ATA_INVENTORY_PIN_FILE"
    return ""


_PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec")


def _pin_audit_hook(event, args):
    g = _pin_guard
    if not g["active"] or g["allowed"] or g["busy"] or (event != "open" and event not in _PROCESS_EVENTS):
        return
    g["busy"] = True
    try:
        why = ""
        if event == "open":
            if args and _is_committed_pin(args[0]):
                g["violations"].append(f"opened {os.fsdecode(os.fspath(args[0]))}")
            return
        if event == "subprocess.Popen":  # (executable, args, cwd, env)
            g["last_popen_argv"] = _argv_strings(args[1])
            why = _process_reads_committed_pin(args[1], args[3], args[2])
        elif event == "os.system":  # (command,): a /bin/sh -c string, in this process's cwd and env
            why = _process_reads_committed_pin(args[0], None)
        else:  # os.posix_spawn / os.exec: (path, argv, env)
            argv = _argv_strings(args[1])
            if event == "os.posix_spawn" and argv == g["last_popen_argv"]:
                g["last_popen_argv"] = None  # subprocess's own posix_spawn: already checked
                return
            why = _process_reads_committed_pin(argv, args[2])
        if why:
            g["violations"].append(f"subprocess {why}")
    finally:
        g["busy"] = False


sys.addaudithook(_pin_audit_hook)


def pytest_configure(config):
    config.addinivalue_line("markers", "reads_committed_pin: the test validates the FORMAT of a committed "
                                       "scripts/scenarios/inventory-pin.<prefix>.env (never its values).")


@pytest.fixture(autouse=True)
def _committed_pin_untouched(request):
    """Fail a test that reads a committed inventory pin file (see the block comment above)."""
    _pin_guard.update(active=True, allowed=request.node.get_closest_marker("reads_committed_pin") is not None,
                      violations=[])
    yield
    violations = list(_pin_guard["violations"])
    _pin_guard.update(active=False, allowed=False, violations=[])
    assert not violations, (
        "this test reads a committed inventory pin file (stack data that changes on every re-pin): "
        f"{violations[:3]}. Write a fixture pin file and set ATA_INVENTORY_PIN_FILE, or mark a format-only "
        "test @pytest.mark.reads_committed_pin (tests/conftest.py).")
