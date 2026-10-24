from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ToolsConfig(BaseSettings):
    """Configuration for ata-tools FastMCP server."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_id: str = Field(default="cloud-assist-fde-2", alias="PROJECT_ID")
    name_prefix: str = Field(default="ata", alias="NAME_PREFIX")
    region: str = Field(default="us-west1", alias="REGION")
    environment: str = Field(default="prod", alias="ENVIRONMENT")
    apigee_org: str = Field(default="cloud-assist-fde-2", alias="APIGEE_ORG")

    # Server settings
    port: int = Field(default=8080, alias="PORT")
    is_local: bool = Field(default=False, alias="IS_LOCAL")

    # Authentication settings
    enforce_auth: bool = Field(default=False, alias="ENFORCE_AUTH")
    verify_oidc: bool = Field(default=False, alias="VERIFY_OIDC")
    expected_audience: Optional[str] = Field(default=None, alias="EXPECTED_AUDIENCE")
    allowed_service_accounts: Optional[str] = Field(default=None, alias="ALLOWED_SERVICE_ACCOUNTS")


_tools_config: Optional[ToolsConfig] = None


def get_tools_config() -> ToolsConfig:
    global _tools_config
    if _tools_config is None:
        _tools_config = ToolsConfig()
    return _tools_config


def reset_tools_config(config: Optional[ToolsConfig] = None) -> None:
    global _tools_config
    _tools_config = config
