from pathlib import Path

import pytest
from bridge.app.config import BridgeConfig, get_settings


def test_bridge_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    config = BridgeConfig(app_env="local")
    assert config.port == 8080
    assert config.name_prefix == "isa"
    assert config.gcp_project == "local-project"
    assert config.gcp_region == "us-central1"
    assert config.tasks_queue == "isa-investigations"
    assert config.tasks_location == "us-central1"
    assert config.servicenow_in_progress_state == "2"
    assert config.servicenow_chunk_size == 12000
    assert config.jira_in_progress_transition == "In Progress"
    assert config.servicenow_instance_url is None
    assert config.jira_base_url is None
    assert config.generic_hmac_key is None
    assert config.tasks_invoker_sa == "isa-tasks-invoker-sa@local-project.iam.gserviceaccount.com"
    assert config.agent_sa == "isa-agent-sa@local-project.iam.gserviceaccount.com"
    assert config.get_secret_id("bridge-servicenow-username") == "isa-bridge-servicenow-username"
    assert "isa-bridge-servicenow-username" in config.secret_ids


def test_bridge_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.setenv("NAME_PREFIX", "acme")
    monkeypatch.setenv("GCP_PROJECT", "custom-gcp-project")
    monkeypatch.setenv("GCP_REGION", "europe-west1")
    monkeypatch.setenv("TASKS_QUEUE", "custom-tasks")
    monkeypatch.setenv("TASKS_LOCATION", "europe-west1")
    monkeypatch.setenv("TASKS_INVOKER_SA", "invoker@custom-project.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://sre-agent-custom.a.run.app")
    monkeypatch.setenv("AGENT_SA", "agent@custom-project.iam.gserviceaccount.com")
    monkeypatch.setenv("CALLBACK_URL", "https://bridge-custom.a.run.app/callback")

    monkeypatch.setenv("SERVICENOW_INSTANCE_URL", "https://instance.service-now.com")
    monkeypatch.setenv("SERVICENOW_USERNAME", "snow_user")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "mock-snow-password")
    monkeypatch.setenv("SERVICENOW_WEBHOOK_TOKEN", "mock-snow-token")
    monkeypatch.setenv("SERVICENOW_IN_PROGRESS_STATE", "in_progress")

    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "jira_user@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "mock-jira-token")
    monkeypatch.setenv("JIRA_WEBHOOK_TOKEN", "mock-jira-token")
    monkeypatch.setenv("JIRA_IN_PROGRESS_TRANSITION", "Working")

    monkeypatch.setenv("GENERIC_HMAC_KEY", "mock-hmac-key")

    config = BridgeConfig()

    assert config.port == 9090
    assert config.name_prefix == "acme"
    assert config.gcp_project == "custom-gcp-project"
    assert config.gcp_region == "europe-west1"
    assert config.tasks_queue == "custom-tasks"
    assert config.tasks_location == "europe-west1"
    assert config.tasks_invoker_sa == "invoker@custom-project.iam.gserviceaccount.com"
    assert config.agent_service_url == "https://sre-agent-custom.a.run.app"
    assert config.agent_sa == "agent@custom-project.iam.gserviceaccount.com"
    assert config.callback_url == "https://bridge-custom.a.run.app/callback"
    assert config.get_secret_id("bridge-servicenow-username") == "acme-bridge-servicenow-username"
    assert "acme-bridge-servicenow-username" in config.secret_ids

    assert config.servicenow_instance_url == "https://instance.service-now.com"
    assert config.servicenow_username == "snow_user"
    assert config.servicenow_password == "mock-snow-password"
    assert config.servicenow_webhook_token == "mock-snow-token"
    assert config.servicenow_in_progress_state == "in_progress"

    assert config.jira_base_url == "https://jira.example.com"
    assert config.jira_email == "jira_user@example.com"
    assert config.jira_api_token == "mock-jira-token"
    assert config.jira_webhook_token == "mock-jira-token"
    assert config.jira_in_progress_transition == "Working"

    assert config.generic_hmac_key == "mock-hmac-key"


def test_bridge_config_name_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.setenv("NAME_PREFIX", "acme")
    config = BridgeConfig(app_env="local")
    assert config.name_prefix == "acme"
    assert config.tasks_queue == "acme-investigations"
    assert config.tasks_invoker_sa == "acme-tasks-invoker-sa@local-project.iam.gserviceaccount.com"
    assert config.agent_sa == "acme-agent-sa@local-project.iam.gserviceaccount.com"
    assert config.get_secret_id("bridge-jira-email") == "acme-bridge-jira-email"
    assert "acme-bridge-jira-email" in config.secret_ids


def test_bridge_config_production_requires_project(monkeypatch: pytest.MonkeyPatch) -> None:
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
        BridgeConfig(gcp_project=None, app_env="production")


def test_all_bridge_config_fields_in_env_example() -> None:
    env_example_path = Path(__file__).resolve().parents[1] / ".env.example"
    assert env_example_path.exists(), ".env.example must exist"
    env_content = env_example_path.read_text()

    example_keys = set()
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=")[0].strip()
            example_keys.add(key.upper())

    model_fields = BridgeConfig.model_fields.keys()
    missing_fields = []
    for field_name in model_fields:
        if field_name.upper() not in example_keys:
            missing_fields.append(field_name)

    assert not missing_fields, f"Fields in BridgeConfig missing from .env.example: {missing_fields}"


def test_get_settings_singleton() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
