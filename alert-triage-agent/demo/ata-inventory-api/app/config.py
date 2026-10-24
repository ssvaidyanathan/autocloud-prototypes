"""Configuration for ata-inventory-api."""

import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    project_id: str = "cloud-assist-fde-2"
    instance_connection_name: str = "cloud-assist-fde-2:us-west1:ata-inventory-db"
    db_user: str = "ata-inventory-sa@cloud-assist-fde-2.iam"
    db_name: str = "inventory"
    db_iam_auth: bool = True
    mock_db: bool = False
    port: int = 8080

    model_config = SettingsConfigDict(
        env_prefix="ATA_INVENTORY_",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def effective_db_user(self) -> str:
        return os.getenv("ATA_INVENTORY_DB_USER") or os.getenv("DB_USER") or self.db_user

    @property
    def effective_mock_db(self) -> bool:
        val = os.getenv("ATA_INVENTORY_MOCK_DB") or os.getenv("MOCK_DB")
        if val is not None:
            return val.lower() in ("true", "1", "yes")
        return self.mock_db


settings = Settings()
