from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Panel
    app_name: str = "Referral Panel"
    secret_key: str = Field(min_length=32)
    # SQLite with the panel's own data: accounts, partner sources, payouts.
    panel_db_url: str = "sqlite:///./data/panel.db"
    timezone: str = "Europe/Moscow"
    # Set to false only for local development over plain HTTP.
    cookie_secure: bool = True
    session_max_age: int = 60 * 60 * 24 * 7
    stats_cache_seconds: int = 60

    # Remnashop PostgreSQL (read-only access is enough and recommended).
    remnashop_dsn: str = "postgresql://panel_ro:password@127.0.0.1:5001/remnashop"
    remnashop_pool_size: int = 5

    # Optional bootstrap admin, created on startup if no admin exists yet.
    admin_login: str | None = None
    admin_password: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
