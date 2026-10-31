"""Configuration settings for Deployment Context MCP Server."""

from __future__ import annotations

import os
from typing import Optional, Set

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    FIXTURES_DIR: str = "fixtures"
    FIXTURES_GCS_URI: Optional[str] = None

    AUTH_DISABLED: bool = False
    AUTHORIZED_CALLER_SERVICE_ACCOUNTS: str = ""
    EXPECTED_AUDIENCE: Optional[str] = None

    def get_authorized_accounts(self) -> Set[str]:
        """Return the set of authorized caller service account email addresses."""
        if not self.AUTHORIZED_CALLER_SERVICE_ACCOUNTS:
            return set()
        return {
            sa.strip() for sa in self.AUTHORIZED_CALLER_SERVICE_ACCOUNTS.split(",") if sa.strip()
        }


def get_settings() -> Settings:
    """Factory function returning active application settings."""
    return Settings(
        HOST=os.getenv("HOST", "0.0.0.0"),
        PORT=int(os.getenv("PORT", "8080")),
        FIXTURES_DIR=os.getenv("FIXTURES_DIR", "fixtures"),
        FIXTURES_GCS_URI=os.getenv("FIXTURES_GCS_URI") or None,
        AUTH_DISABLED=os.getenv("AUTH_DISABLED", "false").lower() in ("true", "1", "yes"),
        AUTHORIZED_CALLER_SERVICE_ACCOUNTS=os.getenv("AUTHORIZED_CALLER_SERVICE_ACCOUNTS", ""),
        EXPECTED_AUDIENCE=os.getenv("EXPECTED_AUDIENCE") or None,
    )
