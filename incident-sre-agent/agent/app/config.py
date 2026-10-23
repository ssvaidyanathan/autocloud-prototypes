from functools import lru_cache
from typing import Any, Optional, Tuple, Type

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class CustomEnvSettingsSource(EnvSettingsSource):
    def decode_complex_value(self, field_name: str, field: Any, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except Exception:
            if isinstance(value, str) and field_name == "allowed_callback_hosts":
                return [h.strip() for h in value.split(",") if h.strip()]
            raise


class CustomDotEnvSettingsSource(DotEnvSettingsSource):
    def decode_complex_value(self, field_name: str, field: Any, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except Exception:
            if isinstance(value, str) and field_name == "allowed_callback_hosts":
                return [h.strip() for h in value.split(",") if h.strip()]
            raise


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


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    port: int = 8080
    gcp_project: Optional[str] = None
    gcp_region: str = "us-central1"
    vertex_location: str = "global"
    model_dispatcher: str
    model_specialist: str
    model_synthesizer: str
    gemini_pro_model: Optional[str] = None
    gemini_flash_model: Optional[str] = None
    app_env: str = "production"
    name_prefix: str = "isa"
    service_name: str = "sre-agent"
    allowed_callback_hosts: list[str] = Field(default_factory=lambda: ["*.run.app"])
    timeout_seconds: int = 600
    investigation_timeout_seconds: Optional[int] = None
    # Not yet enforced; reserved for a cap on all specialist work in one investigation.
    investigation_budget_seconds: float = 300.0
    specialist_timeout_seconds: float = 120.0
    max_review_rounds: int = 1
    specialist_query_limit: int = 5
    specialist_max_entries: int = 50
    specialist_max_llm_calls: int = 8
    specialist_max_tool_calls: int = 5
    max_tool_calls_per_specialist: int = 25
    default_lookback_minutes: int = 60
    max_query_window_hours: int = 24
    callback_connect_timeout_seconds: float = 5.0
    callback_read_timeout_seconds: float = 45.0
    callback_max_retries: int = 3
    callback_backoff_factor: float = 1.5
    onset_window_minutes: int = 60
    onset_pre_buffer_minutes: int = 10
    revision_causality_window_minutes: int = 30
    dependency_fanout_max_depth: int = 2
    dependency_max_services: int = 3
    prior_incident_quiescent_minutes: int = 5

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    @model_validator(mode="before")
    @classmethod
    def apply_model_aliases_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            lookup = {k.lower(): k for k in data.keys()}
            pro_key = lookup.get("gemini_pro_model")
            flash_key = lookup.get("gemini_flash_model")
            pro = data.get(pro_key) if pro_key else None
            flash = data.get(flash_key) if flash_key else None

            if pro and isinstance(pro, str) and pro.strip():
                if not data.get("model_dispatcher"):
                    data["model_dispatcher"] = pro
                if not data.get("model_synthesizer"):
                    data["model_synthesizer"] = pro
            if flash and isinstance(flash, str) and flash.strip():
                if not data.get("model_specialist"):
                    data["model_specialist"] = flash
        return data

    @model_validator(mode="after")
    def apply_model_aliases_and_resolve_project(self) -> "AgentConfig":
        if self.gemini_pro_model:
            self.model_dispatcher = self.gemini_pro_model
            self.model_synthesizer = self.gemini_pro_model
        if self.gemini_flash_model:
            self.model_specialist = self.gemini_flash_model

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
        return self

    @property
    def is_local(self) -> bool:
        return self.app_env.lower() == "local"

    @property
    def effective_timeout_seconds(self) -> int:
        return (
            self.investigation_timeout_seconds
            if self.investigation_timeout_seconds is not None
            else self.timeout_seconds
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CustomEnvSettingsSource(settings_cls),
            CustomDotEnvSettingsSource(settings_cls),
            file_secret_settings,
        )


# Alias for flexibility
AgentSettings = AgentConfig


@lru_cache
def get_settings() -> AgentConfig:
    return AgentConfig()
