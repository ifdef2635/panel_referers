"""Referrer management for the Telegram bot, on top of the panel database.

A referrer is a partner account (Account, role=partner) with a Telegram ID. Their link is on the
panel domain (<PUBLIC_URL>/r/<code>, see app/referrals.py): each new person who opens it counts
as a referral and is sent on to the site. The bot and the web panel read the same tables, so they
show the same numbers. Everything here is synchronous; the bot calls it through asyncio.to_thread.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from .. import analytics as rs
from ..db import SessionLocal, fetch_one
from ..models import ROLE_PARTNER, Account, BotContact, Payout, ReferralVisit, utcnow
from ..referrals import referral_link
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
    link: str
    clicks: int  # all opens of the link, counted or not
    username: str | None


# --------------------------------------------------------------------------- links & contacts


def touch_contact(telegram_id: int, username: str | None, name: str) -> None:
    with SessionLocal() as db:
        stmt = insert(BotContact).values(
            telegram_id=telegram_id, username=username, name=name[:255], first_seen=utcnow(), last_seen=utcnow()
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[BotContact.telegram_id],
                set_={"username": username, "name": name[:255], "last_seen": utcnow()},
            )
        )
        db.commit()


def find_person(query: str) -> dict | None:
    """Someone by Telegram ID or @username: from the bot's contacts or the VPN-bot users.

    A bare Telegram ID is always accepted, even if the person never wrote to either bot.
    """
    q = query.strip()
    if q.isdigit():
        tg = int(q)
        row = fetch_one(
            "SELECT telegram_id, username, name FROM bot_contacts WHERE telegram_id = %(t)s", {"t": tg}
        ) or fetch_one(
            "SELECT telegram_id, username, COALESCE(name, '') AS name FROM bot_users"
            " WHERE telegram_id = %(t)s ORDER BY registered_at LIMIT 1",
            {"t": tg},
        )
        return row or {"telegram_id": tg, "username": None, "name": ""}
    name = q.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", name):
        return None
    return fetch_one(
        "SELECT telegram_id, username, name FROM bot_contacts WHERE lower(username) = lower(%(u)s)"
        " ORDER BY last_seen DESC LIMIT 1",
        {"u": name},
    ) or fetch_one(
        "SELECT telegram_id, username, COALESCE(name, '') AS name FROM bot_users"
        " WHERE lower(username) = lower(%(u)s) AND telegram_id IS NOT NULL"
        " ORDER BY registered_at LIMIT 1",
        {"u": name},
    )


def person_label(u: dict) -> str:
    parts = [u.get("name") or ""]
    if u.get("username"):
        parts.append(f"@{u['username']}")
    parts.append(f"tg {u['telegram_id']}")
    return " · ".join(x for x in parts if x)


# --------------------------------------------------------------------------- referrers


def _load(db, partner_id: int) -> Account:
    p = db.get(Account, partner_id)
    if not p or p.role != ROLE_PARTNER:
        raise BotError("Реферер не найден")
    return p


def _view(db, p: Account, defaults: RewardRules | None = None, clicks: dict | None = None) -> Referrer:
    if clicks is None:
        clicks = {p.id: db.scalar(select(func.count()).where(ReferralVisit.partner_id == p.id))}
    username = (
        db.scalar(select(BotContact.username).where(BotContact.telegram_id == p.telegram_id))
        if p.telegram_id
        else None
    )
    return Referrer(
        partner=p,
        balance=reward_balance(db, p, defaults),
        link=referral_link(db, p),
        clicks=clicks.get(p.id, 0),
        username=username,
    )


def get_referrer(partner_id: int) -> Referrer:
    with SessionLocal() as db:
        return _view(db, _load(db, partner_id))


def list_referrers() -> list[Referrer]:
    with SessionLocal() as db:
        defaults = default_rules(db)
        clicks = dict(
            db.execute(select(ReferralVisit.partner_id, func.count()).group_by(ReferralVisit.partner_id)).all()
        )
        partners = db.scalars(select(Account).where(Account.role == ROLE_PARTNER).order_by(Account.name))
        rows = [_view(db, p, defaults, clicks) for p in partners]
    rows.sort(key=lambda r: (r.partner.is_active, r.balance.due, r.balance.referrals), reverse=True)
    return rows


def referrer_by_telegram(telegram_id: int) -> Referrer | None:
    with SessionLocal() as db:
        p = db.scalar(
            select(Account)
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


def create_referrer(person: dict) -> tuple[Referrer, str]:
    """Partner account for a Telegram user. Returns the referrer and the generated panel password."""
    tg = int(person["telegram_id"])
    with SessionLocal() as db:
        taken = db.scalar(select(Account).where(Account.role == ROLE_PARTNER, Account.telegram_id == tg))
        if taken:
            raise BotError(f"Этот человек уже реферер: «{taken.name}»")
        password = secrets.token_urlsafe(9)
        partner = Account(
            login=_free_login(db, person.get("username") or f"tg{tg}"),
            name=(person.get("name") or (f"@{person['username']}" if person.get("username") else f"tg {tg}"))[:128],
            password_hash=hash_password(password),
            role=ROLE_PARTNER,
            telegram_id=tg,
            note="Добавлен через Telegram-бот",
        )
        db.add(partner)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise BotError("Не удалось создать реферера, попробуйте ещё раз") from None
        rs.clear_cache()
        return _view(db, partner), password


def new_link(partner_id: int) -> str:
    """A new link code; the old link stops working, people who came through it stay."""
    with SessionLocal() as db:
        p = _load(db, partner_id)
        p.invite_code = None
        return referral_link(db, p)


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


# --------------------------------------------------------------------------- visits

REASONS = {"": "✅", "repeat": "🔁 повтор", "ip": "🌐 тот же адрес", "bot": "🤖 превью/бот", "off": "⏸ реф. отключён"}


def visits(partner_id: int, limit: int = 30) -> tuple[list[ReferralVisit], int, int]:
    """The latest opens of the link, the total and the counted number."""
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(ReferralVisit)
                .where(ReferralVisit.partner_id == partner_id)
                .order_by(ReferralVisit.created_at.desc())
                .limit(limit)
            )
        )
        total, counted = db.execute(
            select(func.count(), func.count().filter(ReferralVisit.counted)).where(
                ReferralVisit.partner_id == partner_id
            )
        ).one()
        return rows, int(total), int(counted)


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
    clicks: int
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
        clicks=sum(r.clicks for r in rows),
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
    rs.clear_cache()  # new data arrives through the panel process; don't serve a stale cache
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
