"""Configuration for ata-orders-api."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    inventory_service_url: str = "http://localhost:8080"
    port: int = 8080

    model_config = SettingsConfigDict(
        env_prefix="ATA_ORDERS_",
        case_sensitive=False,
    )


settings = Settings()
