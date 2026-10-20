from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

ROLE_ADMIN = "admin"
ROLE_PARTNER = "partner"

SOURCE_REFERRER = "referrer"  # Remnashop user whose referral link brings people
SOURCE_AD_LINK = "ad_link"  # Remnashop ad link (ad_links table)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    # Days of subscription per attracted buyer; None = global default.
    reward_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    """Links a partner to a Remnashop traffic source. A source belongs to one partner only."""

    __tablename__ = "partner_sources"
    __table_args__ = (UniqueConstraint("kind", "ref_id", name="uq_source_kind_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[int] = mapped_column(Integer)  # users.id or ad_links.id in Remnashop
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    partner: Mapped[Account] = relationship(back_populates="sources")


class Payout(Base):
    """Subscription days issued to a partner."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    days: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    partner: Mapped[Account] = relationship(back_populates="payouts", foreign_keys=[partner_id])


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


DEFAULT_REWARD_DAYS = 3
