"""Statistics over the data received from the bot (tables filled by app/ingest.py)."""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import get_settings
from .db import fetch_all, fetch_one

# --------------------------------------------------------------------------- cache

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def cached(key: tuple, producer):
    ttl = get_settings().stats_cache_seconds
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------- scope


@dataclass(frozen=True)
class Scope:
    """Set of bot users a statistic is computed over.

    all_users=True means the whole shop (admin view). Otherwise the users that came through
    the partner's referrer accounts (1st level, optionally 2nd) or sources (ad links).
    """

    all_users: bool = False
    referrer_ids: tuple[str, ...] = field(default_factory=tuple)
    sources: tuple[str, ...] = field(default_factory=tuple)
    second_level: bool = False

    @property
    def empty(self) -> bool:
        return not self.all_users and not self.referrer_ids and not self.sources

    def params(self) -> dict[str, Any]:
        return {
            "ref_ids": list(self.referrer_ids),
            "sources": list(self.sources),
            "second": self.second_level,
        }

    def cte(self) -> str:
        if self.all_users:
            return """
            scope AS (
                SELECT u.user_id,
                       CASE WHEN u.referrer_id IS NOT NULL THEN 'referral'
                            WHEN u.source IS NOT NULL THEN 'ad_link'
                            ELSE 'organic' END AS via
                FROM bot_users u
            )"""
        return """
            scope_raw AS (
                SELECT u.user_id, 'ref1' AS via, 1 AS prio
                FROM bot_users u
                WHERE u.referrer_id = ANY(%(ref_ids)s::text[])
                UNION ALL
                SELECT u2.user_id, 'ref2', 2
                FROM bot_users u1
                JOIN bot_users u2 ON u2.referrer_id = u1.user_id
                WHERE %(second)s AND u1.referrer_id = ANY(%(ref_ids)s::text[])
                UNION ALL
                SELECT u.user_id, 'ad_link', 3
                FROM bot_users u
                WHERE u.source = ANY(%(sources)s::text[])
            ),
            scope AS (
                SELECT DISTINCT ON (user_id) user_id, via
                FROM scope_raw
                WHERE NOT (user_id = ANY(%(ref_ids)s::text[]))
                ORDER BY user_id, prio
            )"""

    def paid_cte(self) -> str:
        # A purchase: completed, not test, not refunded, positive amount.
        join = "" if self.all_users else "JOIN scope s ON s.user_id = p.user_id"
        return f"""
            paid AS (
                SELECT p.payment_id, p.user_id, p.paid_at AS created_at, p.currency,
                       COALESCE(p.gateway, '—') AS gateway,
                       COALESCE(p.purchase_type, '—') AS purchase_type,
                       COALESCE(p.plan, '—') AS plan, p.amount, p.duration_days
                FROM payments p {join}
                WHERE p.paid_at IS NOT NULL AND p.amount > 0 AND NOT p.is_test
                  AND p.refunded_at IS NULL
            ),
            first_paid AS (
                SELECT user_id, MIN(created_at) AS first_paid_at FROM paid
                WHERE user_id IS NOT NULL GROUP BY user_id
            )"""


def _with(scope: Scope, *, paid: bool = False) -> str:
    parts = [scope.cte()]
    if paid:
        parts.append(scope.paid_cte())
    return "WITH " + ",".join(parts)


def _money(rows: list[dict[str, Any]], key: str = "total") -> dict[str, Decimal]:
    return {r["currency"]: Decimal(r[key] or 0) for r in rows}


ACTIVE = "sub.status = 'ACTIVE' AND sub.expire_at > now()"


# --------------------------------------------------------------------------- stats


def overview(scope: Scope, start: datetime, end: datetime) -> dict[str, Any]:
    if scope.empty:
        return empty_overview()
    p = scope.params() | {"start": start, "end": end}

    def produce():
        head = fetch_one(
            _with(scope, paid=True)
            + f"""
            SELECT
              (SELECT COUNT(*) FROM scope s JOIN bot_users u ON u.user_id = s.user_id
                 WHERE u.registered_at IS NOT NULL) AS users_total,
              (SELECT COUNT(*) FROM scope s JOIN bot_users u ON u.user_id = s.user_id
                 WHERE u.registered_at >= %(start)s AND u.registered_at < %(end)s) AS registrations,
              (SELECT COUNT(*) FROM scope s JOIN bot_users u ON u.user_id = s.user_id
                 WHERE u.trial_started_at >= %(start)s AND u.trial_started_at < %(end)s) AS trials,
              (SELECT COUNT(*) FROM scope s JOIN subscriptions sub ON sub.user_id = s.user_id
                 WHERE {ACTIVE} AND NOT sub.is_trial) AS active_paid,
              (SELECT COUNT(*) FROM scope s JOIN subscriptions sub ON sub.user_id = s.user_id
                 WHERE {ACTIVE} AND sub.is_trial) AS active_trial,
              (SELECT COUNT(*) FROM paid
                 WHERE created_at >= %(start)s AND created_at < %(end)s) AS payments,
              (SELECT COUNT(DISTINCT user_id) FROM paid
                 WHERE created_at >= %(start)s AND created_at < %(end)s) AS buyers,
              (SELECT COUNT(*) FROM first_paid
                 WHERE first_paid_at >= %(start)s AND first_paid_at < %(end)s) AS new_buyers,
              (SELECT COUNT(*) FROM first_paid) AS buyers_total,
              (SELECT COUNT(*) FROM scope s JOIN bot_users u ON u.user_id = s.user_id
                 JOIN first_paid fp ON fp.user_id = s.user_id
                 WHERE u.registered_at >= %(start)s AND u.registered_at < %(end)s) AS converted
            """,
            p,
        )
        revenue = fetch_all(
            _with(scope, paid=True)
            + """
            SELECT currency, SUM(amount) AS total FROM paid
            WHERE created_at >= %(start)s AND created_at < %(end)s
            GROUP BY currency ORDER BY currency
            """,
            p,
        )
        data = dict(head or {})
        data["revenue"] = _money(revenue)
        regs = data.get("registrations") or 0
        data["conversion"] = round(100 * (data.get("converted") or 0) / regs, 1) if regs else 0.0
        return data

    return cached(("overview", scope, start, end), produce)


def empty_overview() -> dict[str, Any]:
    keys = (
        "users_total registrations trials active_paid active_trial payments buyers "
        "new_buyers buyers_total converted"
    ).split()
    return {k: 0 for k in keys} | {"revenue": {}, "conversion": 0.0}


def bucket_for(start: datetime, end: datetime) -> str:
    days = (end - start).days
    if days <= 93:
        return "day"
    if days <= 730:
        return "week"
    return "month"


def series(scope: Scope, start: datetime, end: datetime, bucket: str) -> dict[str, Any]:
    """Registrations, new buyers and revenue per time bucket (in the panel timezone)."""
    if scope.empty:
        return {"registrations": {}, "new_buyers": {}, "revenue": {}}
    assert bucket in ("day", "week", "month")
    p = scope.params() | {"start": start, "end": end, "tz": get_settings().timezone}
    b = f"date_trunc('{bucket}', {{col}} AT TIME ZONE %(tz)s)::date"

    def produce():
        regs = fetch_all(
            _with(scope)
            + f"""
            SELECT {b.format(col='u.registered_at')} AS d, COUNT(*) AS n
            FROM scope s JOIN bot_users u ON u.user_id = s.user_id
            WHERE u.registered_at >= %(start)s AND u.registered_at < %(end)s
            GROUP BY 1 ORDER BY 1
            """,
            p,
        )
        newb = fetch_all(
            _with(scope, paid=True)
            + f"""
            SELECT {b.format(col='first_paid_at')} AS d, COUNT(*) AS n FROM first_paid
            WHERE first_paid_at >= %(start)s AND first_paid_at < %(end)s
            GROUP BY 1 ORDER BY 1
            """,
            p,
        )
        rev = fetch_all(
            _with(scope, paid=True)
            + f"""
            SELECT {b.format(col='created_at')} AS d, currency, SUM(amount) AS total FROM paid
            WHERE created_at >= %(start)s AND created_at < %(end)s
            GROUP BY 1, 2 ORDER BY 1
            """,
            p,
        )
        revenue: dict[str, dict] = {}
        for r in rev:
            revenue.setdefault(r["currency"], {})[r["d"]] = float(r["total"] or 0)
        return {
            "registrations": {r["d"]: r["n"] for r in regs},
            "new_buyers": {r["d"]: r["n"] for r in newb},
            "revenue": revenue,
        }

    return cached(("series", scope, start, end, bucket), produce)


def breakdown(scope: Scope, start: datetime, end: datetime, dimension: str) -> list[dict]:
    """Revenue and payment counts grouped by gateway / plan / purchase_type."""
    assert dimension in ("gateway", "plan", "purchase_type")
    if scope.empty:
        return []
    p = scope.params() | {"start": start, "end": end}
    return cached(
        ("breakdown", scope, start, end, dimension),
        lambda: fetch_all(
            _with(scope, paid=True)
            + f"""
            SELECT {dimension} AS key, currency, COUNT(*) AS payments,
                   COUNT(DISTINCT user_id) AS buyers, SUM(amount) AS total
            FROM paid WHERE created_at >= %(start)s AND created_at < %(end)s
            GROUP BY 1, 2 ORDER BY total DESC
            """,
            p,
        ),
    )


def sources_breakdown(start: datetime, end: datetime) -> list[dict]:
    """Admin: organic vs referral vs ad link."""
    scope = Scope(all_users=True)
    return cached(
        ("sources", start, end),
        lambda: fetch_all(
            _with(scope, paid=True)
            + """
            SELECT s.via,
                   COUNT(*) FILTER (WHERE u.registered_at >= %(start)s AND u.registered_at < %(end)s)
                     AS registrations,
                   COUNT(fp.user_id) FILTER (WHERE fp.first_paid_at >= %(start)s
                     AND fp.first_paid_at < %(end)s) AS new_buyers
            FROM scope s
            JOIN bot_users u ON u.user_id = s.user_id
            LEFT JOIN first_paid fp ON fp.user_id = s.user_id
            GROUP BY s.via ORDER BY registrations DESC
            """,
            {"start": start, "end": end},
        ),
    )


def geography(scope: Scope, start: datetime, end: datetime, limit: int = 15) -> list[dict]:
    """Registrations and new buyers by country for users registered in the period."""
    if scope.empty:
        return []
    p = scope.params() | {"start": start, "end": end, "limit": limit}
    return cached(
        ("geo", scope, start, end, limit),
        lambda: fetch_all(
            _with(scope, paid=True)
            + """
            SELECT COALESCE(u.country, '—') AS country, COUNT(*) AS registrations,
                   COUNT(fp.user_id) AS buyers
            FROM scope s
            JOIN bot_users u ON u.user_id = s.user_id
            LEFT JOIN first_paid fp ON fp.user_id = s.user_id
            WHERE u.registered_at >= %(start)s AND u.registered_at < %(end)s
            GROUP BY 1 ORDER BY registrations DESC LIMIT %(limit)s
            """,
            p,
        ),
    )


def platforms(scope: Scope) -> list[dict]:
    """Devices by platform (all time): devices and distinct users."""
    if scope.empty:
        return []
    return cached(
        ("platforms", scope),
        lambda: fetch_all(
            _with(scope)
            + """
            SELECT d.platform, COUNT(*) AS devices, COUNT(DISTINCT d.user_id) AS users
            FROM devices d JOIN scope s ON s.user_id = d.user_id
            GROUP BY 1 ORDER BY users DESC, devices DESC
            """,
            scope.params(),
        ),
    )


def buyers_total(scope: Scope) -> int:
    """All-time number of users in scope with at least one purchase (reward basis)."""
    if scope.empty:
        return 0
    row = cached(
        ("buyers_total", scope),
        lambda: fetch_one(
            _with(scope, paid=True) + "SELECT COUNT(*) AS n FROM first_paid", scope.params()
        ),
    )
    return int(row["n"]) if row else 0


def reward_base(
    scope: Scope, renewals: bool, start: datetime | None = None, end: datetime | None = None
) -> dict[str, int]:
    """What partner rewards are computed from: attracted users and purchased subscription days.

    renewals=False counts only the first purchase of every user. start/end limit the window
    (registration date for referrals, payment date for purchases); None means all time.
    """
    zero = {"referrals": 0, "buyers": 0, "purchases": 0, "purchases_no_duration": 0, "days": 0}
    if scope.empty:
        return zero
    p = scope.params() | {"renewals": renewals, "start": start, "end": end}
    window = "(%(start)s::timestamptz IS NULL OR {col} >= %(start)s) AND (%(end)s::timestamptz IS NULL OR {col} < %(end)s)"

    def produce():
        row = fetch_one(
            _with(scope, paid=True)
            + f""",
            counted AS (
                SELECT user_id, created_at, duration_days FROM paid WHERE %(renewals)s
                UNION ALL
                SELECT user_id, created_at, duration_days FROM (
                    SELECT DISTINCT ON (user_id) user_id, created_at, duration_days FROM paid
                    WHERE user_id IS NOT NULL ORDER BY user_id, created_at
                ) f WHERE NOT %(renewals)s
            )
            SELECT
              (SELECT COUNT(*) FROM scope s LEFT JOIN bot_users u ON u.user_id = s.user_id
                 WHERE (%(start)s::timestamptz IS NULL AND %(end)s::timestamptz IS NULL)
                    OR ({window.format(col="u.registered_at")})) AS referrals,
              COUNT(DISTINCT c.user_id) AS buyers,
              COUNT(*) AS purchases,
              COUNT(*) - COUNT(c.duration_days) AS purchases_no_duration,
              COALESCE(SUM(c.duration_days), 0) AS days
            FROM counted c
            WHERE {window.format(col="c.created_at")}
            """,
            p,
        )
        return {k: int(row[k] or 0) for k in zero} if row else zero

    return cached(("reward_base", scope, renewals, start, end), produce)


USER_FILTERS = ("all", "buyers", "active", "trial", "no_purchase")


def users_page(
    scope: Scope,
    *,
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    flt: str = "all",
) -> tuple[list[dict], int]:
    if scope.empty:
        return [], 0
    where = ["u.registered_at IS NOT NULL"]
    p = scope.params() | {"limit": per_page, "offset": (page - 1) * per_page}
    if search:
        where.append(
            "(u.username ILIKE %(q)s OR u.name ILIKE %(q)s OR u.telegram_id::text = %(qexact)s"
            " OR u.user_id = %(qexact)s)"
        )
        p["q"] = "%" + search.lstrip("@").replace("%", r"\%").replace("_", r"\_") + "%"
        p["qexact"] = search.strip()
    if flt == "buyers":
        where.append("fp.user_id IS NOT NULL")
    elif flt == "no_purchase":
        where.append("fp.user_id IS NULL")
    elif flt == "active":
        where.append(f"{ACTIVE} AND NOT sub.is_trial")
    elif flt == "trial":
        where.append(f"{ACTIVE} AND sub.is_trial")

    base = (
        _with(scope, paid=True)
        + """,
        totals AS (
            SELECT user_id, SUM(c) AS payments, jsonb_object_agg(currency, total) AS spent
            FROM (SELECT user_id, currency, SUM(amount) AS total, COUNT(*) AS c
                  FROM paid GROUP BY user_id, currency) x
            GROUP BY user_id
        )
        """
    )
    joins = """
        FROM scope s
        JOIN bot_users u ON u.user_id = s.user_id
        LEFT JOIN subscriptions sub ON sub.user_id = s.user_id
        LEFT JOIN first_paid fp ON fp.user_id = s.user_id
        LEFT JOIN totals t ON t.user_id = s.user_id
        LEFT JOIN bot_users ru ON ru.user_id = u.referrer_id
    """
    w = " WHERE " + " AND ".join(where)
    total = fetch_one(base + "SELECT COUNT(*) AS n " + joins + w, p)
    rows = fetch_all(
        base
        + """
        SELECT u.user_id, u.telegram_id, u.username, u.name, u.registered_at AS created_at,
               u.country, u.source, s.via,
               sub.status AS sub_status, sub.expire_at, sub.is_trial,
               fp.first_paid_at, COALESCE(t.payments, 0) AS payments, t.spent,
               u.referrer_id, ru.username AS referrer_username, ru.name AS referrer_name
        """
        + joins
        + w
        + " ORDER BY u.registered_at DESC, u.user_id LIMIT %(limit)s OFFSET %(offset)s",
        p,
    )
    return rows, int(total["n"]) if total else 0


def recent_payments(scope: Scope, limit: int = 20) -> list[dict]:
    if scope.empty:
        return []
    return cached(
        ("recent", scope, limit),
        lambda: fetch_all(
            _with(scope, paid=True)
            + """
            SELECT pd.*, u.username, u.name, u.telegram_id,
                   COALESCE(s.via, 'organic') AS via,
                   (fp.first_paid_at = pd.created_at) AS is_first
            FROM paid pd
            LEFT JOIN bot_users u ON u.user_id = pd.user_id
            LEFT JOIN scope s ON s.user_id = pd.user_id
            LEFT JOIN first_paid fp ON fp.user_id = pd.user_id
            ORDER BY pd.created_at DESC LIMIT %(limit)s
            """,
            scope.params() | {"limit": limit},
        ),
    )


# --------------------------------------------------------------------------- lookups


def find_user(query: str) -> dict | None:
    """Find a bot user by bot id, Telegram id or @username."""
    q = query.strip()
    if not q:
        return None
    row = fetch_one(
        "SELECT user_id, telegram_id, username, name FROM bot_users WHERE user_id = %(q)s",
        {"q": q},
    )
    if row:
        return row
    if q.lstrip("-").isdigit():
        return fetch_one(
            "SELECT user_id, telegram_id, username, name FROM bot_users"
            " WHERE telegram_id = %(n)s LIMIT 1",
            {"n": int(q)},
        )
    return fetch_one(
        "SELECT user_id, telegram_id, username, name FROM bot_users"
        " WHERE lower(username) = lower(%(q)s) ORDER BY registered_at LIMIT 1",
        {"q": q.lstrip("@")},
    )


def users_by_ids(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = fetch_all(
        "SELECT user_id, telegram_id, username, name FROM bot_users"
        " WHERE user_id = ANY(%(ids)s::text[])",
        {"ids": ids},
    )
    return {r["user_id"]: r for r in rows}


def source_list() -> list[dict]:
    """Ad links / sources seen in user.registered events."""
    return fetch_all(
        """
        SELECT source AS name, COUNT(*) AS users, MAX(registered_at) AS last_at
        FROM bot_users WHERE source IS NOT NULL
        GROUP BY source ORDER BY users DESC
        """
    )


def top_referrers(limit: int = 100) -> list[dict]:
    return cached(
        ("top_referrers", limit),
        lambda: fetch_all(
            """
            WITH buyers AS (
                SELECT DISTINCT user_id FROM payments
                WHERE paid_at IS NOT NULL AND amount > 0 AND NOT is_test AND refunded_at IS NULL
            )
            SELECT r.referrer_id AS user_id, ru.telegram_id, ru.username, ru.name,
                   COUNT(*) AS referrals, COUNT(b.user_id) AS buyers
            FROM bot_users r
            LEFT JOIN bot_users ru ON ru.user_id = r.referrer_id
            LEFT JOIN buyers b ON b.user_id = r.user_id
            WHERE r.referrer_id IS NOT NULL
            GROUP BY r.referrer_id, ru.telegram_id, ru.username, ru.name
            ORDER BY referrals DESC LIMIT %(limit)s
            """,
            {"limit": limit},
        ),
    )


def ingest_stats() -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT COUNT(*) FILTER (WHERE NOT is_ignored) AS accepted,
               COUNT(*) FILTER (WHERE is_ignored) AS ignored,
               MAX(received_at) AS last_received_at,
               COUNT(*) FILTER (WHERE received_at > now() - interval '24 hours') AS last_24h
        FROM events
        """
    )
    by_type = fetch_all(
        "SELECT type, COUNT(*) AS n, MAX(received_at) AS last_at FROM events"
        " GROUP BY type ORDER BY n DESC"
    )
    return (row or {}) | {"by_type": by_type}
