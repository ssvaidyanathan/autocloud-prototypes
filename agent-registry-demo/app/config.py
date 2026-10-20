from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    project_id: str = "cloud-assist-fde-7"
    region: str = "us-central1"
    agent_registry_endpoint: str = "https://agentregistry.googleapis.com/v1alpha"
    port: int = 8080
    log_level: str = "INFO"
    service_name: str = "agent-registry-demo"
    skill_name: str = "calculator"


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
