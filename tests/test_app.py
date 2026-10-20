"""End-to-end tests against a real PostgreSQL with the Remnashop schema.

    TEST_REMNASHOP_DSN=postgresql://user:pass@127.0.0.1/remnashop_test pytest
The database is wiped and re-seeded (tests/remnashop_schema.sql must be applied).
"""

import os
import re
import secrets
import sys
from pathlib import Path

import pytest

DSN = os.environ.get("TEST_REMNASHOP_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_REMNASHOP_DSN is not set")

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    from seed import seed

    seed(os.environ.get("TEST_SEED_DSN", DSN))
    db = tmp_path_factory.mktemp("panel") / "panel.db"
    os.environ.update(
        SECRET_KEY=secrets.token_urlsafe(48),
        PANEL_DB_URL=f"sqlite:///{db}",
        REMNASHOP_DSN=DSN,
        COOKIE_SECURE="false",
        ADMIN_LOGIN="admin",
        ADMIN_PASSWORD="adminpass123",
        STATS_CACHE_SECONDS="0",
    )
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(c, user, password):
    c.cookies.clear()
    page = c.get("/login")
    return c.post("/login", data={"login": user, "password": password, "csrf": csrf(page.text)})


def test_full_flow(client):
    c = client
    assert c.get("/admin").status_code == 200 and "Вход" in c.get("/admin").text  # redirected to login

    r = login(c, "admin", "wrong-password")
    assert "Неверный логин" in r.text

    r = login(c, "admin", "adminpass123")
    assert r.status_code == 200 and "Общая статистика" in r.text
    for path in ("/admin?period=all", "/admin?period=7d", "/admin?period=custom&from=2025-01-01&to=2030-01-01",
                 "/admin/users", "/admin/users?filter=buyers&q=user1", "/admin/partners",
                 "/admin/payouts", "/admin/referrers", "/admin/settings", "/account"):
        assert c.get(path).status_code == 200, path

    # create partner from top referrer #1 (tg 100001)
    page = c.get("/admin/partners/new?referrer=1")
    assert "будет сразу привязан" in page.text
    r = c.post("/admin/partners/new", data={
        "csrf": csrf(page.text), "login": "alice", "name": "Alice", "password": "alicepass1",
        "telegram_id": "100001", "referrer": "1",
    })
    assert "Партнёр создан" in r.text
    pid = int(re.search(r"/admin/partners/(\d+)/settings", r.url.path + r.text).group(1))

    # same referrer can't be attached twice
    t = csrf(r.text)
    r = c.post(f"/admin/partners/{pid}/sources", data={"csrf": t, "kind": "referrer", "query": "100001"})
    assert "уже привязан" in r.text
    r = c.post(f"/admin/partners/{pid}/sources", data={"csrf": t, "kind": "ad_link", "ad_link_id": "2"})
    assert "Источник добавлен" in r.text

    # payout bookkeeping
    m = re.search(r'name="days" value="(\d+)"', r.text)
    due = int(m.group(1))
    assert due > 0 and due % 3 == 0
    r = c.post(f"/admin/partners/{pid}/payouts", data={"csrf": t, "days": "3", "note": "x"})
    assert "Записана выдача" in r.text
    assert f'name="days" value="{due - 3}"' in r.text

    # bad csrf is rejected
    assert c.post(f"/admin/partners/{pid}/payouts", data={"csrf": "bad", "days": "3"}).status_code == 400

    for path in (f"/admin/partners/{pid}", f"/admin/partners/{pid}?period=all",
                 f"/admin/partners/{pid}/users", f"/admin/partners/{pid}/users?filter=active"):
        assert c.get(path).status_code == 200, path

    # partner view: own data only, admin pages forbidden, contacts masked
    r = login(c, "alice", "alicepass1")
    assert "Статистика" in r.text and "Заработано за период" in r.text
    assert c.get("/admin").status_code == 403
    users = c.get("/p/users")
    assert users.status_code == 200 and not re.search(r"@user\d", users.text) and "Юзер" not in users.text
    rewards = c.get("/p/rewards")
    assert "К выдаче" in rewards.text and f"{due - 3}" in rewards.text


def test_reward_math(client):
    """Accrual = distinct attracted buyers (completed, non-test, amount > 0) × rate."""
    from app import remnashop as rs

    scope = rs.Scope(referrer_ids=(2,))
    expected = rs.fetch_one(
        """
        SELECT COUNT(DISTINCT t.user_id) AS n FROM transactions t
        JOIN referrals r ON r.referred_id = t.user_id
        WHERE r.referrer_id = 2 AND t.status = 'COMPLETED' AND NOT t.is_test
          AND (t.pricing->>'final_amount')::numeric > 0
        """
    )["n"]
    assert rs.buyers_total(scope) == expected

    # second level adds users invited by the referrer's invitees
    second = rs.Scope(referrer_ids=(2,), second_level=True)
    assert rs.overview(second, *_all())["users_total"] >= rs.overview(scope, *_all())["users_total"]

    # test / free transactions of user 6 are ignored
    assert all(p["amount"] > 0 for p in rs.recent_payments(rs.Scope(all_users=True), 500))


def _all():
    from app.services import resolve_period

    p = resolve_period("all", None, None)
    return p.start, p.end
