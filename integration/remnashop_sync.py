#!/usr/bin/env python3
"""Send Remnashop history (and, with --watch, ongoing changes) to Kago Analytics.

Runs next to the bot and reads its PostgreSQL (read-only is enough):

    pip install "psycopg[binary]" httpx
    export KAGO_URL=https://<panel>/api/ingest KAGO_TOKEN=kago_...
    export KAGO_CA_FILE=kago.crt   # while the panel has a self-signed certificate (no domain)
    export REMNASHOP_DSN=postgresql://remnashop:<password>@127.0.0.1:5001/remnashop
    python remnashop_sync.py              # full backfill
    python remnashop_sync.py --watch 60   # backfill, then send changes every 60 s

Event ids are deterministic, so running it again never duplicates data.
The cursor (last processed updated_at per table) is kept in --state.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row, tuple_row

log = logging.getLogger("remnashop_sync")
BATCH = 500
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Re-read a small window before the cursor: rows committed late are not missed (dupes are free).
OVERLAP = timedelta(minutes=5)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


def lower(v):
    return str(v).lower() if v else None


# --------------------------------------------------------------------------- collectors
# Each returns (events, max_updated_at). Queries follow the Remnashop schema (migrations >= 0026).


def users(cur, since: datetime):
    cur.execute(
        """
        SELECT u.id, u.telegram_id, u.username, u.name, u.language::text AS language,
               u.created_at, u.updated_at, al.name AS ad_link, r.referrer_id,
               GREATEST(u.updated_at, r.created_at) AS changed_at
        FROM users u
        LEFT JOIN ad_links al ON al.id = u.ad_link_id
        LEFT JOIN referrals r ON r.referred_id = u.id
        WHERE GREATEST(u.updated_at, r.created_at) > %(since)s
        ORDER BY changed_at
        """,
        {"since": since},
    )
    events, top = [], since
    for u in cur:
        top = max(top, u["changed_at"])
        events.append(
            {
                # Versioned id: profile changes get through, the registration date never moves.
                "id": f"reg:{u['id']}:{int(u['updated_at'].timestamp())}",
                "type": "user.registered",
                "ts": iso(u["created_at"]),
                "data": {
                    "user_id": u["id"],
                    "telegram_id": u["telegram_id"],
                    "username": u["username"],
                    "name": u["name"],
                    "language": lower(u["language"]),
                    "referrer_id": u["referrer_id"],
                    "source": u["ad_link"],
                },
            }
        )
    return events, top


def payments(cur, since: datetime):
    cur.execute(
        """
        SELECT payment_id, user_id, status::text AS status, is_test, purchase_type::text AS purchase_type,
               gateway_type::text AS gateway, currency::text AS currency,
               pricing->>'final_amount' AS amount, plan_snapshot->>'name' AS plan,
               created_at, updated_at
        FROM transactions
        WHERE status IN ('COMPLETED', 'REFUNDED') AND updated_at > %(since)s
        ORDER BY updated_at
        """,
        {"since": since},
    )
    events, top = [], since
    for t in cur:
        top = max(top, t["updated_at"])
        events.append(
            {
                "id": f"payment:{t['payment_id']}",
                "type": "payment.completed",
                "ts": iso(t["created_at"]),
                "data": {
                    "payment_id": str(t["payment_id"]),
                    "user_id": t["user_id"],
                    "amount": float(t["amount"] or 0),
                    "currency": t["currency"],
                    "purchase_type": t["purchase_type"],
                    "plan": t["plan"],
                    "gateway": t["gateway"],
                    "is_test": t["is_test"],
                },
            }
        )
        if t["status"] == "REFUNDED":
            events.append(
                {
                    "id": f"refund:{t['payment_id']}",
                    "type": "payment.refunded",
                    "ts": iso(max(t["updated_at"], t["created_at"])),
                    "data": {"payment_id": str(t["payment_id"])},
                }
            )
    return events, top


def subscriptions(cur, since: datetime):
    # Current subscription of every user (the panel keeps the latest state per user) plus every
    # trial subscription, so the panel knows who took a trial even after buying.
    cur.execute(
        """
        SELECT s.id, s.user_id, s.status::text AS status, s.is_trial, s.expire_at,
               s.created_at, s.updated_at, (u.current_subscription_id = s.id) AS is_current
        FROM subscriptions s
        JOIN users u ON u.id = s.user_id
        WHERE s.updated_at > %(since)s
          AND (u.current_subscription_id = s.id OR s.is_trial)
        ORDER BY s.updated_at
        """,
        {"since": since},
    )
    events, top = [], since
    for s in cur:
        top = max(top, s["updated_at"])
        data = {
            "user_id": s["user_id"],
            "status": s["status"],
            "expire_at": iso(s["expire_at"]),
            "is_trial": s["is_trial"],
            "created_at": iso(s["created_at"]),
        }
        if s["is_current"]:
            events.append(
                {
                    "id": f"sub:{s['user_id']}:{s['updated_at'].timestamp()}",
                    "type": "subscription.updated",
                    "ts": iso(s["updated_at"]),
                    "data": data,
                }
            )
        else:
            # Old trial: its ts is its creation, so it never overrides the current state.
            events.append(
                {
                    "id": f"trial:{s['id']}",
                    "type": "subscription.updated",
                    "ts": iso(s["created_at"]),
                    "data": data,
                }
            )
    return events, top


COLLECTORS = {"users": users, "payments": payments, "subscriptions": subscriptions}

# Columns the collectors read. information_schema only lists columns the role may SELECT,
# so a missing entry means either an incompatible schema or missing grants.
REQUIRED_COLUMNS = {
    "users": ["id", "telegram_id", "username", "name", "language", "created_at", "updated_at",
              "ad_link_id", "current_subscription_id"],
    "referrals": ["referrer_id", "referred_id", "created_at"],
    "transactions": ["payment_id", "user_id", "status", "is_test", "purchase_type", "gateway_type",
                     "currency", "pricing", "plan_snapshot", "created_at", "updated_at"],
    "subscriptions": ["id", "user_id", "status", "is_trial", "expire_at", "created_at", "updated_at"],
    "ad_links": ["id", "name"],
}


def check_schema(conn) -> list[str]:
    """Problems that prevent syncing; an empty list means the database looks like Remnashop."""
    rows = conn.cursor(row_factory=tuple_row).execute(
        """
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%(t)s)
        """,
        {"t": list(REQUIRED_COLUMNS)},
    ).fetchall()
    conn.rollback()
    have: dict[str, set[str]] = {}
    for table, column in rows:
        have.setdefault(table, set()).add(column)
    problems = []
    for table, columns in REQUIRED_COLUMNS.items():
        if table not in have:
            problems.append(f"нет таблицы {table} (или нет прав SELECT на неё)")
            continue
        missing = [c for c in columns if c not in have[table]]
        if missing:
            problems.append(f"{table}: нет колонок {', '.join(missing)} (или нет прав SELECT)")
    return problems


# --------------------------------------------------------------------------- sending


class Sender:
    def __init__(self, url: str, token: str, verify: bool | str = True):
        self.url = url
        self.client = httpx.Client(
            verify=verify, timeout=30, headers={"Authorization": f"Bearer {token}"}
        )
        self.totals = {"accepted": 0, "duplicates": 0, "ignored": 0, "errors": 0}

    def send(self, events: list[dict]) -> None:
        for i in range(0, len(events), BATCH):
            self._post(events[i : i + BATCH])

    def _post(self, batch: list[dict]) -> None:
        for attempt in range(8):
            try:
                r = self.client.post(self.url, json={"events": batch})
            except httpx.HTTPError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    sys.exit(
                        "Сертификат панели не прошёл проверку. Если он самоподписанный, скопируйте"
                        " /etc/kago/tls/selfsigned.crt с сервера панели и укажите KAGO_CA_FILE=<путь>."
                    )
                log.warning("network error: %s", exc)
            else:
                if r.status_code == 200:
                    body = r.json()
                    for k in ("accepted", "duplicates", "ignored"):
                        self.totals[k] += body.get(k, 0)
                    for e in body.get("errors", []):
                        self.totals["errors"] += 1
                        log.warning("rejected %s: %s", e.get("id"), e.get("error"))
                    return
                if r.status_code in (401, 403):
                    sys.exit(f"Токен не принят ({r.status_code}). Проверьте KAGO_TOKEN.")
                if r.status_code < 500 and r.status_code != 429:
                    sys.exit(f"Запрос отклонён: {r.status_code} {r.text[:300]}")
                log.warning("server answered %s, retrying", r.status_code)
            time.sleep(min(2**attempt, 60))
        raise RuntimeError("Не удалось отправить события, сервер недоступен")


def load_state(path: Path) -> dict[str, datetime]:
    try:
        raw = json.loads(path.read_text())
        return {k: datetime.fromisoformat(v) for k, v in raw.items()}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, datetime]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.isoformat() for k, v in state.items()}, indent=2))
    tmp.replace(path)


def run_once(conn, send, state: dict[str, datetime]) -> int:
    """One pass over all collectors; `send(events)` delivers a list of events."""
    sent = 0
    for name, collect in COLLECTORS.items():
        since = state.get(name, EPOCH)
        with conn.cursor(row_factory=dict_row) as cur:
            events, top = collect(cur, since - OVERLAP if since > EPOCH else since)
        conn.rollback()  # end the read transaction
        if events:
            send(events)
            sent += len(events)
        state[name] = max(top, since)
    return sent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="keep running, sync every N seconds")
    ap.add_argument("--state", default="remnashop_sync.state.json", help="cursor file")
    ap.add_argument("--full", action="store_true", help="ignore the cursor and resend everything")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    url, token = os.getenv("KAGO_URL"), os.getenv("KAGO_TOKEN")
    dsn = os.getenv("REMNASHOP_DSN")
    if not (url and token and dsn):
        sys.exit("Нужны переменные KAGO_URL, KAGO_TOKEN и REMNASHOP_DSN")
    # Self-signed panel certificate: pin it with KAGO_CA_FILE; KAGO_INSECURE=1 is a last resort.
    verify: bool | str = os.getenv("KAGO_CA_FILE") or os.getenv("KAGO_INSECURE", "0") != "1"

    state_path = Path(args.state)
    state = {} if args.full else load_state(state_path)
    sender = Sender(url, token, verify)

    while True:
        try:
            with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
                problems = check_schema(conn)
                if problems:
                    sys.exit("БД не похожа на Remnashop:\n  " + "\n  ".join(problems))
                n = run_once(conn, sender.send, state)
            save_state(state_path, state)
            log.info("sent %d events, totals %s", n, sender.totals)
        except (psycopg.Error, RuntimeError) as exc:
            if not args.watch:
                raise
            log.error("sync failed: %s", exc)
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
