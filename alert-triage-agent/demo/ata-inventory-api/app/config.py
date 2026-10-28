"""Configuration for ata-inventory-api.

Every setting is read from ATA_INVENTORY_<NAME> (pydantic-settings env_prefix). There are
no legacy aliases: DB_USER and MOCK_DB are not read. The Cloud SQL connection settings
have no built-in project values; require_db_settings() fails loudly when the database is
used without them (Terraform, deploy.sh and repin-inventory.sh set them).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    instance_connection_name: str = ""
    db_user: str = ""
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
        return self.db_user

    @property
    def effective_mock_db(self) -> bool:
        return self.mock_db

    def require_db_settings(self) -> None:
        """Raise unless the Cloud SQL connection settings are configured."""
        missing = [
            f"ATA_INVENTORY_{name.upper()}"
            for name in ("instance_connection_name", "db_user", "db_name")
            if not getattr(self, name)
        ]
        if missing:
            raise RuntimeError(
                "Cloud SQL settings not configured: " + ", ".join(missing)
                + " (set them, or ATA_INVENTORY_MOCK_DB=true for the in-memory catalog)"
            )


settings = Settings()
