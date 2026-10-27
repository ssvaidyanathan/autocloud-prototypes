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
    # Numeric GCP project number: builds the deterministic Cloud Run URL
    # https://<prefix>-tools-<PROJECT_NUMBER>.<REGION>.run.app, an accepted ID-token audience.
    project_number: Optional[str] = Field(default=None, alias="PROJECT_NUMBER")
    # Apigee environment whose deployments get_change_timeline reads. No default: without it
    # the Apigee deployment history is reported as failed, never guessed.
    apigee_env: Optional[str] = Field(default=None, alias="APIGEE_ENV")

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


_tools_config: Optional[ToolsConfig] = None


def get_tools_config() -> ToolsConfig:
    global _tools_config
    if _tools_config is None:
        _tools_config = ToolsConfig()
    return _tools_config


def reset_tools_config(config: Optional[ToolsConfig] = None) -> None:
    global _tools_config
    _tools_config = config
