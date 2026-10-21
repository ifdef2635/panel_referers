from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

ROLE_ADMIN = "admin"
ROLE_PARTNER = "partner"

SOURCE_REFERRER = "referrer"  # bot user whose referral link brings people (ref = users.id in the bot)
SOURCE_AD = "source"  # ad link / source name sent in user.registered.data.source

# Reward rules (global defaults, can be overridden per partner)
DEFAULT_SIGNUP_DAYS = 1  # days for every attracted user
DEFAULT_PURCHASE_PERCENT = 50  # % of the purchased subscription duration


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- panel accounts


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_PARTNER)
    name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on password change / deactivation to invalidate existing sessions.
    session_version: Mapped[int] = mapped_column(Integer, default=1)

    # Partner settings
    # Legacy: days per attracted buyer. Kept for old databases, not used any more.
    reward_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Reward overrides; None = global default (see services.reward_rules).
    reward_signup_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Legacy, not used: the link is built from invite_code (see app/bot/service.py).
    referral_link: Mapped[str] = mapped_column(String(255), default="")
    # Code of the partner's own link to the Telegram bot: t.me/<bot>?start=r_<code>.
    invite_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    include_second_level: Mapped[bool] = mapped_column(Boolean, default=False)
    show_contacts: Mapped[bool] = mapped_column(Boolean, default=False)
    # Telegram ID of the partner (where rewarded days go), informational.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sources: Mapped[list["PartnerSource"]] = relationship(
        back_populates="partner", cascade="all, delete-orphan", order_by="PartnerSource.id"
    )
    payouts: Mapped[list["Payout"]] = relationship(
        back_populates="partner",
        cascade="all, delete-orphan",
        order_by="Payout.created_at.desc()",
        foreign_keys="Payout.partner_id",
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class PartnerSource(Base):
    """Links a partner to a traffic source. A source belongs to one partner only."""

    __tablename__ = "partner_sources"
    __table_args__ = (UniqueConstraint("kind", "ref", name="uq_source_kind_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(128))  # bot user id or source name
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    partner: Mapped[Account] = relationship(back_populates="sources")


PAYOUT_DONE = "done"  # issued (by hand, or applied by kago-sync)
PAYOUT_PENDING = "pending"  # queued for kago-sync
PAYOUT_PROCESSING = "processing"  # taken by kago-sync, result not reported yet
PAYOUT_FAILED = "failed"  # kago-sync could not apply it
PAYOUT_CANCELLED = "cancelled"
PAYOUT_LABELS = {
    PAYOUT_DONE: "выдано",
    PAYOUT_PENDING: "в очереди",
    PAYOUT_PROCESSING: "выполняется",
    PAYOUT_FAILED: "ошибка",
    PAYOUT_CANCELLED: "отменено",
}
# Statuses that count as issued: queued payouts too, so the same days are never queued twice.
PAYOUT_COUNTED = (PAYOUT_DONE, PAYOUT_PENDING, PAYOUT_PROCESSING)


class Payout(Base):
    """Subscription days issued to a partner."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    days: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default=PAYOUT_DONE)
    # Set by kago-sync: when the days were added in the VPN service and the new expiry date.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")
    # When the bot told the referrer (applied) or the admins (failed).
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    partner: Mapped[Account] = relationship(back_populates="payouts", foreign_keys=[partner_id])


class Invite(Base):
    """Someone who opened a partner's link in the Telegram bot. The first partner wins.

    The invite counts once that Telegram user shows up as a VPN-bot user registered after the
    click (bot_users.telegram_id, filled by the sync); purchases are matched the same way.
    """

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotContact(Base):
    """Everyone who wrote to the Telegram bot, to find people by @username."""

    __tablename__ = "bot_contacts"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class IngestToken(Base):
    """Bearer token the bot uses for /api/ingest. Only the latest non-revoked one works."""

    __tablename__ = "ingest_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(32))  # first chars, for display
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- data received from the bot
# Written by app/ingest.py, read with plain SQL by app/analytics.py.


class Event(Base):
    """Every accepted event, keyed by its id (idempotency)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_received_at", "received_at"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_ignored: Mapped[bool] = mapped_column(Boolean, default=False)


class BotUser(Base):
    __tablename__ = "bot_users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    referrer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(128), index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str | None] = mapped_column(String(16))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Payment(Base):
    __tablename__ = "payments"

    payment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    purchase_type: Mapped[str | None] = mapped_column(String(16))
    plan: Mapped[str | None] = mapped_column(String(128))
    # Duration of the purchased plan in days (None = unknown or unlimited).
    duration_days: Mapped[int | None] = mapped_column(Integer)
    gateway: Mapped[str | None] = mapped_column(String(64))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    # A refund may arrive before the payment itself; the row then has no amount yet.
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionState(Base):
    """Latest subscription state per user."""

    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "platform", "device_key", name="uq_device"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    device_key: Mapped[str] = mapped_column(String(128), default="")  # device_id or ""
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
