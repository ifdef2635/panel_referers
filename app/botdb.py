"""Direct connection to the bot database (Remnashop PostgreSQL).

When REMNASHOP_DSN is set, a background thread reads changes from the bot database every
REMNASHOP_SYNC_SECONDS and feeds them through the same pipeline as /api/ingest. Event ids
match integration/remnashop_sync.py and the bot emitter, so all three can run together.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from sqlalchemy import text

from integration import remnashop_sync as rsync

from . import analytics
from .config import get_settings
from .db import SessionLocal, engine
from .ingest import MAX_EVENTS, process
from .models import Setting, utcnow

log = logging.getLogger("kago.botdb")

STATE_KEY = "botdb_state"
LOCK_ID = 7315_2026  # pg advisory lock: only one panel process syncs at a time

_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None


def enabled() -> bool:
    return bool(get_settings().remnashop_dsn)


def masked_dsn() -> str:
    dsn = get_settings().remnashop_dsn or ""
    try:
        info = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "—"
    return f"{info.get('user', '?')}@{info.get('host', '?')}:{info.get('port', 5432)}/{info.get('dbname', '?')}"


def connect() -> psycopg.Connection:
    return psycopg.connect(
        get_settings().remnashop_dsn,
        connect_timeout=10,
        options="-c default_transaction_read_only=on -c statement_timeout=120000",
    )


# --------------------------------------------------------------------------- state


def _load(db) -> dict[str, Any]:
    row = db.get(Setting, STATE_KEY)
    try:
        return json.loads(row.value) if row else {}
    except ValueError:
        return {}


def _save(db, state: dict[str, Any]) -> None:
    row = db.get(Setting, STATE_KEY)
    value = json.dumps(state, ensure_ascii=False, default=str)
    if row:
        row.value = value
    else:
        db.add(Setting(key=STATE_KEY, value=value))


def status() -> dict[str, Any]:
    with SessionLocal() as db:
        state = _load(db)
    return state | {"enabled": enabled(), "dsn": masked_dsn(), "interval": get_settings().remnashop_sync_seconds}


def reset_cursors() -> None:
    with SessionLocal() as db:
        state = _load(db)
        state["cursors"] = {}
        _save(db, state)
        db.commit()


# --------------------------------------------------------------------------- sync


def sync_once() -> dict[str, Any]:
    """Read changes since the stored cursors and apply them. Returns the run summary."""
    started = utcnow()
    totals = {"accepted": 0, "duplicates": 0, "ignored": 0, "errors": 0}

    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar():
            return {"skipped": "уже выполняется в другом процессе"}
        lock_conn.commit()  # the advisory lock is session-level; don't sit in a transaction
        try:
            with SessionLocal() as db:
                state = _load(db)
            cursors = {
                k: datetime.fromisoformat(v) for k, v in (state.get("cursors") or {}).items()
            }
            error = None
            problems: list[str] = []
            try:
                with connect() as conn:
                    problems = rsync.check_schema(conn)
                    if problems:
                        raise RuntimeError("БД не похожа на Remnashop: " + "; ".join(problems))

                    def apply(events: list[dict]) -> None:
                        for i in range(0, len(events), MAX_EVENTS):
                            with SessionLocal() as db:
                                result = process(db, events[i : i + MAX_EVENTS])
                                db.commit()
                            for k in ("accepted", "duplicates", "ignored"):
                                totals[k] += result[k]
                            totals["errors"] += len(result["errors"])
                            for e in result["errors"][:5]:
                                log.warning("bot db event %s rejected: %s", e["id"], e["error"])

                    rsync.run_once(conn, apply, cursors)
            except Exception as exc:  # keep the thread alive, show the error in the panel
                error = f"{type(exc).__name__}: {exc}"
                log.error("bot db sync failed: %s", error)

            summary = {
                "started_at": started.isoformat(),
                "finished_at": utcnow().isoformat(),
                **totals,
                "error": error,
                "problems": problems,
            }
            with SessionLocal() as db:
                state = _load(db)
                state["cursors"] = {k: v.isoformat() for k, v in cursors.items()}
                state["last_run"] = summary
                if error is None:
                    state["last_ok_at"] = summary["finished_at"]
                    state["total_accepted"] = state.get("total_accepted", 0) + totals["accepted"]
                _save(db, state)
                db.commit()
            if totals["accepted"]:
                analytics.clear_cache()
            return summary
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
            lock_conn.commit()


def _loop() -> None:
    interval = max(15, get_settings().remnashop_sync_seconds)
    log.info("bot db sync started (%s, every %ss)", masked_dsn(), interval)
    while not _stop.is_set():
        try:
            sync_once()
        except Exception:
            log.exception("bot db sync crashed")
        _wake.wait(interval)
        _wake.clear()


def trigger() -> None:
    _wake.set()


def start() -> None:
    global _thread
    if not enabled() or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="botdb-sync", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def check() -> dict[str, Any]:
    """Connection + schema check with row counts, for the CLI and the integrations page."""
    with connect() as conn:
        conn.row_factory = dict_row
        info = conn.execute(
            "SELECT current_user AS user, current_database() AS db, version() AS version"
        ).fetchone()
        problems = rsync.check_schema(conn)
        counts = {}
        if not problems:
            for table in ("users", "referrals", "transactions", "subscriptions", "ad_links"):
                counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        try:
            info["migration"] = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        except psycopg.Error:
            conn.rollback()
            info["migration"] = None
    return {**info, "problems": problems, "counts": counts, "checked_at": datetime.now(timezone.utc)}
