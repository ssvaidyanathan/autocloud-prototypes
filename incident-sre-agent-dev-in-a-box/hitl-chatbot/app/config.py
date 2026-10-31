"""Configuration settings for HITL Chatbot."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Chatbot runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Agent & LLM settings
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    prompts_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent / "prompts" / "chatbot.yaml",
        alias="PROMPTS_PATH",
    )

    # Security & Tokens
    hmac_secret_key: str = Field(
        default="dev-hitl-chatbot-hmac-secret-key-32b",
        alias="HMAC_SECRET_KEY",
    )
    token_expiry_seconds: int = Field(default=600, alias="TOKEN_EXPIRY_SECONDS")

    # Upstream Integrations
    mcp_server_url: str = Field(default="http://localhost:8000", alias="MCP_SERVER_URL")
    bridge_url: str = Field(default="http://localhost:8080", alias="BRIDGE_URL")
    project_id: Optional[str] = Field(default=None, alias="PROJECT_ID")
    service_url: Optional[str] = Field(default=None, alias="SERVICE_URL")

    # Jira defaults
    jira_project_key: str = Field(default="AMJTEST", alias="JIRA_PROJECT_KEY")
    jira_issue_type: str = Field(default="Task", alias="JIRA_ISSUE_TYPE")

    # Slack integration
    slack_bot_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("SLACK_BOT_TOKEN", "HITL_SLACK_BOT_TOKEN"),
    )
    slack_signing_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("SLACK_SIGNING_SECRET", "HITL_SLACK_SIGNING_SECRET"),
    )

    # Auth & OIDC
    app_env: str = Field(default="prod", alias="APP_ENV")
    auth_disabled: bool = Field(default=False, alias="AUTH_DISABLED")
    authorized_caller_service_accounts: str = Field(
        default="", alias="AUTHORIZED_CALLER_SERVICE_ACCOUNTS"
    )
    expected_audience: Optional[str] = Field(default=None, alias="EXPECTED_AUDIENCE")
    bridge_caller_service_accounts: str = Field(default="", alias="BRIDGE_CALLER_SERVICE_ACCOUNTS")

    def get_authorized_caller_accounts(self) -> set[str]:
        """Return configured caller service account emails or prefixes."""
        accounts: set[str] = set()
        if self.authorized_caller_service_accounts:
            for sa in self.authorized_caller_service_accounts.split(","):
                if sa.strip():
                    accounts.add(sa.strip())
        return accounts

    def get_bridge_caller_accounts(self) -> set[str]:
        """Return configured bridge caller service account emails or prefixes."""
        accounts: set[str] = set()
        if self.bridge_caller_service_accounts:
            for sa in self.bridge_caller_service_accounts.split(","):
                if sa.strip():
                    accounts.add(sa.strip())
        return accounts


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
