"""Referrer management for the Telegram bot, on top of the panel database.

A referrer is a partner account (Account, role=partner) with a Telegram ID and the bot user it
stands for linked as a "referrer" source, so the bot and the web panel show the same numbers.
Everything here is synchronous; the bot calls it through asyncio.to_thread.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass

import psycopg
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .. import analytics as rs
from .. import botdb
from ..config import get_settings
from ..db import SessionLocal, fetch_one
from ..models import ROLE_PARTNER, SOURCE_REFERRER, Account, PartnerSource, Payout
from ..security import hash_password
from ..services import (
    RewardBalance,
    RewardRules,
    default_rules,
    get_setting,
    reward_balance,
    save_default_rules,
    set_setting,
)

log = logging.getLogger("kago.bot")

NOTIFY_KEY = "bot_notify_state"


class BotError(Exception):
    """A user-facing error (shown to the admin as is)."""


@dataclass
class Referrer:
    partner: Account
    balance: RewardBalance
    bot_user_id: str | None
    username: str | None


# --------------------------------------------------------------------------- lookup


def find_bot_user(query: str) -> dict | None:
    """A VPN-bot user by Telegram ID, @username or bot id (users.id)."""
    q = query.strip()
    if q.isdigit():
        row = fetch_one(
            "SELECT user_id, telegram_id, username, name, registered_at FROM bot_users"
            " WHERE telegram_id = %(n)s ORDER BY registered_at LIMIT 1",
            {"n": int(q)},
        )
        if row:
            return row
    row = rs.find_user(q)
    if row:
        row = fetch_one(
            "SELECT user_id, telegram_id, username, name, registered_at FROM bot_users"
            " WHERE user_id = %(u)s",
            {"u": row["user_id"]},
        )
    return row


def user_label(u: dict) -> str:
    """Same format as the source labels created in the web panel."""
    parts = [u.get("name") or f"id {u.get('user_id')}"]
    if u.get("username"):
        parts.append(f"@{u['username']}")
    if u.get("telegram_id"):
        parts.append(f"tg {u['telegram_id']}")
    return " · ".join(x for x in parts if x)[:255]


def referral_link(bot_user_id: str) -> str:
    """The referral link of a bot user, built from users.referral_code in the Remnashop DB."""
    s = get_settings()
    if not (botdb.enabled() and s.vpn_bot_username):
        return ""
    try:
        with botdb.connect() as conn:
            row = conn.execute(
                "SELECT referral_code FROM users WHERE id = %s", (int(bot_user_id),)
            ).fetchone()
    except (psycopg.Error, ValueError) as exc:
        log.warning("referral code of %s: %s", bot_user_id, exc)
        return ""
    if not row or not row[0]:
        return ""
    return s.referral_link_template.format(bot=s.vpn_bot_username.lstrip("@"), code=row[0])


# --------------------------------------------------------------------------- referrers


def _load(db, partner_id: int) -> Account:
    p = db.get(Account, partner_id, options=[selectinload(Account.sources)])
    if not p or p.role != ROLE_PARTNER:
        raise BotError("Реферер не найден")
    return p


def _view(db, p: Account, defaults: RewardRules | None = None) -> Referrer:
    src = next((s for s in p.sources if s.kind == SOURCE_REFERRER), None)
    user = rs.users_by_ids([src.ref]).get(src.ref) if src else None
    return Referrer(
        partner=p,
        balance=reward_balance(db, p, defaults),
        bot_user_id=src.ref if src else None,
        username=(user or {}).get("username"),
    )


def get_referrer(partner_id: int) -> Referrer:
    with SessionLocal() as db:
        return _view(db, _load(db, partner_id))


def list_referrers() -> list[Referrer]:
    with SessionLocal() as db:
        defaults = default_rules(db)
        partners = db.scalars(
            select(Account)
            .options(selectinload(Account.sources))
            .where(Account.role == ROLE_PARTNER)
            .order_by(Account.name)
        )
        rows = [_view(db, p, defaults) for p in partners]
    rows.sort(key=lambda r: (r.partner.is_active, r.balance.due, r.balance.referrals), reverse=True)
    return rows


def referrer_by_telegram(telegram_id: int) -> Referrer | None:
    with SessionLocal() as db:
        p = db.scalar(
            select(Account)
            .options(selectinload(Account.sources))
            .where(Account.role == ROLE_PARTNER, Account.telegram_id == telegram_id)
            .order_by(Account.is_active.desc(), Account.id)
            .limit(1)
        )
        return _view(db, p) if p else None


def _free_login(db, base: str) -> str:
    base = re.sub(r"[^a-z0-9_.-]", "", base.lower())[:60]
    if len(base) < 3:
        base = f"ref{base}"
    login, n = base, 1
    while db.scalar(select(Account.id).where(Account.login == login)):
        n += 1
        login = f"{base}{n}"
    return login


def create_referrer(bot_user: dict) -> tuple[Referrer, str]:
    """Partner account for a bot user, with the user linked as a referrer source.

    Returns the referrer and the generated panel password.
    """
    user_id = str(bot_user["user_id"])
    tg = bot_user.get("telegram_id")
    with SessionLocal() as db:
        taken = db.scalar(
            select(PartnerSource).where(
                PartnerSource.kind == SOURCE_REFERRER, PartnerSource.ref == user_id
            )
        )
        if taken:
            raise BotError(f"Этот пользователь уже реферер: «{taken.partner.name}»")
        password = secrets.token_urlsafe(9)
        partner = Account(
            login=_free_login(db, bot_user.get("username") or f"tg{tg or user_id}"),
            name=(bot_user.get("name") or bot_user.get("username") or f"id {user_id}")[:128],
            password_hash=hash_password(password),
            role=ROLE_PARTNER,
            telegram_id=tg,
            referral_link=referral_link(user_id),
            note="Добавлен через Telegram-бот",
        )
        db.add(partner)
        db.flush()
        db.add(
            PartnerSource(
                partner_id=partner.id, kind=SOURCE_REFERRER, ref=user_id, label=user_label(bot_user)
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise BotError("Этот пользователь уже привязан к другому партнёру") from None
        rs.clear_cache()
        return _view(db, _load(db, partner.id)), password


def refresh_link(partner_id: int) -> str:
    with SessionLocal() as db:
        p = _load(db, partner_id)
        src = next((s for s in p.sources if s.kind == SOURCE_REFERRER), None)
        if not src:
            raise BotError("К рефереру не привязан пользователь бота")
        link = referral_link(src.ref)
        if not link:
            raise BotError(
                "Не удалось получить ссылку из БД бота: проверьте REMNASHOP_DSN и VPN_BOT_USERNAME"
            )
        p.referral_link = link
        db.commit()
        return link


def set_link(partner_id: int, link: str) -> None:
    link = link.strip()
    if not link.startswith(("https://", "http://", "tg://")) or len(link) > 255 or " " in link:
        raise BotError("Ссылка должна начинаться с https:// и быть не длиннее 255 символов")
    with SessionLocal() as db:
        _load(db, partner_id).referral_link = link
        db.commit()


def set_rates(partner_id: int, *, signup_days: int | None = ..., percent: int | None = ...) -> None:
    """Individual rates; None resets to the global default, ... leaves as is."""
    with SessionLocal() as db:
        p = _load(db, partner_id)
        if signup_days is not ...:
            p.reward_signup_days = signup_days
        if percent is not ...:
            p.reward_percent = percent
        db.commit()


def toggle_active(partner_id: int) -> bool:
    with SessionLocal() as db:
        p = _load(db, partner_id)
        p.is_active = not p.is_active
        if not p.is_active:
            p.session_version += 1  # log out of the web panel
        db.commit()
        rs.clear_cache()
        return p.is_active


def delete_referrer(partner_id: int) -> str:
    with SessionLocal() as db:
        p = _load(db, partner_id)
        name = p.name
        db.delete(p)
        db.commit()
    rs.clear_cache()
    return name


def reset_password(partner_id: int) -> tuple[str, str]:
    with SessionLocal() as db:
        p = _load(db, partner_id)
        password = secrets.token_urlsafe(9)
        p.password_hash = hash_password(password)
        p.session_version += 1
        p.is_active = True
        db.commit()
        return p.login, password


def add_payout(partner_id: int, days: int, note: str = "через Telegram-бот") -> Referrer:
    if days == 0 or abs(days) > 100000:
        raise BotError("Количество дней: целое число, не ноль")
    with SessionLocal() as db:
        p = _load(db, partner_id)
        db.add(Payout(partner_id=p.id, days=days, note=note[:255]))
        db.commit()
        return _view(db, p)


def last_payouts(partner_id: int, limit: int = 5) -> list[Payout]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(Payout)
                .where(Payout.partner_id == partner_id)
                .order_by(Payout.created_at.desc())
                .limit(limit)
            )
        )


# --------------------------------------------------------------------------- rules & totals


def get_rules() -> RewardRules:
    with SessionLocal() as db:
        return default_rules(db)


def update_rules(**changes) -> RewardRules:
    with SessionLocal() as db:
        r = default_rules(db)
        new = RewardRules(**{**r.__dict__, **changes})
        save_default_rules(db, new.signup_days, new.percent, new.renewals)
        db.commit()
        return new


@dataclass
class Totals:
    referrers: int
    active: int
    referrals: int
    buyers: int
    accrued: int
    issued: int
    due: int


def totals(rows: list[Referrer] | None = None) -> Totals:
    rows = list_referrers() if rows is None else rows
    return Totals(
        referrers=len(rows),
        active=sum(r.partner.is_active for r in rows),
        referrals=sum(r.balance.referrals for r in rows),
        buyers=sum(r.balance.buyers for r in rows),
        accrued=sum(r.balance.accrued for r in rows),
        issued=sum(r.balance.issued for r in rows),
        due=sum(max(0, r.balance.due) for r in rows),
    )


# --------------------------------------------------------------------------- notifications


@dataclass
class Change:
    telegram_id: int
    referrer: Referrer
    new_referrals: int
    new_purchases: int
    new_days: int


def collect_changes() -> list[Change]:
    """Growth of every active referrer since the previous call (the first call only remembers)."""
    rs.clear_cache()  # the panel process syncs the data; don't serve this process' stale cache
    rows = [r for r in list_referrers() if r.partner.is_active and r.partner.telegram_id]
    with SessionLocal() as db:
        try:
            state = json.loads(get_setting(db, NOTIFY_KEY, "{}"))
        except ValueError:
            state = {}
        changes, new_state = [], {}
        for r in rows:
            b = r.balance
            key = str(r.partner.id)
            new_state[key] = {"r": b.referrals, "p": b.purchases, "a": b.accrued}
            old = state.get(key)
            if not old:
                continue
            dr, dp = b.referrals - old["r"], b.purchases - old["p"]
            if dr > 0 or dp > 0:
                changes.append(Change(r.partner.telegram_id, r, max(dr, 0), max(dp, 0), b.accrued - old["a"]))
        set_setting(db, NOTIFY_KEY, json.dumps(new_state))
        db.commit()
    return changes
