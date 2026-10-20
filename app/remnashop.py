"""Read-only access to the Remnashop PostgreSQL database.

Targets the Remnashop schema after migration 0026 (users.id based foreign keys):
users, referrals, transactions, subscriptions, ad_links.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            s = get_settings()
            _pool = ConnectionPool(
                s.remnashop_dsn,
                min_size=1,
                max_size=s.remnashop_pool_size,
                kwargs={
                    "row_factory": dict_row,
                    # Defence in depth: even with a writable role nothing gets modified.
                    "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
                    "connect_timeout": 5,
                },
                open=True,
                timeout=10,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return list(conn.execute(sql, params or {}).fetchall())


def fetch_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def ping() -> bool:
    try:
        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


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
    """Set of Remnashop users a statistic is computed over.

    all_users=True means the whole shop (admin view). Otherwise the users that came
    through the partner's referrer accounts (1st level, optionally 2nd) or ad links.
    """

    all_users: bool = False
    referrer_ids: tuple[int, ...] = field(default_factory=tuple)
    ad_link_ids: tuple[int, ...] = field(default_factory=tuple)
    second_level: bool = False

    @property
    def empty(self) -> bool:
        return not self.all_users and not self.referrer_ids and not self.ad_link_ids

    def params(self) -> dict[str, Any]:
        return {
            "ref_ids": list(self.referrer_ids),
            "ad_ids": list(self.ad_link_ids),
            "second": self.second_level,
        }

    def cte(self) -> str:
        if self.all_users:
            return """
            scope AS (
                SELECT u.id AS user_id,
                       CASE WHEN r.id IS NOT NULL THEN 'referral'
                            WHEN u.ad_link_id IS NOT NULL THEN 'ad_link'
                            ELSE 'organic' END AS via
                FROM users u
                LEFT JOIN referrals r ON r.referred_id = u.id
            )"""
        return """
            scope_raw AS (
                SELECT r.referred_id AS user_id, 'ref1' AS via, 1 AS prio
                FROM referrals r
                WHERE r.referrer_id = ANY(%(ref_ids)s::int[])
                UNION ALL
                SELECT r2.referred_id, 'ref2', 2
                FROM referrals r1
                JOIN referrals r2 ON r2.referrer_id = r1.referred_id
                WHERE %(second)s AND r1.referrer_id = ANY(%(ref_ids)s::int[])
                UNION ALL
                SELECT u.id, 'ad_link', 3
                FROM users u
                WHERE u.ad_link_id = ANY(%(ad_ids)s::int[])
            ),
            scope AS (
                SELECT DISTINCT ON (user_id) user_id, via
                FROM scope_raw
                WHERE NOT (user_id = ANY(%(ref_ids)s::int[]))
                ORDER BY user_id, prio
            )"""


# A "purchase" is a completed, non-test transaction with a positive final amount.
_PAID_CTE = """
    paid AS (
        SELECT t.id, t.user_id, t.created_at,
               t.currency::text AS currency,
               t.gateway_type::text AS gateway,
               t.purchase_type::text AS purchase_type,
               COALESCE(t.plan_snapshot->>'name', '—') AS plan,
               COALESCE(NULLIF(t.pricing->>'final_amount', '')::numeric, 0) AS amount
        FROM transactions t
        JOIN scope s ON s.user_id = t.user_id
        WHERE t.status = 'COMPLETED' AND NOT t.is_test
          AND COALESCE(NULLIF(t.pricing->>'final_amount', '')::numeric, 0) > 0
    ),
    first_paid AS (
        SELECT user_id, MIN(created_at) AS first_paid_at FROM paid GROUP BY user_id
    )"""


def _with(scope: Scope, *, paid: bool = False) -> str:
    parts = [scope.cte()]
    if paid:
        parts.append(_PAID_CTE)
    return "WITH " + ",".join(parts)


def _money(rows: list[dict[str, Any]], key: str = "total") -> dict[str, Decimal]:
    return {r["currency"]: Decimal(r[key] or 0) for r in rows}


# --------------------------------------------------------------------------- stats


def overview(scope: Scope, start: datetime, end: datetime) -> dict[str, Any]:
    if scope.empty:
        return empty_overview()
    p = scope.params() | {"start": start, "end": end}

    def produce():
        head = fetch_one(
            _with(scope, paid=True)
            + """
            SELECT
              (SELECT COUNT(*) FROM scope) AS users_total,
              (SELECT COUNT(*) FROM scope s JOIN users u ON u.id = s.user_id
                 WHERE u.created_at >= %(start)s AND u.created_at < %(end)s) AS registrations,
              (SELECT COUNT(DISTINCT sub.user_id) FROM subscriptions sub
                 JOIN scope s ON s.user_id = sub.user_id
                 WHERE sub.is_trial AND sub.created_at >= %(start)s
                   AND sub.created_at < %(end)s) AS trials,
              (SELECT COUNT(*) FROM scope s JOIN users u ON u.id = s.user_id
                 JOIN subscriptions sub ON sub.id = u.current_subscription_id
                 WHERE sub.status = 'ACTIVE' AND sub.expire_at > now()
                   AND NOT sub.is_trial) AS active_paid,
              (SELECT COUNT(*) FROM scope s JOIN users u ON u.id = s.user_id
                 JOIN subscriptions sub ON sub.id = u.current_subscription_id
                 WHERE sub.status = 'ACTIVE' AND sub.expire_at > now()
                   AND sub.is_trial) AS active_trial,
              (SELECT COUNT(*) FROM paid
                 WHERE created_at >= %(start)s AND created_at < %(end)s) AS payments,
              (SELECT COUNT(DISTINCT user_id) FROM paid
                 WHERE created_at >= %(start)s AND created_at < %(end)s) AS buyers,
              (SELECT COUNT(*) FROM first_paid
                 WHERE first_paid_at >= %(start)s AND first_paid_at < %(end)s) AS new_buyers,
              (SELECT COUNT(*) FROM first_paid) AS buyers_total,
              (SELECT COUNT(*) FROM scope s JOIN users u ON u.id = s.user_id
                 JOIN first_paid fp ON fp.user_id = s.user_id
                 WHERE u.created_at >= %(start)s AND u.created_at < %(end)s) AS converted
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
            SELECT {b.format(col='u.created_at')} AS d, COUNT(*) AS n
            FROM scope s JOIN users u ON u.id = s.user_id
            WHERE u.created_at >= %(start)s AND u.created_at < %(end)s
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
    p = {"start": start, "end": end}
    return cached(
        ("sources", start, end),
        lambda: fetch_all(
            _with(scope, paid=True)
            + """
            SELECT s.via,
                   COUNT(*) FILTER (WHERE u.created_at >= %(start)s AND u.created_at < %(end)s)
                     AS registrations,
                   COUNT(fp.user_id) FILTER (WHERE fp.first_paid_at >= %(start)s
                     AND fp.first_paid_at < %(end)s) AS new_buyers
            FROM scope s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN first_paid fp ON fp.user_id = s.user_id
            GROUP BY s.via ORDER BY registrations DESC
            """,
            p,
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
    where = ["TRUE"]
    p = scope.params() | {"limit": per_page, "offset": (page - 1) * per_page}
    if search:
        where.append(
            "(u.username ILIKE %(q)s OR u.name ILIKE %(q)s OR u.telegram_id::text = %(qexact)s"
            " OR u.id::text = %(qexact)s)"
        )
        p["q"] = f"%{search.lstrip('@')}%"
        p["qexact"] = search.strip()
    if flt == "buyers":
        where.append("fp.user_id IS NOT NULL")
    elif flt == "no_purchase":
        where.append("fp.user_id IS NULL")
    elif flt == "active":
        where.append("sub.status = 'ACTIVE' AND sub.expire_at > now() AND NOT sub.is_trial")
    elif flt == "trial":
        where.append("sub.is_trial AND sub.status = 'ACTIVE' AND sub.expire_at > now()")

    base = (
        _with(scope, paid=True)
        + """,
        totals AS (
            SELECT user_id, SUM(c) AS payments,
                   jsonb_object_agg(currency, total) AS spent
            FROM (SELECT user_id, currency, SUM(amount) AS total, COUNT(*) AS c
                  FROM paid GROUP BY user_id, currency) x
            GROUP BY user_id
        )
        """
    )
    joins = """
        FROM scope s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN subscriptions sub ON sub.id = u.current_subscription_id
        LEFT JOIN first_paid fp ON fp.user_id = s.user_id
        LEFT JOIN totals t ON t.user_id = s.user_id
        LEFT JOIN referrals ref ON ref.referred_id = s.user_id
        LEFT JOIN users ru ON ru.id = ref.referrer_id
        LEFT JOIN ad_links al ON al.id = u.ad_link_id
    """
    w = " WHERE " + " AND ".join(where)
    total = fetch_one(base + "SELECT COUNT(*) AS n " + joins + w, p)
    rows = fetch_all(
        base
        + """
        SELECT u.id, u.telegram_id, u.username, u.name, u.created_at, u.is_blocked,
               s.via, sub.status::text AS sub_status, sub.expire_at, sub.is_trial,
               sub.plan_snapshot->>'name' AS plan,
               fp.first_paid_at, COALESCE(t.payments, 0) AS payments, t.spent,
               ru.id AS referrer_id, ru.username AS referrer_username, ru.name AS referrer_name,
               al.name AS ad_link_name
        """
        + joins
        + w
        + " ORDER BY u.created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
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
            SELECT pd.*, u.username, u.name, u.telegram_id, s.via,
                   (fp.first_paid_at = pd.created_at) AS is_first
            FROM paid pd
            JOIN users u ON u.id = pd.user_id
            JOIN scope s ON s.user_id = pd.user_id
            JOIN first_paid fp ON fp.user_id = pd.user_id
            ORDER BY pd.created_at DESC LIMIT %(limit)s
            """,
            scope.params() | {"limit": limit},
        ),
    )


# --------------------------------------------------------------------------- lookups


def find_user(query: str) -> dict | None:
    """Find a Remnashop user by internal id, telegram id, @username or referral code."""
    q = query.strip()
    if not q:
        return None
    if q.lstrip("-").isdigit():
        row = fetch_one(
            "SELECT id, telegram_id, username, name, referral_code FROM users"
            " WHERE telegram_id = %(n)s ORDER BY id LIMIT 1",
            {"n": int(q)},
        )
        if row:
            return row
        if abs(int(q)) < 2**31:
            return fetch_one(
                "SELECT id, telegram_id, username, name, referral_code FROM users WHERE id = %(n)s",
                {"n": int(q)},
            )
        return None
    return fetch_one(
        "SELECT id, telegram_id, username, name, referral_code FROM users"
        " WHERE lower(username) = lower(%(q)s) OR referral_code = %(raw)s ORDER BY id LIMIT 1",
        {"q": q.lstrip("@"), "raw": q},
    )


def users_by_ids(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = fetch_all(
        "SELECT id, telegram_id, username, name, referral_code FROM users"
        " WHERE id = ANY(%(ids)s::int[])",
        {"ids": ids},
    )
    return {r["id"]: r for r in rows}


def ad_links() -> list[dict]:
    return fetch_all(
        """
        SELECT al.id, al.name, al.code, al.is_active, COUNT(u.id) AS users
        FROM ad_links al LEFT JOIN users u ON u.ad_link_id = al.id
        GROUP BY al.id ORDER BY al.id DESC
        """
    )


def top_referrers(limit: int = 50) -> list[dict]:
    return cached(
        ("top_referrers", limit),
        lambda: fetch_all(
            """
            SELECT u.id, u.telegram_id, u.username, u.name, COUNT(r.id) AS referrals,
                   COUNT(DISTINCT t.user_id) AS buyers
            FROM referrals r
            JOIN users u ON u.id = r.referrer_id
            LEFT JOIN transactions t ON t.user_id = r.referred_id AND t.status = 'COMPLETED'
                 AND NOT t.is_test
                 AND COALESCE(NULLIF(t.pricing->>'final_amount', '')::numeric, 0) > 0
            GROUP BY u.id ORDER BY referrals DESC LIMIT %(limit)s
            """,
            {"limit": limit},
        ),
    )
