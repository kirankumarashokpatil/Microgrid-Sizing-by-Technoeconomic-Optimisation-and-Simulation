"""
Persistence settings for the DIP web-app API.

Read from environment variables (prefix ``DIP_``) or a local ``.env`` file. Set
the discrete ``DIP_DB_*`` fields (host/port/user/password/name) and the async
SQLAlchemy URL is assembled for you; or set a full ``DIP_DATABASE_URL`` to
override. ``DIP_DB_SCHEMA`` must match the schema created from ``schema.sql``.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Full async SQLAlchemy URL. Leave unset to assemble it from the fields below.
    database_url: str | None = None

    # Discrete Postgres credentials (used when database_url is not provided).
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "postgres"

    # Schema the tables live in (must match the schema you created on the server).
    db_schema: str = "gigapark_bess_designer"

    # ── Generation API (wind now; solar as a second module in the same repo) ──
    # Base URL differs by environment (local vs deployed) — set it in .env.
    gen_api_base: str = "http://127.0.0.1:8001"
    gen_wind_path: str = "/yield"
    gen_solar_path: str = "/solar-profile"   # BESS-facing endpoint (same app as /yield)
    gen_api_timeout: float = 120.0
    gen_enabled: bool = True
    # When True, a missing/unreachable solar endpoint yields zero solar instead of
    # failing the whole profile build (solar module not live yet).
    gen_solar_optional: bool = True

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "Settings":
        if not self.database_url:
            user = quote(self.db_user, safe="")
            password = quote(self.db_password, safe="")
            self.database_url = (
                f"postgresql+asyncpg://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
