import os
from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    port: int = 8080
    app_env: str = "production"
    environment: str = "production"
    name_prefix: str = "gdi"
    service_name: str = "gdi-agent"
    gcp_project: Optional[str] = None
    bridge_service_url: str = ""

    # GitHub API configuration
    github_api_url: str = "https://api.github.com"
    github_token: Optional[str] = None
    github_app_id: Optional[str] = None
    github_app_private_key: Optional[str] = None

    @property
    def is_local(self) -> bool:
        env_val = self.app_env or self.environment
        return env_val.lower() in ("local", "dev", "development", "test", "testing")

    @model_validator(mode="after")
    def resolve_project_defaults(self) -> "AgentConfig":
        if not self.gcp_project or not self.gcp_project.strip():
            for env_var in ("GCP_PROJECT", "PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
                val = os.getenv(env_var, "").strip()
                if val:
                    self.gcp_project = val
                    break
            if not self.gcp_project:
                self.gcp_project = "local-project"
        return self


@lru_cache
def get_settings() -> AgentConfig:
    return AgentConfig()
