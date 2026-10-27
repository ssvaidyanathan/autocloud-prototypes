"""Suite-wide test settings.

- NAME_PREFIX, PROJECT_ID and REGION are set explicitly. The services have no default prefix (no literal 'ata' in code),
  and the topology loader reads NAME_PREFIX when it is imported, so this must happen before the
  test modules import the services. The suite uses 'ata' as its test prefix; prefix-portability
  tests switch to another prefix explicitly. Project and region are neutral test values: the
  services have no live project/region defaults.
- Live Vertex AI (Gemini) calls are blocked: with google-genai installed, the gray-zone path
  would otherwise call the real API with whatever ADC the machine has. Tests that exercise
  Gemini replace google.genai.Client with a fake.
"""

import os

import pytest

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
