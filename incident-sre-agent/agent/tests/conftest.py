from collections.abc import Iterator

import pytest
from agent.app.config import get_settings
from agent.app.tools.clients import clear_client_cache


@pytest.fixture(autouse=True)
def default_local_test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure agent tests run hermetically under test environment."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setattr("agent.app.config.resolve_platform_project", lambda: None)
    get_settings.cache_clear()
    clear_client_cache()
    yield
    get_settings.cache_clear()
    clear_client_cache()
