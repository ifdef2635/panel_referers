from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# create_all() does not touch existing tables: columns added after the first release go here.
MIGRATIONS = (
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reward_signup_days INTEGER",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reward_percent INTEGER",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS referral_link VARCHAR(255) NOT NULL DEFAULT ''",
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS duration_days INTEGER",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS invite_code VARCHAR(32)",
    "CREATE UNIQUE INDEX IF NOT EXISTS accounts_invite_code_key ON accounts (invite_code)",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'done'",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS expire_at TIMESTAMPTZ",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ",
)


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for sql in MIGRATIONS:
            conn.exec_driver_sql(sql)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run raw SQL with psycopg-style %(name)s parameters."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.exec_driver_sql(sql, params or {}).mappings()]


def fetch_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None
