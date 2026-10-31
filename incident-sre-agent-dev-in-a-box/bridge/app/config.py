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
    name_prefix: str = "isa"
    service_name: str = "incident-bridge"
    gcp_project: Optional[str] = None
    gcp_region: str = "us-central1"
    tasks_queue: str = "investigations"
    tasks_location: str = "us-central1"
    tasks_invoker_sa: str = ""
    agent_service_url: str = ""
    agent_sa: str = ""
    callback_url: str = ""
    adapter_timeout_seconds: float = 30.0
    servicenow_chunk_size: int = 12000
    jira_chunk_size: int = 32000

    # ServiceNow
    servicenow_instance_url: Optional[str] = None
    servicenow_username: Optional[str] = None
    servicenow_password: Optional[str] = None
    servicenow_webhook_token: Optional[str] = None
    servicenow_in_progress_state: str = "2"

    # Jira
    jira_base_url: Optional[str] = None
    jira_email: Optional[str] = None
    jira_api_token: Optional[str] = None
    jira_webhook_token: Optional[str] = None
    jira_in_progress_transition: str = "In Progress"

    # Generic webhook
    generic_hmac_key: Optional[str] = None

    # HITL and Templates
    ticket_template_path: Optional[str] = None
    hitl_caller_sa: Optional[str] = None

    @property
    def is_local(self) -> bool:
        env_val = self.app_env or self.environment
        return env_val.lower() == "local"

    @model_validator(mode="after")
    def resolve_project_and_defaults(self) -> "BridgeConfig":
        if not self.gcp_project or not self.gcp_project.strip():
            if self.is_local:
                self.gcp_project = "local-project"
            else:
                import os

                for env_var in ("GCP_PROJECT", "PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
                    val = os.getenv(env_var, "").strip()
                    if val:
                        self.gcp_project = val
                        break

                if not self.gcp_project or not self.gcp_project.strip():
                    self.gcp_project = resolve_platform_project()

                if not self.gcp_project or not self.gcp_project.strip():
                    raise ValueError(
                        "GCP_PROJECT is required and could not be resolved from environment, "
                        "ADC, or metadata server. Set GCP_PROJECT in the environment or .env file."
                    )

        if not self.tasks_queue or self.tasks_queue == "investigations":
            self.tasks_queue = f"{self.name_prefix}-investigations"

        if not self.tasks_invoker_sa:
            self.tasks_invoker_sa = (
                f"{self.name_prefix}-tasks-invoker-sa@{self.gcp_project}.iam.gserviceaccount.com"
            )
        if not self.agent_sa:
            self.agent_sa = (
                f"{self.name_prefix}-agent-sa@{self.gcp_project}.iam.gserviceaccount.com"
            )
        if not self.hitl_caller_sa:
            self.hitl_caller_sa = f"hitl-chatbot-sa@{self.gcp_project}.iam.gserviceaccount.com"

        return self

    @property
    def secret_ids(self) -> list[str]:
        return [
            f"{self.name_prefix}-bridge-servicenow-username",
            f"{self.name_prefix}-bridge-servicenow-password",
            f"{self.name_prefix}-bridge-servicenow-webhook-token",
            f"{self.name_prefix}-bridge-jira-email",
            f"{self.name_prefix}-bridge-jira-api-token",
            f"{self.name_prefix}-bridge-jira-webhook-token",
            f"{self.name_prefix}-bridge-generic-hmac-key",
        ]

    def get_secret_id(self, base_name: str) -> str:
        """Returns the prefixed Secret Manager secret ID for a given base secret name."""
        if base_name.startswith(f"{self.name_prefix}-"):
            return base_name
        return f"{self.name_prefix}-{base_name}"


# Alias for flexibility
BridgeSettings = BridgeConfig


@lru_cache
def get_settings() -> BridgeConfig:
    return BridgeConfig()
