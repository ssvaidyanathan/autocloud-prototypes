import os

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    """Configuration for the sample pipeline application."""

    app_name: str = Field(default="sample-pipeline-app", description="Name of the service")
    app_version: str = Field(default="1.0.0", description="Application semantic version")
    environment: str = Field(
        default_factory=lambda: os.getenv("APP_ENV", "development"),
        description="Deployment environment (development, staging, production)",
    )
    port: int = Field(
        default_factory=lambda: int(os.getenv("PORT", "8080")),
        description="Listening HTTP port",
    )
    crash_on_startup: bool = Field(
        default_factory=lambda: os.getenv("DEMO_CRASH_ON_STARTUP", "").lower() in ("true", "1"),
        description="Demo failure injection flag to crash process on startup",
    )
    allocate_oom: bool = Field(
        default_factory=lambda: os.getenv("DEMO_ALLOCATE_OOM", "").lower() in ("true", "1"),
        description="Demo failure injection flag to simulate memory exhaustion (exit 137)",
    )


def get_config() -> AppConfig:
    """Retrieve application configuration instance."""
    return AppConfig()
