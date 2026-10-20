"""Tests against real PostgreSQL databases.

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1/kago_test \
    TEST_REMNASHOP_DSN=postgresql://user:pass@127.0.0.1/remnashop_test pytest

Both databases are wiped. The Remnashop one must have tests/remnashop_schema.sql applied;
it is only needed for the end-to-end sync test.
"""

import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
RS_DSN = os.environ.get("TEST_REMNASHOP_DSN")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL is not set")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "integration"))


@pytest.fixture(scope="session")
def app_client():
    os.environ.update(
        SECRET_KEY=secrets.token_urlsafe(48),
        DATABASE_URL=DB_URL,
        COOKIE_SECURE="false",
        ADMIN_LOGIN="admin",
        ADMIN_PASSWORD="adminpass123",
        STATS_CACHE_SECONDS="0",
    )
    from fastapi.testclient import TestClient

    from app.db import Base, engine
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def token(app_client):
    from app.db import SessionLocal
    from app.ingest import issue_token

    with SessionLocal() as db:
        return issue_token(db)


def post(c, token, payload, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/api/ingest", json=payload, headers=headers, **kw)


def ev(eid, etype, data, ts=None):
    ts = ts or datetime.now(timezone.utc)
    return {"id": eid, "type": etype, "ts": ts.isoformat(), "data": data}


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(c, user, password):
    c.cookies.clear()
    page = c.get("/login")
    return c.post("/login", data={"login": user, "password": password, "csrf": csrf(page.text)})


# --------------------------------------------------------------------------- ingest API


def test_ingest_auth_and_limits(app_client, token):
    c = app_client
    assert post(c, None, [ev("a1", "user.registered", {"user_id": 1})]).status_code == 401
    assert post(c, "kago_wrong", [ev("a1", "user.registered", {"user_id": 1})]).status_code == 401
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert c.post("/api/ingest", content=b"not json", headers=h).status_code == 400
    assert c.post("/api/ingest", content=b"", headers=h).status_code == 400
    assert post(c, token, []).status_code == 400
    assert post(c, token, [ev(f"x{i}", "noop", {}) for i in range(501)]).status_code == 400
    big = {"events": [ev("big", "noop", {"pad": "x" * (1024 * 1024)})]}
    assert post(c, token, big).status_code == 413


def test_ingest_formats_validation_and_duplicates(app_client, token):
    c = app_client
    # single event, array and {"events": [...]}
    r = post(c, token, ev("t-single", "user.registered", {"user_id": 9001, "country": "RU"}))
    assert r.json() == {"accepted": 1, "duplicates": 0, "ignored": 0, "errors": []}
    batch = [
        ev("t-single", "user.registered", {"user_id": 9001}),  # duplicate id
        ev("t-unknown", "something.new", {"x": 1}),  # unknown type
        ev("t-badcountry", "user.registered", {"user_id": 9002, "country": "Russia"}),
        ev("t-noamount", "payment.completed", {"payment_id": "p1", "user_id": 9001, "currency": "RUB"}),
        ev("t-future", "user.registered", {"user_id": 9003}, datetime.now(timezone.utc) + timedelta(days=3)),
        {"id": "t-nots", "type": "user.registered", "data": {"user_id": 9004}},
        ev("t-ok", "payment.completed", {"payment_id": "p1", "user_id": 9001, "amount": 199, "currency": "rub"}),
    ]
    body = post(c, token, {"events": batch}).json()
    assert body["accepted"] == 1 and body["duplicates"] == 1 and body["ignored"] == 1
    errs = {e["id"]: e["error"] for e in body["errors"]}
    assert errs["t-badcountry"] == "data.country: country — двухбуквенный код (RU)"
    assert set(errs) == {"t-badcountry", "t-noamount", "t-future", "t-nots"}
    assert all(e["status"] == "error" for e in body["errors"])
    # a rejected event can be fixed and resent with the same id
    fixed = ev("t-badcountry", "user.registered", {"user_id": 9002, "country": "kz"})
    assert post(c, token, [fixed]).json()["accepted"] == 1


def test_ingest_order_independence(app_client, token):
    from app.db import fetch_one

    c = app_client
    now = datetime.now(timezone.utc)
    t1, t2 = now - timedelta(days=10), now - timedelta(days=2)
    post(c, token, [
        # refund arrives before the payment
        ev("o-ref", "payment.refunded", {"payment_id": "p-ord"}, now),
        ev("o-pay", "payment.completed", {"payment_id": "p-ord", "user_id": 7001, "amount": 10, "currency": "USD"}, t1),
        # newer subscription state first, older one later must not overwrite it
        ev("o-s2", "subscription.updated", {"user_id": 7001, "status": "ACTIVE", "expire_at": (now + timedelta(days=30)).isoformat()}, t2),
        ev("o-s1", "subscription.updated", {"user_id": 7001, "status": "EXPIRED", "expire_at": t1.isoformat(), "is_trial": True}, t1),
        # registration: earliest date wins, first referrer is kept
        ev("o-r2", "user.registered", {"user_id": 7001, "referrer_id": 1, "username": "new"}, t2),
        ev("o-r1", "user.registered", {"user_id": 7001, "referrer_id": 2}, t1),
        ev("o-self", "user.registered", {"user_id": 7002, "referrer_id": 7002}, t1),
    ])
    pay = fetch_one("SELECT * FROM payments WHERE payment_id = 'p-ord'")
    assert pay["amount"] == 10 and pay["refunded_at"] is not None
    sub = fetch_one("SELECT * FROM subscriptions WHERE user_id = '7001'")
    assert sub["status"] == "ACTIVE"
    u = fetch_one("SELECT * FROM bot_users WHERE user_id = '7001'")
    assert u["registered_at"] == t1 and u["referrer_id"] == "1" and u["username"] == "new"
    assert u["trial_started_at"] is not None  # the trial is remembered
    assert fetch_one("SELECT referrer_id FROM bot_users WHERE user_id = '7002'")["referrer_id"] is None


# --------------------------------------------------------------------------- end to end


@pytest.mark.skipif(not RS_DSN, reason="TEST_REMNASHOP_DSN is not set")
def test_remnashop_sync_and_ui(app_client, token, tmp_path):
    import psycopg
    import remnashop_sync
    from seed import seed

    from app import analytics as rs
    from app.db import engine

    c = app_client
    with engine.begin() as conn:  # start from a clean projection
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices")
    seed(RS_DSN)

    sender = remnashop_sync.Sender("/api/ingest", token)
    sender.client = c  # send through the test client
    c.headers["Authorization"] = f"Bearer {token}"
    state: dict = {}
    with psycopg.connect(RS_DSN) as conn:
        sent = remnashop_sync.run_once(conn, sender.send, state)
        assert sent > 0 and sender.totals["errors"] == 0
        # second pass only re-reads the overlap window: nothing new is accepted
        before = sender.totals["accepted"]
        remnashop_sync.run_once(conn, sender.send, state)
        assert sender.totals["accepted"] == before
    del c.headers["Authorization"]

    # numbers match a direct query against the bot database
    with psycopg.connect(RS_DSN) as conn:
        users_n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        buyers_ref2 = conn.execute(
            """SELECT COUNT(DISTINCT t.user_id) FROM transactions t
               JOIN referrals r ON r.referred_id = t.user_id
               WHERE r.referrer_id = 2 AND t.status = 'COMPLETED' AND NOT t.is_test
                 AND (t.pricing->>'final_amount')::numeric > 0"""
        ).fetchone()[0]
        revenue_rub = conn.execute(
            """SELECT SUM((pricing->>'final_amount')::numeric) FROM transactions
               WHERE status = 'COMPLETED' AND NOT is_test AND currency = 'RUB'"""
        ).fetchone()[0]
    from app.services import resolve_period

    period = resolve_period("all", None, None)
    ov = rs.overview(rs.Scope(all_users=True), period.start, period.end)
    assert ov["users_total"] == users_n
    assert ov["revenue"]["RUB"] == revenue_rub
    assert rs.buyers_total(rs.Scope(referrer_ids=("2",))) == buyers_ref2

    # ---- admin UI
    r = login(c, "admin", "wrong-password")
    assert "Неверный логин" in r.text
    r = login(c, "admin", "adminpass123")
    assert "Общая статистика" in r.text and "География" in r.text
    for path in ("/admin?period=all", "/admin?period=7d", "/admin?period=custom&from=2025-01-01&to=2030-01-01",
                 "/admin/users", "/admin/users?filter=buyers&q=user1", "/admin/partners", "/admin/payouts",
                 "/admin/referrers", "/admin/settings", "/admin/integrations", "/account",
                 "/admin/integrations/files/events_api.md", "/admin/integrations/files/remnashop_sync.py"):
        assert c.get(path).status_code == 200, path
    assert c.get("/admin/integrations/files/..%2Fapp%2Fconfig.py").status_code == 404

    page = c.get("/admin/partners/new?referrer=1")
    assert "будет сразу привязан" in page.text
    r = c.post("/admin/partners/new", data={
        "csrf": csrf(page.text), "login": "alice", "name": "Alice", "password": "alicepass1",
        "telegram_id": "100001", "referrer": "1",
    })
    assert "Партнёр создан" in r.text
    pid = int(re.search(r"/admin/partners/(\d+)/settings", r.text).group(1))
    t = csrf(r.text)
    r = c.post(f"/admin/partners/{pid}/sources", data={"csrf": t, "kind": "referrer", "query": "100001"})
    assert "уже привязан" in r.text
    r = c.post(f"/admin/partners/{pid}/sources", data={"csrf": t, "kind": "source", "source_name": "Блогер B"})
    assert "Источник добавлен" in r.text

    due = int(re.search(r'name="days" value="(\d+)"', r.text).group(1))
    assert due > 0 and due % 3 == 0
    r = c.post(f"/admin/partners/{pid}/payouts", data={"csrf": t, "days": "3", "note": "x"})
    assert "Записана выдача" in r.text and f'name="days" value="{due - 3}"' in r.text
    assert c.post(f"/admin/partners/{pid}/payouts", data={"csrf": "bad", "days": "3"}).status_code == 400
    for path in (f"/admin/partners/{pid}", f"/admin/partners/{pid}?period=all",
                 f"/admin/partners/{pid}/users", f"/admin/partners/{pid}/users?filter=active"):
        assert c.get(path).status_code == 200, path

    # token re-issue from the UI invalidates the old one
    r = c.post("/admin/integrations/token", data={"csrf": t})
    new_token = re.search(r"<pre class=\"code\">(kago_[^<]+)</pre>", r.text).group(1)
    assert new_token != token
    assert new_token not in c.get("/admin/integrations").text  # shown only once
    assert post(c, token, [ev("z1", "noop", {})]).status_code == 401
    assert post(c, new_token, [ev("z1", "noop", {})]).status_code == 200

    # ---- partner: own data only, contacts masked
    r = login(c, "alice", "alicepass1")
    assert "Заработано за период" in r.text
    assert c.get("/admin").status_code == 403
    users = c.get("/p/users")
    assert users.status_code == 200 and not re.search(r"@user\d", users.text) and "Юзер" not in users.text
    rewards = c.get("/p/rewards")
    assert "К выдаче" in rewards.text and str(due - 3) in rewards.text


@pytest.mark.skipif(not RS_DSN, reason="TEST_REMNASHOP_DSN is not set")
def test_direct_bot_db_sync(app_client):
    import psycopg

    from app import analytics as rs
    from app import botdb
    from app.config import get_settings
    from app.db import engine
    from app.services import resolve_period

    c = app_client
    settings = get_settings()
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices")
    try:
        # a database that is not Remnashop is refused with a clear message
        settings.remnashop_dsn = DB_URL.replace("+psycopg", "")
        botdb.reset_cursors()
        summary = botdb.sync_once()
        assert summary["error"] and "нет таблицы referrals" in summary["error"]

        settings.remnashop_dsn = RS_DSN
        botdb.reset_cursors()
        info = botdb.check()
        assert info["problems"] == [] and info["counts"]["users"] > 0
        summary = botdb.sync_once()
        assert summary["error"] is None and summary["accepted"] > 0 and summary["errors"] == 0
        assert botdb.sync_once()["accepted"] == 0  # only the overlap window is re-read

        with psycopg.connect(RS_DSN) as conn:
            users_n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        period = resolve_period("all", None, None)
        assert rs.overview(rs.Scope(all_users=True), period.start, period.end)["users_total"] == users_n

        login(c, "admin", "adminpass123")
        page = c.get("/admin/integrations")
        assert "Подключение к БД бота" in page.text and "работает" in page.text
        r = c.post("/admin/integrations/botdb/check", data={"csrf": csrf(page.text)})
        assert "Подключение работает" in r.text
        r = c.post("/admin/integrations/botdb/sync", data={"csrf": csrf(page.text), "full": "true"})
        assert "Полная пересинхронизация запущена" in r.text
    finally:
        settings.remnashop_dsn = ""
