from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    OFF_SERVICE_API_KEY: str
    OFF_PARQUET_PATH: str = "/app/data/openfoodfacts/food.parquet"
    OFF_LOOKUP_TIMEOUT_SECONDS: float = 6.0
    OFF_LOOKUP_MEMORY_LIMIT_MB: int = 512


settings = Settings()
