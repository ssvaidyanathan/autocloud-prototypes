import logging
import os
import re
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class BridgeConfig(BaseSettings):
    """Bridge service configuration externalizing all environment variables."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_id: str = Field(default="cloud-assist-fde-2", alias="PROJECT_ID")
    # Numeric GCP project number. Used to build the bridge's deterministic Cloud Run URL
    # (https://<prefix>-bridge-<number>.<region>.run.app), an accepted ID-token audience.
    # Required when not local: startup fails and auth returns 500 without it.
    project_number: Optional[str] = Field(default=None, alias="PROJECT_NUMBER")
    name_prefix: str = Field(default="ata", alias="NAME_PREFIX")
    region: str = Field(default="us-west1", alias="REGION")
    environment: str = Field(default="prod", alias="ENVIRONMENT")

    # Cloud Storage Incidents Bucket
    incidents_bucket: Optional[str] = Field(default=None, alias="INCIDENTS_BUCKET")
    incident_store_path: Optional[str] = Field(default=None, alias="INCIDENT_STORE_PATH")

    # Agent Service
    agent_url: str = Field(default="http://localhost:8080", alias="AGENT_URL")

    # Cloud Tasks
    tasks_queue: Optional[str] = Field(default=None, alias="TASKS_QUEUE")
    tasks_location: Optional[str] = Field(default=None, alias="TASKS_LOCATION")
    tasks_invoker_sa: Optional[str] = Field(default=None, alias="TASKS_INVOKER_SA")

    # Webhook Authentication Token
    webhook_token: Optional[str] = Field(default=None, alias="WEBHOOK_TOKEN")

    # ServiceNow Integration
    servicenow_instance_url: Optional[str] = Field(default=None, alias="SERVICENOW_INSTANCE_URL")
    servicenow_username: Optional[str] = Field(default=None, alias="SERVICENOW_USERNAME")
    servicenow_password: Optional[str] = Field(default=None, alias="SERVICENOW_PASSWORD")
    servicenow_caller: Optional[str] = Field(default=None, alias="SERVICENOW_CALLER")
    servicenow_assignment_group: Optional[str] = Field(default=None, alias="SERVICENOW_ASSIGNMENT_GROUP")

    is_local: bool = Field(default=False, alias="IS_LOCAL")
    verifier_sa: Optional[str] = Field(default=None, alias="VERIFIER_SA")
    adapter_timeout_seconds: float = Field(default=30.0, alias="ADAPTER_TIMEOUT_SECONDS")
    cool_down_duration: float = Field(default=180.0, alias="COOL_DOWN_DURATION")

    @field_validator("project_number", mode="before")
    @classmethod
    def _validate_project_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if not re.fullmatch(r"[0-9]+", str(value)):
            raise ValueError(f"PROJECT_NUMBER must be the numeric GCP project number, got {value!r}")
        return str(value)

    def missing_required_settings(self) -> list[str]:
        """Settings without which a non-local bridge cannot authenticate callers correctly."""
        if self.is_local:
            return []
        return ["PROJECT_NUMBER"] if self.project_number is None else []

    @property
    def effective_incidents_bucket(self) -> str:
        return self.incidents_bucket or f"{self.project_id}-{self.name_prefix}-incidents"

    @property
    def effective_tasks_queue(self) -> str:
        return self.tasks_queue or f"{self.name_prefix}-triage-queue"

    @property
    def effective_tasks_location(self) -> str:
        return self.tasks_location or self.region

    @property
    def effective_tasks_invoker_sa(self) -> str:
        return self.tasks_invoker_sa or f"{self.name_prefix}-bridge-sa@{self.project_id}.iam.gserviceaccount.com"

    def resolve_secrets_from_manager(self) -> None:
        """Fetch secret values from Secret Manager if running in GCP and values are unset."""
        if self.is_local:
            return

        keys = {
            "webhook_token": f"{self.name_prefix}-webhook-token",
            "servicenow_username": f"{self.name_prefix}-servicenow-username",
            "servicenow_password": f"{self.name_prefix}-servicenow-password",
        }

        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            for attr_name, secret_id in keys.items():
                if getattr(self, attr_name) is None:
                    name = f"projects/{self.project_id}/secrets/{secret_id}/versions/latest"
                    try:
                        resp = client.access_secret_version(request={"name": name})
                        val = resp.payload.data.decode("utf-8").strip()
                        if val and (attr_name == "webhook_token" or val != "placeholder"):
                            setattr(self, attr_name, val)
                            logger.info("Loaded secret %s from Secret Manager", secret_id)
                    except Exception as exc:
                        logger.debug("Secret Manager fetch skipped for %s: %s", secret_id, exc)
        except Exception as exc:
            logger.debug("SecretManagerServiceClient unavailable: %s", exc)


_settings: Optional[BridgeConfig] = None


def get_settings() -> BridgeConfig:
    global _settings
    if _settings is None:
        _settings = BridgeConfig()
        _settings.resolve_secrets_from_manager()
    return _settings


def reset_settings(new_settings: Optional[BridgeConfig] = None) -> None:
    global _settings
    _settings = new_settings
