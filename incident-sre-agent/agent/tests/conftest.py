import os
from collections.abc import Iterator

import pytest
from agent.app.config import get_settings
from agent.app.tools.clients import clear_client_cache
from agent.app.tools.registry import default_query_cache


@pytest.fixture(autouse=True)
def default_local_test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure agent tests run hermetically under test environment."""
    if os.getenv("RUN_LIVE_TESTS") != "1":
        monkeypatch.setenv("GCP_PROJECT", "test-proj")
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setattr("agent.app.config.resolve_platform_project", lambda: None)
    get_settings.cache_clear()
    clear_client_cache()
    default_query_cache.clear_sync()
    yield
    get_settings.cache_clear()
    clear_client_cache()
    default_query_cache.clear_sync()
