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
    # Apigee organization. Defaults to PROJECT_ID (an Apigee X org is named after its host
    # project); set APIGEE_ORG when the org lives in another project.
    apigee_org: Optional[str] = Field(default=None, alias="APIGEE_ORG")
    # Apigee environment the proxies are deployed to (e.g. prod). No default: the change
    # timeline reports Apigee deployments as unavailable, naming APIGEE_ENV, until it is set.
    apigee_env: Optional[str] = Field(default=None, alias="APIGEE_ENV")

    # Server settings
    port: int = Field(default=8080, alias="PORT")
    is_local: bool = Field(default=False, alias="IS_LOCAL")

    # Authentication settings
    enforce_auth: bool = Field(default=False, alias="ENFORCE_AUTH")
    verify_oidc: bool = Field(default=False, alias="VERIFY_OIDC")
    expected_audience: Optional[str] = Field(default=None, alias="EXPECTED_AUDIENCE")
    allowed_service_accounts: Optional[str] = Field(default=None, alias="ALLOWED_SERVICE_ACCOUNTS")


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
        if self.apigee_org is None:
            self.apigee_org = self.project_id
        return self

    @property
    def apigee_access_log_id(self) -> str:
        """Cloud Logging log id written by the Apigee proxies' MessageLogging policy."""
        return f"{self.name_prefix}-apigee-access"

    def require_apigee_env(self) -> str:
        if not self.apigee_env:
            raise ValueError("APIGEE_ENV is not set; it names the Apigee environment the proxies are deployed to")
        return self.apigee_env


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
