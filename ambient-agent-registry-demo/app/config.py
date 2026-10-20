"""Application configuration and settings for Ambient Agent Registry Demo."""

import os
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    project_id: str = Field(
        default="demo-project-id",
        validation_alias=AliasChoices("project_id", "google_cloud_project"),
    )
    region: str = Field(
        default="us-central1",
        validation_alias=AliasChoices("region", "google_cloud_location"),
    )
    skill_location: str = Field(
        default="global",
        validation_alias=AliasChoices("skill_location", "google_cloud_skill_location"),
    )
    agent_registry_endpoint: str = "https://agentregistry.googleapis.com/v1alpha"
    port: int = 8080
    log_level: str = "INFO"
    service_name: str = "ambient-agent-service"
    skill_name: str = "ambient-privilege-audit-skill"
    skill_cache_ttl: float = 60.0
    tools_registry_endpoint: str = "https://agentregistry.googleapis.com/v1"
    calculator_service_url: str = "http://localhost:8080/mcp"
    calculator_mcp_server: str = "ambient-calculator"
    model_name: str = Field(
        default="gemini-2.5-flash",
        validation_alias=AliasChoices("model_name", "gemini_model"),
    )
    mock_registry: bool = False

    def model_post_init(self, __context: object) -> None:
        """Enforce production safety guard prohibiting mock registry in production."""
        is_production = bool(os.getenv("K_SERVICE")) or (self.project_id != "demo-project-id")
        if is_production and self.mock_registry:
            raise RuntimeError(
                "Production safety guard: mock_registry must not be enabled in production "
                f"mode (project_id='{self.project_id}', K_SERVICE='{os.getenv('K_SERVICE')}')."
            )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
