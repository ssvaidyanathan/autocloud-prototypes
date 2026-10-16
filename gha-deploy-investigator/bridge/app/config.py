import os
from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_platform_project() -> Optional[str]:
    """Resolve project ID from ADC or Compute Engine metadata server."""
    try:
        import google.auth

        _, adc_project = google.auth.default()
        if adc_project:
            return str(adc_project)
    except Exception:
        pass

    try:
        import urllib.request

        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            meta_project = str(resp.read().decode().strip())
            if meta_project:
                return meta_project
    except Exception:
        pass

    return None


class BridgeConfig(BaseSettings):
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
    service_name: str = "gdi-bridge"
    gcp_project: Optional[str] = None
    gcp_region: str = "us-central1"
    tasks_queue: str = "investigations"
    tasks_location: str = "us-central1"
    tasks_invoker_sa: str = ""
    agent_service_url: str = ""
    agent_sa: str = ""
    callback_url: str = ""
    adapter_timeout_seconds: float = 30.0

    # ServiceNow Configuration
    servicenow_instance_url: Optional[str] = None
    servicenow_username: Optional[str] = None
    servicenow_password: Optional[str] = None
    servicenow_webhook_token: Optional[str] = None
    servicenow_in_progress_state: str = "2"

    # GitHub Configuration
    github_webhook_secret: Optional[str] = None
    github_token: Optional[str] = None
    github_api_url: str = "https://api.github.com"
    enable_github_writeback: bool = True

    @property
    def is_local(self) -> bool:
        env_val = self.app_env or self.environment
        return env_val.lower() in ("local", "dev", "development", "test", "testing")

    @model_validator(mode="after")
    def resolve_project_and_defaults(self) -> "BridgeConfig":
        if not self.gcp_project or not self.gcp_project.strip():
            if self.is_local:
                self.gcp_project = "local-project"
            else:
                for env_var in ("GCP_PROJECT", "PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
                    val = os.getenv(env_var, "").strip()
                    if val:
                        self.gcp_project = val
                        break

                if not self.gcp_project or not self.gcp_project.strip():
                    self.gcp_project = resolve_platform_project()

                if not self.gcp_project or not self.gcp_project.strip():
                    self.gcp_project = "local-project"

        return self


@lru_cache
def get_settings() -> BridgeConfig:
    return BridgeConfig()
