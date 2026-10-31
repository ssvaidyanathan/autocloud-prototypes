from collections.abc import Iterator

import pytest
from bridge.app.config import get_settings


@pytest.fixture(autouse=True)
def default_bridge_test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure bridge tests run hermetically with test project and environment."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr("bridge.app.config.resolve_platform_project", lambda: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
