import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analytics as rs
from .config import get_settings
from .db import fetch_one
from .models import (
    DEFAULT_PURCHASE_PERCENT,
    DEFAULT_SIGNUP_DAYS,
    ROLE_PARTNER,
    SOURCE_AD,
    SOURCE_REFERRER,
    Account,
    Payout,
    Setting,
)

# --------------------------------------------------------------------------- settings


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def _int_setting(db: Session, key: str, default: int) -> int:
    try:
        return int(get_setting(db, key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class RewardRules:
    signup_days: int  # days for every attracted user
    percent: int  # % of the purchased subscription days
    renewals: bool  # every purchase counts (False: only the first purchase of a user)

    def purchase_days(self, plan_days: int) -> int:
        return plan_days * self.percent // 100

    def describe(self) -> str:
        what = "каждой покупки" if self.renewals else "первой покупки"
        return f"{self.signup_days} дн. за приглашённого + {self.percent}% срока {what}"


def default_rules(db: Session) -> RewardRules:
    return RewardRules(
        signup_days=_int_setting(db, "reward_signup_days", DEFAULT_SIGNUP_DAYS),
        percent=_int_setting(db, "reward_percent", DEFAULT_PURCHASE_PERCENT),
        renewals=get_setting(db, "reward_renewals", "1") == "1",
    )


def save_default_rules(db: Session, signup_days: int, percent: int, renewals: bool) -> None:
    set_setting(db, "reward_signup_days", str(signup_days))
    set_setting(db, "reward_percent", str(percent))
    set_setting(db, "reward_renewals", "1" if renewals else "0")


def reward_rules(db: Session, partner: Account, defaults: RewardRules | None = None) -> RewardRules:
    d = defaults or default_rules(db)
    return RewardRules(
        signup_days=d.signup_days if partner.reward_signup_days is None else partner.reward_signup_days,
        percent=d.percent if partner.reward_percent is None else partner.reward_percent,
        renewals=d.renewals,
    )


# --------------------------------------------------------------------------- invite links

BOT_USERNAME_KEY = "bot_username"  # saved by the Telegram bot on start
START_PREFIX = "r_"


def invite_link(db: Session, partner: Account) -> str:
    """The partner's link to the Telegram bot (t.me/<bot>?start=r_<code>); "" until the bot ran."""
    if not partner.invite_code:
        partner.invite_code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "z")
        db.commit()
    name = get_setting(db, BOT_USERNAME_KEY, "")
    return f"https://t.me/{name}?start={START_PREFIX}{partner.invite_code}" if name else ""


# --------------------------------------------------------------------------- scope


def partner_scope(partner: Account) -> rs.Scope:
    return rs.Scope(
        referrer_ids=tuple(sorted(s.ref for s in partner.sources if s.kind == SOURCE_REFERRER)),
        sources=tuple(sorted(s.ref for s in partner.sources if s.kind == SOURCE_AD)),
        second_level=partner.include_second_level,
        invite_partners=(partner.id,),
    )


# --------------------------------------------------------------------------- rewards


@dataclass
class RewardBalance:
    rules: RewardRules
    referrals: int  # attracted users (all time)
    buyers: int  # attracted users with a counted purchase
    purchases: int  # counted purchases
    purchases_no_duration: int  # purchases whose plan duration is unknown (earn nothing)
    plan_days: int  # total days of the counted purchases
    signup_reward: int  # days
    purchase_reward: int  # days
    accrued: int  # days
    issued: int  # days
    due: int  # days left to issue


def reward_balance(db: Session, partner: Account, defaults: RewardRules | None = None) -> RewardBalance:
    rules = reward_rules(db, partner, defaults)
    base = rs.reward_base(partner_scope(partner), rules.renewals)
    issued = int(
        db.scalar(select(func.coalesce(func.sum(Payout.days), 0)).where(Payout.partner_id == partner.id))
    )
    signup = base["referrals"] * rules.signup_days
    purchase = rules.purchase_days(base["days"])
    accrued = signup + purchase
    return RewardBalance(
        rules=rules,
        referrals=base["referrals"],
        buyers=base["buyers"],
        purchases=base["purchases"],
        purchases_no_duration=base["purchases_no_duration"],
        plan_days=base["days"],
        signup_reward=signup,
        purchase_reward=purchase,
        accrued=accrued,
        issued=issued,
        due=accrued - issued,
    )


def earned_in_period(db: Session, partner: Account, start: datetime, end: datetime) -> dict:
    rules = reward_rules(db, partner)
    base = rs.reward_base(partner_scope(partner), rules.renewals, start, end)
    signup = base["referrals"] * rules.signup_days
    purchase = rules.purchase_days(base["days"])
    return base | {"signup_reward": signup, "purchase_reward": purchase, "total": signup + purchase, "rules": rules}


def partners(db: Session) -> list[Account]:
    return list(
        db.scalars(select(Account).where(Account.role == ROLE_PARTNER).order_by(Account.name))
    )


# --------------------------------------------------------------------------- periods

PRESETS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "7d": "7 дней",
    "30d": "30 дней",
    "month": "Этот месяц",
    "prev_month": "Прошлый месяц",
    "all": "Всё время",
}


@dataclass
class Period:
    key: str
    start: datetime
    end: datetime
    date_from: date
    date_to: date  # inclusive, for display / inputs
    label: str


def resolve_period(key: str | None, date_from: str | None, date_to: str | None) -> Period:
    tz = get_settings().tz
    today = datetime.now(tz).date()

    def mk(k: str, d1: date, d2: date, label: str) -> Period:
        start = datetime.combine(d1, time.min, tzinfo=tz)
        end = datetime.combine(d2 + timedelta(days=1), time.min, tzinfo=tz)
        return Period(k, start, end, d1, d2, label)

    if key == "custom" or (key is None and (date_from or date_to)):
        try:
            d1 = date.fromisoformat(date_from) if date_from else today - timedelta(days=29)
            d2 = date.fromisoformat(date_to) if date_to else today
        except ValueError:
            d1, d2 = today - timedelta(days=29), today
        if d1 > d2:
            d1, d2 = d2, d1
        return mk("custom", d1, d2, f"{d1:%d.%m.%Y} — {d2:%d.%m.%Y}")

    key = key if key in PRESETS else "30d"
    if key == "today":
        return mk(key, today, today, PRESETS[key])
    if key == "yesterday":
        y = today - timedelta(days=1)
        return mk(key, y, y, PRESETS[key])
    if key == "7d":
        return mk(key, today - timedelta(days=6), today, PRESETS[key])
    if key == "month":
        return mk(key, today.replace(day=1), today, PRESETS[key])
    if key == "prev_month":
        last = today.replace(day=1) - timedelta(days=1)
        return mk(key, last.replace(day=1), last, PRESETS[key])
    if key == "all":
        return mk(key, date(2020, 1, 1), today, PRESETS[key])
    return mk("30d", today - timedelta(days=29), today, PRESETS["30d"])


def chart_payload(scope: rs.Scope, period: Period) -> dict:
    """Series with gaps filled, ready for Chart.js."""
    start, end = period.start, period.end
    if period.key == "all":
        # Start the chart at the first registration instead of 2020.
        start = max(start, _first_activity(scope) or start).astimezone(get_settings().tz)
    bucket = rs.bucket_for(start, end)
    data = rs.series(scope, start, end, bucket)

    labels: list[date] = []
    d = start.date()
    if bucket == "week":
        d -= timedelta(days=d.weekday())
    elif bucket == "month":
        d = d.replace(day=1)
    last = period.date_to
    while d <= last:
        labels.append(d)
        if bucket == "day":
            d += timedelta(days=1)
        elif bucket == "week":
            d += timedelta(days=7)
        else:
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)

    fmt = "%d.%m" if bucket != "month" else "%m.%Y"
    return {
        "bucket": bucket,
        "labels": [x.strftime(fmt) for x in labels],
        "registrations": [data["registrations"].get(x, 0) for x in labels],
        "new_buyers": [data["new_buyers"].get(x, 0) for x in labels],
        "revenue": {
            cur: [round(vals.get(x, 0), 2) for x in labels]
            for cur, vals in sorted(data["revenue"].items())
        },
    }


def _first_activity(scope: rs.Scope) -> datetime | None:
    if scope.empty:
        return None
    row = rs.cached(
        ("first_activity", scope),
        lambda: fetch_one(
            rs._with(scope)
            + " SELECT MIN(u.registered_at) AS m FROM scope s"
            " JOIN bot_users u ON u.user_id = s.user_id",
            scope.params(),
        ),
    )
    return row["m"] if row and row["m"] else None
