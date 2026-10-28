import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Same rule as the Terraform name_prefix validation.
LOCAL_BRIDGE_URL = "http://localhost:8080"
LOCAL_AGENT_URL = "http://localhost:8000"
NAME_PREFIX_PATTERN = r"[a-z][a-z0-9]{1,9}"


class AgentConfig(BaseSettings):
    """Agent service configuration externalizing all environment variables."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment identity. No defaults: PROJECT_ID and REGION are required unless IS_LOCAL;
    # NAME_PREFIX is always required and follows the Terraform rule ^[a-z][a-z0-9]{1,9}$.
    project_id: Optional[str] = Field(default=None, alias="PROJECT_ID")
    name_prefix: str = Field(alias="NAME_PREFIX")
    region: Optional[str] = Field(default=None, alias="REGION")
    environment: str = Field(default="prod", alias="ENVIRONMENT")

    # Cloud Storage Incidents Bucket & Mount Path
    incidents_bucket: Optional[str] = Field(default=None, alias="INCIDENTS_BUCKET")
    incident_store_path: str = Field(default="/knowledge/incident-manager", alias="INCIDENT_STORE_PATH")
    fallback_store_path: str = Field(default="/tmp/knowledge/incident-manager", alias="FALLBACK_STORE_PATH")

    # Bridge & Agent URLs. No defaults when not local: a non-local agent without them would post
    # ticket instructions and timer wakes to localhost and fail far from the cause, so they are
    # required like TOOLS_URL (startup fails; agent/scripts/deploy.sh sets both). IS_LOCAL=true
    # falls back to the local dev ports below.
    bridge_url: Optional[str] = Field(default=None, alias="BRIDGE_URL")
    agent_url: Optional[str] = Field(default=None, alias="AGENT_URL")

    # Cloud Tasks timers. Derived when unset: LOCATION = REGION,
    # TIMER_QUEUE_NAME = {NAME_PREFIX}-timer-queue,
    # SERVICE_ACCOUNT_EMAIL = {NAME_PREFIX}-agent-sa@{PROJECT_ID}.iam.gserviceaccount.com.
    location: Optional[str] = Field(default=None, alias="LOCATION")
    timer_queue_name: Optional[str] = Field(default=None, alias="TIMER_QUEUE_NAME")
    service_account_email: Optional[str] = Field(default=None, alias="SERVICE_ACCOUNT_EMAIL")

    # Gemini gray-zone correlation (Vertex AI). Model tuning values, overridable per deploy.
    model_name: str = Field(default="gemini-3.8-flash", alias="MODEL_NAME")
    gemini_location: str = Field(default="global", alias="GEMINI_LOCATION")
    # Bound of one gray-zone Gemini call; on timeout the deterministic fallback decides (WARNING).
    # The call runs outside the global ingest lock (grouping/manager.py), so this bounds one alert.
    gemini_timeout_seconds: float = Field(default=20.0, alias="GEMINI_TIMEOUT_SECONDS", gt=0, le=120)

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
    # Change attribution: a Cloud Run deploy in the change window is the cause (change_induced,
    # rollback) only if (a) the alert onset is at most CHANGE_ONSET_WINDOW_MINUTES after it, or
    # (b) the new revision had no healthy period before the onset: not one healthy minute with
    # requests and at least one failing one (Cloud Monitoring per revision, per minute). A span of
    # at least CHANGE_HEALTHY_PERIOD_MINUTES with every minute with requests healthy is a healthy period. Without that data only (a) applies, and the
    # ticket says so. Otherwise the deploy is listed under "Recent changes (likely unrelated)".
    # Live INC0010343: a re-pin 59 min before an S1 fault, healthy 21:52-22:44Z, was blamed
    # under the old any-deploy-in-window rule.
    change_onset_window_minutes: int = Field(default=15, alias="CHANGE_ONSET_WINDOW_MINUTES", ge=1, le=120)
    change_healthy_period_minutes: int = Field(default=10, alias="CHANGE_HEALTHY_PERIOD_MINUTES", ge=1, le=120)
    # A healthy period also needs requests in at least this many of its minutes ("a sustained
    # period of normal traffic"): two requests 40 min apart are not one. Only healthy minutes count:
    # rule (b) needs zero healthy minutes (and a failing one); 1+ healthy minutes without a
    # qualifying span leave the period undetermined, so only rule (a) applies, stated in the ticket.
    change_healthy_min_active_minutes: int = Field(default=3, alias="CHANGE_HEALTHY_MIN_ACTIVE_MINUTES", ge=1, le=120)
    # Healthy minute (rule b): HTTP 5xx share of the revision's requests under this fraction and
    # its mean request latency under CHANGE_HEALTHY_MAX_LATENCY_MS. These are agent heuristics,
    # NOT the alert policies (infra/terraform/observability: 5xx rate > 0.05/s, p99 latency >
    # 2000 ms), so a revision can count as healthy in a minute in which its alert condition held.
    change_healthy_max_5xx_share: float = Field(default=0.05, alias="CHANGE_HEALTHY_MAX_5XX_SHARE", gt=0, le=1)
    change_healthy_max_latency_ms: float = Field(default=2000.0, alias="CHANGE_HEALTHY_MAX_LATENCY_MS", gt=0, le=300000)
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
    # Minimum spacing between two recovery attempts on one group. A trigger (alert ingest or
    # watchdog) sooner than this after the last recorded attempt starts no attempt and does not
    # degrade, so a burst of triggers during an outage uses one attempt, not all of them.
    investigation_recovery_min_interval_seconds: float = Field(
        default=300.0, alias="INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS", gt=0
    )

    # ata-tools (FastMCP) service URL, e.g. https://<prefix>-tools-<PROJECT_NUMBER>.<REGION>.run.app.
    # The change specialist calls its get_change_timeline tool over MCP streamable HTTP with an
    # ID token whose audience is this URL. No default: required when not local (startup fails
    # without it). Must be https with no path; set by deploy.sh from the deployed service.
    tools_url: Optional[str] = Field(default=None, alias="TOOLS_URL")
    # Per-attempt HTTP timeout of an ata-tools call. get_change_timeline lists every Cloud Run
    # service's revisions plus the Apigee deployments (15 s per upstream call inside ata-tools).
    tools_timeout_seconds: float = Field(default=60.0, alias="TOOLS_TIMEOUT_SECONDS", gt=0, le=300)
    # Attempts in total for an ata-tools call on transient failures (connection error, timeout,
    # HTTP 429/5xx), with exponential backoff (TOOLS_RETRY_BASE_SECONDS * 2**(n-1), capped at 10 s).
    tools_retry_attempts: int = Field(default=3, alias="TOOLS_RETRY_ATTEMPTS", ge=1, le=5)
    tools_retry_base_seconds: float = Field(default=1.0, alias="TOOLS_RETRY_BASE_SECONDS", ge=0, le=10)

    @field_validator("tools_url", mode="before")
    @classmethod
    def _validate_tools_url(cls, value: Optional[str]) -> Optional[str]:
        """https://host[:port] only (an optional trailing slash is dropped); empty means unset."""
        if value is None or str(value).strip() == "":
            return None
        url = str(value).strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.fragment:
            raise ValueError(f"TOOLS_URL must be an https URL with no path (https://<host>), got {value!r}")
        return url

    def missing_required_settings(self) -> list[str]:
        """Settings without which a non-local agent cannot run a complete investigation."""
        if self.is_local:
            return []
        required = {"TOOLS_URL": self.tools_url, "BRIDGE_URL": self.bridge_url, "AGENT_URL": self.agent_url}
        return [name for name, value in required.items() if value is None]

    @field_validator("name_prefix")
    @classmethod
    def _validate_name_prefix(cls, value: str) -> str:
        if not re.fullmatch(NAME_PREFIX_PATTERN, value or ""):
            raise ValueError(f"NAME_PREFIX {value!r} must match ^{NAME_PREFIX_PATTERN}$ (the Terraform name_prefix rule)")
        return value

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.@_-]*", value or ""):
            raise ValueError(f"MODEL_NAME {value!r} is not a Vertex AI model id (e.g. gemini-2.5-flash)")
        return value

    @field_validator("gemini_location")
    @classmethod
    def _validate_gemini_location(cls, value: str) -> str:
        if not re.fullmatch(r"global|[a-z]+-[a-z]+[0-9]{1,2}", value or ""):
            raise ValueError(f"GEMINI_LOCATION {value!r} must be 'global' or a region such as us-central1")  # prefix-ok: example in an error message
        return value

    @model_validator(mode="after")
    def _require_and_derive(self) -> "AgentConfig":
        # IS_LOCAL turns on mock tokens and local fallbacks; never on Cloud Run (K_SERVICE is set there).
        if self.is_local and os.getenv("K_SERVICE"):
            raise ValueError("IS_LOCAL=true is not allowed on Cloud Run (K_SERVICE is set)")
        for field_name in ("project_id", "region", "location", "timer_queue_name", "service_account_email",
                           "bridge_url", "agent_url"):
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                setattr(self, field_name, None)
        missing = [env for env, value in (("PROJECT_ID", self.project_id), ("REGION", self.region)) if not value]
        if missing and not self.is_local:
            raise ValueError(f"required setting(s) not set: {', '.join(missing)} (only IS_LOCAL=true may omit them)")
        if self.is_local:
            # Local development only (uvicorn on the default ports).
            self.bridge_url = self.bridge_url or LOCAL_BRIDGE_URL
            self.agent_url = self.agent_url or LOCAL_AGENT_URL
        if self.location is None:
            self.location = self.region
        if self.timer_queue_name is None:
            self.timer_queue_name = f"{self.name_prefix}-timer-queue"
        if self.service_account_email is None and self.project_id:
            self.service_account_email = f"{self.name_prefix}-agent-sa@{self.project_id}.iam.gserviceaccount.com"
        return self

    @property
    def effective_incidents_bucket(self) -> str:
        if self.incidents_bucket:
            return self.incidents_bucket
        if not self.project_id:
            raise ValueError("INCIDENTS_BUCKET or PROJECT_ID must be set to name the incidents bucket")
        return f"{self.project_id}-{self.name_prefix}-incidents"

    @property
    def apigee_access_log_id(self) -> str:
        """Cloud Logging log id written by the Apigee proxies' MessageLogging policy."""
        return f"{self.name_prefix}-apigee-access"

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
