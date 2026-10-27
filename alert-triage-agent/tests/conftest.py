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
"""

import ipaddress
import os
import socket

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
