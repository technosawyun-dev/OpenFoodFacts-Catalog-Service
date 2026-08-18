from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    OFF_SERVICE_API_KEY: str
    OFF_PARQUET_PATH: str = "/app/data/openfoodfacts/food.parquet"
    # Built by refresh/build_index.py (indexed on `code`) alongside the raw
    # parquet swap. When present, lookups use this instead of scanning the
    # parquet fresh every request — ~1-6ms vs ~1-2.5s. Falls back to the raw
    # parquet scan if this hasn't been built yet (e.g. first deploy before
    # the first refresh has run) so lookups still work, just slower.
    OFF_INDEX_DB_PATH: str = "/app/data/openfoodfacts/food_index.duckdb"
    OFF_LOOKUP_TIMEOUT_SECONDS: float = 6.0
    OFF_LOOKUP_MEMORY_LIMIT_MB: int = 512


settings = Settings()
