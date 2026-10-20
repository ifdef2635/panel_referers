from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Kago Analytics"
    secret_key: str = Field(min_length=32)
    # PostgreSQL of the panel: accounts, partners, payouts and the events received from the bot.
    database_url: str = "postgresql+psycopg://kago:kago@127.0.0.1:5432/kago"
    # Public base URL, shown on the integrations page (e.g. https://stats.example.com).
    public_url: str = ""
    timezone: str = "Europe/Moscow"
    # Set to false only for local development over plain HTTP.
    cookie_secure: bool = True
    session_max_age: int = 60 * 60 * 24 * 7
    stats_cache_seconds: int = 30

    # Optional bootstrap admin, created on startup if no admin exists yet.
    admin_login: str | None = None
    admin_password: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
