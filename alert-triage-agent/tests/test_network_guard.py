"""The suite-wide network guard (tests/conftest.py): loopback only (test review M1)."""

import os
import socket

import pytest

from tests import conftest


pytestmark = pytest.mark.skipif(os.environ.get("ATA_TESTS_ALLOW_NETWORK") == "1",
                                reason="guard disabled by ATA_TESTS_ALLOW_NETWORK=1")


@pytest.mark.parametrize("host", ["metadata.google.internal", "iamcredentials.googleapis.com", "169.254.169.254",
                                  "8.8.8.8"])
def test_non_loopback_lookups_and_connects_are_blocked(host):
    before = len(conftest.BLOCKED_NETWORK_ATTEMPTS)
    with pytest.raises(OSError, match="network access blocked in tests"):
        socket.create_connection((host, 80), timeout=1)
    assert len(conftest.BLOCKED_NETWORK_ATTEMPTS) > before


def test_raw_connect_to_a_non_loopback_address_is_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(conftest.NetworkBlocked):
            s.connect(("169.254.169.254", 80))
        with pytest.raises(conftest.NetworkBlocked):
            s.connect_ex(("10.1.2.3", 443))
    finally:
        s.close()


def test_loopback_still_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        port = server.getsockname()[1]
        client = socket.create_connection(("localhost", port), timeout=2)
        client.close()
    finally:
        server.close()


def test_google_auth_default_finds_no_gce_credentials(monkeypatch):
    """On a GCE host this used to return the VM's service account (real API calls followed)."""
    google_auth = pytest.importorskip("google.auth")
    from google.auth import compute_engine, exceptions

    assert os.environ.get("NO_GCE_CHECK") == "true"
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    try:
        creds, _ = google_auth.default()
    except exceptions.DefaultCredentialsError:
        return
    # A developer machine with a gcloud ADC file: user credentials, never the VM's.
    assert not isinstance(creds, compute_engine.Credentials)
