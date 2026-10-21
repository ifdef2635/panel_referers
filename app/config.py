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

    # Optional direct connection to the bot database (Remnashop PostgreSQL), read-only is enough.
    # When set, the panel pulls changes from it in the background (see app/botdb.py).
    remnashop_dsn: str = ""
    remnashop_sync_seconds: int = 60

    # Telegram bot for managing referrers (python -m app.bot); empty token = the bot is off.
    bot_token: str = ""
    # Telegram IDs of the bot admins, comma-separated.
    bot_admin_ids: str = ""
    # Username of the VPN bot (Remnashop) whose referral links are handed out, without @.
    vpn_bot_username: str = ""
    # {bot} = vpn_bot_username, {code} = users.referral_code in the Remnashop database.
    referral_link_template: str = "https://t.me/{bot}?start=ref_{code}"
    # How often referrers get "new invites / purchases" messages; 0 = off.
    bot_notify_seconds: int = 300

    # Optional bootstrap admin, created on startup if no admin exists yet.
    admin_login: str | None = None
    admin_password: str | None = None

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.bot_admin_ids.replace(";", ",").split(",") if x.strip().isdigit()}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
