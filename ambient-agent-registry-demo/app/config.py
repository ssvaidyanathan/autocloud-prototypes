"""Application configuration and settings for Ambient Agent Registry Demo."""

import os
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="ambient_", case_sensitive=False, extra="ignore"
    )

    project_id: str = Field(
        default="demo-project-id",
        validation_alias=AliasChoices("ambient_project_id", "project_id", "google_cloud_project"),
    )
    region: str = Field(
        default="us-central1",
        validation_alias=AliasChoices("ambient_region", "region", "google_cloud_location"),
    )
    skill_location: str = Field(
        default="global",
        validation_alias=AliasChoices(
            "ambient_skill_location", "skill_location", "google_cloud_skill_location"
        ),
    )
    agent_registry_endpoint: str = Field(
        default="https://agentregistry.googleapis.com/v1alpha",
        validation_alias=AliasChoices(
            "ambient_agent_registry_endpoint", "agent_registry_endpoint"
        ),
    )
    port: int = Field(
        default=8080,
        validation_alias=AliasChoices("ambient_port", "port"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("ambient_log_level", "log_level"),
    )
    service_name: str = Field(
        default="ambient-agent-service",
        validation_alias=AliasChoices("ambient_service_name", "service_name"),
    )
    skill_name: str = Field(
        default="ambient-privilege-audit-skill",
        validation_alias=AliasChoices("ambient_skill_name", "skill_name"),
    )
    skill_cache_ttl: float = Field(
        default=60.0,
        validation_alias=AliasChoices("ambient_skill_cache_ttl", "skill_cache_ttl"),
    )
    tools_registry_endpoint: str = Field(
        default="https://agentregistry.googleapis.com/v1",
        validation_alias=AliasChoices(
            "ambient_tools_registry_endpoint", "tools_registry_endpoint"
        ),
    )
    calculator_service_url: str = Field(
        default="http://localhost:8080/mcp",
        validation_alias=AliasChoices(
            "ambient_calculator_service_url", "calculator_service_url", "tool_service_url"
        ),
    )
    calculator_mcp_server: str = Field(
        default="ambient-calculator",
        validation_alias=AliasChoices(
            "ambient_calculator_mcp_server", "calculator_mcp_server"
        ),
    )
    model_name: str = Field(
        default="gemini-2.5-flash",
        validation_alias=AliasChoices("ambient_model_name", "model_name", "gemini_model"),
    )
    mock_registry: bool = Field(
        default=False,
        validation_alias=AliasChoices("ambient_mock_registry", "mock_registry"),
    )

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
