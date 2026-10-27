import logging
import os
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class AgentConfig(BaseSettings):
    """Agent service configuration externalizing all environment variables."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_id: str = Field(default="cloud-assist-fde-2", alias="PROJECT_ID")
    name_prefix: str = Field(default="ata", alias="NAME_PREFIX")
    region: str = Field(default="us-west1", alias="REGION")
    environment: str = Field(default="prod", alias="ENVIRONMENT")

    # Cloud Storage Incidents Bucket & Mount Path
    incidents_bucket: Optional[str] = Field(default=None, alias="INCIDENTS_BUCKET")
    incident_store_path: str = Field(default="/knowledge/incident-manager", alias="INCIDENT_STORE_PATH")
    fallback_store_path: str = Field(default="/tmp/knowledge/incident-manager", alias="FALLBACK_STORE_PATH")

    # Bridge & Agent URLs
    bridge_url: str = Field(default="http://localhost:8080", alias="BRIDGE_URL")
    agent_url: str = Field(default="http://localhost:8000", alias="AGENT_URL")

    # Cloud Tasks Timers Configuration
    location: str = Field(default="us-west1", alias="LOCATION")
    timer_queue_name: str = Field(default="ata-timer-queue", alias="TIMER_QUEUE_NAME")
    service_account_email: str = Field(
        default="ata-agent-sa@cloud-assist-fde-2.iam.gserviceaccount.com",
        alias="SERVICE_ACCOUNT_EMAIL",
    )

    # Model Configuration
    model_name: str = Field(default="gemini-3.8-flash", alias="MODEL_NAME")

    is_local: bool = Field(default=False, alias="IS_LOCAL")

    # Cool-down outcome: when all alerts in a ticketed group close and the cool-down
    # passes with no recurrence, resolve the ServiceNow ticket (true) or leave it
    # In Progress with a "please verify and resolve" work note (false, default).
    # Monitoring recovery is not proof of a fix (low traffic can mask a live fault).
    auto_resolve_on_recovery: bool = Field(default=False, alias="AUTO_RESOLVE_ON_RECOVERY")

    @property
    def effective_incidents_bucket(self) -> str:
        return self.incidents_bucket or f"{self.project_id}-{self.name_prefix}-incidents"

    def get_effective_store_path(self) -> Path:
        """Return primary store path if accessible/writable, otherwise fallback."""
        primary = Path(self.incident_store_path)
        try:
            if primary.exists() and os_is_writable(primary):
                return primary
            if not primary.exists():
                primary.mkdir(parents=True, exist_ok=True)
                return primary
        except Exception as exc:
            logger.error("Primary store path %s unavailable (%s); using fallback %s", primary, exc, self.fallback_store_path)

        fallback = Path(self.fallback_store_path)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def os_is_writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


_agent_settings: Optional[AgentConfig] = None


def get_agent_settings() -> AgentConfig:
    global _agent_settings
    if _agent_settings is None:
        _agent_settings = AgentConfig()
    return _agent_settings


def reset_agent_settings(settings: Optional[AgentConfig] = None) -> None:
    global _agent_settings
    _agent_settings = settings


# Alias for compatibility with get_settings
get_settings = get_agent_settings
