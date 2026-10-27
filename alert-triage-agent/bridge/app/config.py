import logging
import os
import re
from typing import Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Same rule as the Terraform name_prefix validation (and the agent/tools configs).
NAME_PREFIX_PATTERN = r"[a-z][a-z0-9]{1,9}"


class BridgeConfig(BaseSettings):
    """Bridge service configuration externalizing all environment variables."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment identity. No defaults: PROJECT_ID and REGION are required unless IS_LOCAL;
    # NAME_PREFIX is always required and follows the Terraform rule ^[a-z][a-z0-9]{1,9}$.
    project_id: Optional[str] = Field(default=None, alias="PROJECT_ID")
    # Numeric GCP project number. Used to build the bridge's deterministic Cloud Run URL
    # (https://<prefix>-bridge-<number>.<region>.run.app), an accepted ID-token audience.
    # Required when not local: startup fails and auth returns 500 without it.
    project_number: Optional[str] = Field(default=None, alias="PROJECT_NUMBER")
    # This bridge's own Cloud Run URL: the service's status.url, e.g. the hash-style
    # https://<prefix>-bridge-<hash>-<region-code>.a.run.app. ata-agent mints its ID tokens for
    # the URL it calls (agent BRIDGE_URL, set from this status.url), so it is an accepted
    # audience. Required when not local. bridge/scripts/deploy.sh sets it.
    bridge_url: Optional[str] = Field(default=None, alias="BRIDGE_URL")
    name_prefix: str = Field(alias="NAME_PREFIX")
    region: Optional[str] = Field(default=None, alias="REGION")
    environment: str = Field(default="prod", alias="ENVIRONMENT")

    # Cloud Storage Incidents Bucket
    incidents_bucket: Optional[str] = Field(default=None, alias="INCIDENTS_BUCKET")
    incident_store_path: Optional[str] = Field(default=None, alias="INCIDENT_STORE_PATH")

    # Agent Service
    # The agent's URL (Cloud Tasks target and OIDC audience). Required when not local: no silent
    # localhost fallback outside IS_LOCAL=true (bridge/scripts/deploy.sh sets it).
    agent_url: Optional[str] = Field(default=None, alias="AGENT_URL")

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

    @field_validator("name_prefix")
    @classmethod
    def _validate_name_prefix(cls, value: str) -> str:
        if not re.fullmatch(NAME_PREFIX_PATTERN, value or ""):
            raise ValueError(f"NAME_PREFIX {value!r} must match ^{NAME_PREFIX_PATTERN}$ (the Terraform name_prefix rule)")
        return value

    @model_validator(mode="after")
    def _require_project_and_region(self) -> "BridgeConfig":
        # IS_LOCAL turns on mock tokens and local fallbacks; never on Cloud Run (K_SERVICE is set there).
        if self.is_local and os.getenv("K_SERVICE"):
            raise ValueError("IS_LOCAL=true is not allowed on Cloud Run (K_SERVICE is set)")
        for field_name in ("project_id", "region", "agent_url"):
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                setattr(self, field_name, None)
        if self.is_local and self.agent_url is None:
            self.agent_url = "http://localhost:8080"  # local development only
        missing = [env for env, value in (("PROJECT_ID", self.project_id), ("REGION", self.region)) if not value]
        if missing and not self.is_local:
            raise ValueError(f"required setting(s) not set: {', '.join(missing)} (only IS_LOCAL=true may omit them)")
        return self

    @field_validator("project_number", mode="before")
    @classmethod
    def _validate_project_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if not re.fullmatch(r"[0-9]+", str(value)):
            raise ValueError(f"PROJECT_NUMBER must be the numeric GCP project number, got {value!r}")
        return str(value)

    @field_validator("verifier_sa", mode="before")
    @classmethod
    def _validate_verifier_sa(cls, value: Optional[str]) -> Optional[str]:
        """VERIFIER_SA: comma-separated caller emails, lowercased (Google ID-token email claims
        are lower case). A non-email entry fails at startup instead of a silent 403 later."""
        if value is None or str(value).strip() == "":
            return None
        entries = [e.strip().lower() for e in str(value).split(",") if e.strip()]
        bad = [e for e in entries if not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+", e)]
        if bad:
            raise ValueError(f"VERIFIER_SA entries must be email addresses (comma-separated), got {bad!r}")
        return ",".join(entries) if entries else None

    @field_validator("bridge_url", mode="before")
    @classmethod
    def _validate_bridge_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        url = str(value).rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+", url):
            raise ValueError(f"BRIDGE_URL must be the bridge's https Cloud Run URL without a path, got {value!r}")
        return url

    def missing_required_settings(self) -> list[str]:
        """Settings without which a non-local bridge cannot authenticate callers correctly."""
        if self.is_local:
            return []
        required = {"PROJECT_NUMBER": self.project_number, "BRIDGE_URL": self.bridge_url, "AGENT_URL": self.agent_url}
        return [name for name, value in required.items() if value is None]

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
