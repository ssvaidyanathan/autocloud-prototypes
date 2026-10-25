import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def synthesis_thinking_enabled(thinking_budget: Optional[int]) -> bool:
    """A None or negative synthesis thinking budget disables thinkingConfig."""
    return thinking_budget is not None and thinking_budget >= 0


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
    vertex_ai_location: str = "us-central1"
    gemini_model: str
    # Gemini synthesis generation budget. Thinking tokens count against
    # maxOutputTokens on thinking models, so both are tunable together.
    synthesis_max_output_tokens: int = Field(default=8192, gt=0)
    # Coupled to gemini_model: non-thinking models (e.g. the 2.0 family) reject
    # thinkingConfig outright, and gemini-2.5-pro rejects budgets below 128
    # (so 0 is invalid there). Set to None or a negative value (e.g.
    # SYNTHESIS_THINKING_BUDGET=-1) to omit thinkingConfig from the request.
    synthesis_thinking_budget: Optional[int] = Field(default=1024)

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
    def validate_synthesis_budget(self) -> "AgentConfig":
        budget = self.synthesis_thinking_budget
        if (
            budget is not None
            and synthesis_thinking_enabled(budget)
            and budget >= self.synthesis_max_output_tokens
        ):
            raise ValueError(
                "synthesis_thinking_budget must be less than synthesis_max_output_tokens "
                "when thinking is enabled (thinking tokens count against the output "
                "budget, so the response would always be truncated)"
            )
        return self

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
