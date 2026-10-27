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

    # Origin selection: an upstream service explains a downstream one only if its alert or
    # telemetry onset is no later than the downstream onset plus this tolerance. Alert policies
    # use 60s duration + 60s alignment, so alert open times skew by up to ~2 min; the extra
    # margin allows a gradual upstream ramp to push the downstream over its threshold first.
    origin_onset_tolerance_seconds: float = Field(default=300.0, alias="ORIGIN_ONSET_TOLERANCE_SECONDS", ge=0)
    # Cool-down outcome: when all alerts in a ticketed group close and the cool-down
    # passes with no recurrence, resolve the ServiceNow ticket (true) or leave it
    # In Progress with a "please verify and resolve" work note (false, default).
    # Monitoring recovery is not proof of a fix (low traffic can mask a live fault).
    auto_resolve_on_recovery: bool = Field(default=False, alias="AUTO_RESOLVE_ON_RECOVERY")
    # Rollback target: the revision(s) that received requests on the changed service in this
    # many minutes before the new revision was created (Cloud Monitoring request_count by
    # revision). A rollback target is named only when exactly one revision served.
    rollback_traffic_lookback_minutes: int = Field(default=30, alias="ROLLBACK_TRAFFIC_LOOKBACK_MINUTES", ge=1)
    # Recovery note: request_count of the group's Cloud Run services is read from this many
    # seconds before the last alert closed until the note is written, to state whether the
    # close coincided with traffic stopping (missing data).
    recovery_traffic_lookback_seconds: int = Field(default=300, alias="RECOVERY_TRAFFIC_LOOKBACK_SECONDS", ge=0)
    # Apigee access-log entries read per investigation (oldest first). Live S3 had 634
    # entries in the -15/+5 min window. When the cap is hit, the report says so, no revision
    # switch is evaluated and log-derived counts are stated as lower bounds.
    apigee_access_log_max_entries: int = Field(default=5000, alias="APIGEE_ACCESS_LOG_MAX_ENTRIES", ge=1)
    # Entries per Cloud Logging entries.list page. Each page is one call against the project's
    # 'Read requests per minute' quota. The API default is 50, so 651 access-log entries cost 14
    # read requests and a 5000-entry cap up to 100. The API maximum is 1000.
    logging_read_page_size: int = Field(default=1000, alias="LOGGING_READ_PAGE_SIZE", ge=1, le=1000)
    # Telemetry reads that fail with a quota error (HTTP 429 / RESOURCE_EXHAUSTED) are retried
    # up to this many attempts in total, with full-jitter exponential backoff: before retry n
    # the wait is uniform in [0, min(MAX, BASE * 2**(n-1))] seconds. The Logging read quota is
    # per minute and shared with other workloads in the project (live S3, 2026-10-27 13:03Z).
    # After the last attempt the data is reported as unavailable, never as a negative finding.
    gcp_read_retry_attempts: int = Field(default=5, alias="GCP_READ_RETRY_ATTEMPTS", ge=1)
    gcp_read_retry_base_seconds: float = Field(default=2.0, alias="GCP_READ_RETRY_BASE_SECONDS", ge=0)
    gcp_read_retry_max_seconds: float = Field(default=30.0, alias="GCP_READ_RETRY_MAX_SECONDS", ge=0)
    # Latency reports: below this many requests/min on the origin over the investigation window
    # the report says the latency rests on little traffic (it may be a cold start) instead of
    # calling it a service-wide degradation. Live 2026-10-27: the stray cold-start ticket
    # INC0010327 had 1.9 requests/min (32 in 17.2 min); S2's origin ata-inventory-api ~35
    # requests/min (627 in ~18 min).
    low_traffic_rpm: float = Field(default=5.0, alias="LOW_TRAFFIC_RPM", gt=0)
    # Late alerts on ticketed groups: an alert of a signal type the group does not have yet
    # (e.g. 5xx joining a latency group) whose onset is more than this many seconds after the
    # group's first alert opens a new group instead of joining a ticketed group. Same-service
    # latency/5xx score 35 (topology) + 30 (causal) = the 65-pt attach threshold, so the
    # temporal score alone never separates them. Default: the 10-min temporal scoring horizon.
    # Live S5 (one incident) had latency -> 5xx 413 s apart; keep the window above that.
    late_new_signal_max_onset_gap_seconds: float = Field(
        default=600.0, alias="LATE_NEW_SIGNAL_MAX_ONSET_GAP_SECONDS", ge=0
    )

    # Stuck investigations: a group in 'investigating' with no ServiceNow sys_id whose last
    # settle/max-wait wake attempt is more than this many seconds old is treated as stuck (its
    # wake exhausted ata-timer-queue's retries). Every retry of a failing wake re-stamps the
    # attempt, so this must exceed timer_queue_max_backoff plus the agent request timeout
    # (Cloud Run default 300s), or a recovery can start a second investigation beside a live one.
    investigation_stale_seconds: float = Field(default=3600.0, alias="INVESTIGATION_STALE_SECONDS", gt=0)
    # Recoveries (re-enqueued settle wakes) per stuck group. When one more would be needed the
    # group is marked degraded (ERROR) and new alerts open a new group instead of joining it.
    investigation_max_recoveries: int = Field(default=3, alias="INVESTIGATION_MAX_RECOVERIES", ge=1)

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
