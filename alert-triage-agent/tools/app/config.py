import re
from typing import Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Same rule as the Terraform name_prefix validation.
NAME_PREFIX_PATTERN = r"[a-z][a-z0-9]{1,9}"


class ToolsConfig(BaseSettings):
    """Configuration for ata-tools FastMCP server."""
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
    # Apigee organization and environment whose deployments get_change_timeline reads. No
    # defaults and no fallback to PROJECT_ID: without them the Apigee deployment history is
    # reported as a failed source naming the missing setting, never guessed.
    apigee_org: Optional[str] = Field(default=None, alias="APIGEE_ORG")
    apigee_env: Optional[str] = Field(default=None, alias="APIGEE_ENV")

    # Server settings
    port: int = Field(default=8080, alias="PORT")
    is_local: bool = Field(default=False, alias="IS_LOCAL")

    # Authentication settings
    enforce_auth: bool = Field(default=False, alias="ENFORCE_AUTH")
    verify_oidc: bool = Field(default=False, alias="VERIFY_OIDC")
    expected_audience: Optional[str] = Field(default=None, alias="EXPECTED_AUDIENCE")
    allowed_service_accounts: Optional[str] = Field(default=None, alias="ALLOWED_SERVICE_ACCOUNTS")
    # Numeric GCP project number: builds the deterministic Cloud Run URL
    # https://<prefix>-tools-<PROJECT_NUMBER>.<REGION>.run.app, an accepted ID-token audience.
    project_number: Optional[str] = Field(default=None, alias="PROJECT_NUMBER")

    @property
    def effective_expected_audiences(self) -> list[str]:
        """ID-token audiences accepted: EXPECTED_AUDIENCE (comma-separated URLs, e.g. the URL the
        agent calls as TOOLS_URL) plus the deterministic <prefix>-tools URL when PROJECT_NUMBER is set."""
        audiences = [a.strip().rstrip("/") for a in (self.expected_audience or "").split(",") if a.strip()]
        if self.project_number:
            derived = f"https://{self.name_prefix}-tools-{self.project_number}.{self.region}.run.app"
            if derived not in audiences:
                audiences.append(derived)
        return audiences

    @property
    def effective_allowed_service_accounts(self) -> list[str]:
        """Callers accepted: ALLOWED_SERVICE_ACCOUNTS (comma-separated), else the agent SA."""
        explicit = [s.strip() for s in (self.allowed_service_accounts or "").split(",") if s.strip()]
        return explicit or [f"{self.name_prefix}-agent-sa@{self.project_id}.iam.gserviceaccount.com"]


    @field_validator("name_prefix")
    @classmethod
    def _validate_name_prefix(cls, value: str) -> str:
        if not re.fullmatch(NAME_PREFIX_PATTERN, value or ""):
            raise ValueError(f"NAME_PREFIX {value!r} must match ^{NAME_PREFIX_PATTERN}$ (the Terraform name_prefix rule)")
        return value

    @model_validator(mode="after")
    def _require_and_derive(self) -> "ToolsConfig":
        for field_name in ("project_id", "region", "apigee_org", "apigee_env"):
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                setattr(self, field_name, None)
        missing = [env for env, value in (("PROJECT_ID", self.project_id), ("REGION", self.region)) if not value]
        if missing and not self.is_local:
            raise ValueError(f"required setting(s) not set: {', '.join(missing)} (only IS_LOCAL=true may omit them)")
        return self

    @property
    def apigee_access_log_id(self) -> str:
        """Cloud Logging log id written by the Apigee proxies' MessageLogging policy."""
        return f"{self.name_prefix}-apigee-access"


_tools_config: Optional[ToolsConfig] = None


def get_tools_config() -> ToolsConfig:
    global _tools_config
    if _tools_config is None:
        _tools_config = ToolsConfig()
    return _tools_config


def reset_tools_config(config: Optional[ToolsConfig] = None) -> None:
    global _tools_config
    _tools_config = config


# Same accessor name as the agent and bridge configs (used by app/contracts/alert.py).
get_settings = get_tools_config
