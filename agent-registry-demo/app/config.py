from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    project_id: str = Field(
        default="cloud-assist-fde-7",
        validation_alias=AliasChoices("project_id", "google_cloud_project"),
    )
    region: str = Field(
        default="us-central1",
        validation_alias=AliasChoices("region", "google_cloud_location"),
    )
    agent_registry_endpoint: str = "https://agentregistry.googleapis.com/v1alpha"
    port: int = 8080
    log_level: str = "INFO"
    service_name: str = "agent-registry-demo"
    skill_name: str = "calculator"
    skill_cache_ttl: float = 60.0
    tools_registry_endpoint: str = "https://agentregistry.googleapis.com/v1"
    calculator_service_url: str = "http://localhost:8080/mcp"
    calculator_mcp_server: str = "calculator"


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
