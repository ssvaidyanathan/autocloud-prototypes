from pathlib import Path

import pytest
from agent.app.config import AgentConfig, get_settings


def test_agent_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    config = AgentConfig()
    assert config.port == 8080
    assert config.name_prefix == "isa"
    assert config.gcp_project == "local-project"
    assert config.gcp_region == "us-central1"
    assert config.vertex_location == "global"
    assert config.model_dispatcher == "gemini-3.8-flash"
    assert config.model_specialist == "gemini-3.8-flash"
    assert config.model_synthesizer == "gemini-3.8-flash"
    assert config.allowed_callback_hosts == ["*.run.app"]
    assert config.timeout_seconds == 600
    assert config.specialist_timeout_seconds == 120.0
    assert config.specialist_max_llm_calls == 8
    assert config.specialist_max_tool_calls == 5
    assert config.max_tool_calls_per_specialist == 25
    assert config.max_query_window_hours == 24
    assert config.callback_connect_timeout_seconds == 5.0
    assert config.callback_read_timeout_seconds == 45.0


def test_agent_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9091")
    monkeypatch.setenv("NAME_PREFIX", "acme")
    monkeypatch.setenv("GCP_PROJECT", "custom-agent-project")
    monkeypatch.setenv("GCP_REGION", "europe-west4")
    monkeypatch.setenv("VERTEX_LOCATION", "europe-west4")
    monkeypatch.setenv("MODEL_DISPATCHER", "test-model-dispatcher")
    monkeypatch.setenv("MODEL_SPECIALIST", "test-model-specialist")
    monkeypatch.setenv("MODEL_SYNTHESIZER", "test-model-synthesizer")
    monkeypatch.setenv("ALLOWED_CALLBACK_HOSTS", "custom.run.app,internal.net,localhost")
    monkeypatch.setenv("TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("SPECIALIST_MAX_LLM_CALLS", "12")
    monkeypatch.setenv("SPECIALIST_MAX_TOOL_CALLS", "6")
    monkeypatch.setenv("MAX_TOOL_CALLS_PER_SPECIALIST", "15")
    monkeypatch.setenv("MAX_QUERY_WINDOW_HOURS", "12")
    monkeypatch.setenv("CALLBACK_CONNECT_TIMEOUT_SECONDS", "10.0")
    monkeypatch.setenv("CALLBACK_READ_TIMEOUT_SECONDS", "60.0")

    config = AgentConfig()

    assert config.port == 9091
    assert config.name_prefix == "acme"
    assert config.gcp_project == "custom-agent-project"
    assert config.gcp_region == "europe-west4"
    assert config.vertex_location == "europe-west4"
    assert config.model_dispatcher == "test-model-dispatcher"
    assert config.model_specialist == "test-model-specialist"
    assert config.model_synthesizer == "test-model-synthesizer"
    assert config.allowed_callback_hosts == ["custom.run.app", "internal.net", "localhost"]
    assert config.timeout_seconds == 300
    assert config.specialist_max_llm_calls == 12
    assert config.specialist_max_tool_calls == 6
    assert config.max_tool_calls_per_specialist == 15
    assert config.max_query_window_hours == 12
    assert config.callback_connect_timeout_seconds == 10.0
    assert config.callback_read_timeout_seconds == 60.0


def test_agent_config_production_requires_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr("google.auth.default", lambda: (None, None))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Mock no metadata")),
    )

    with pytest.raises(ValueError, match="GCP_PROJECT is required"):
        AgentConfig(gcp_project=None)


def test_all_config_fields_in_env_example() -> None:
    env_example_path = Path(__file__).resolve().parents[1] / ".env.example"
    assert env_example_path.exists(), ".env.example must exist"
    env_content = env_example_path.read_text()

    # Extract uppercase variable keys from .env.example
    example_keys = set()
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=")[0].strip()
            example_keys.add(key.upper())

    # Ensure every field in AgentConfig is documented in .env.example
    model_fields = AgentConfig.model_fields.keys()
    missing_fields = []
    for field_name in model_fields:
        if field_name.upper() not in example_keys:
            missing_fields.append(field_name)

    assert not missing_fields, f"Fields in AgentConfig missing from .env.example: {missing_fields}"


def test_get_settings_singleton() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
