"""Payout delivery end to end: panel queue → kago-sync (remnashop_sync.deliver_payouts) →
Remnawave (faked) + the Remnashop test database → result back in the panel.

Needs TEST_DATABASE_URL and TEST_REMNASHOP_DSN (see test_app.py).
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from test_app import DB_URL, RS_DSN, app_client, token  # noqa: F401

pytestmark = pytest.mark.skipif(not (DB_URL and RS_DSN), reason="test databases are not set")


class FakeRemnawave:
    def __init__(self):
        self.users: dict[str, dict] = {}
        self.fail_patch = False
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer rw-token"
        if request.method == "GET" and request.url.path.startswith("/api/users/"):
            uuid = request.url.path.rsplit("/", 1)[1]
            if uuid not in self.users:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"response": {"uuid": uuid, **self.users[uuid]}})
        if request.method == "PATCH" and request.url.path == "/api/users":
            if self.fail_patch:
                return httpx.Response(500, json={"message": "boom"})
            body = __import__("json").loads(request.content)
            self.users[body["uuid"]]["expireAt"] = body["expireAt"]
            if body.get("status"):
                self.users[body["uuid"]]["status"] = body["status"]
            return httpx.Response(200, json={"response": {"uuid": body["uuid"], **self.users[body["uuid"]]}})
        return httpx.Response(404)


@pytest.fixture()
def env(app_client, token):  # noqa: F811
    import psycopg
    import remnashop_sync
    from seed import seed

    from app.db import engine

    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices, invites, bot_contacts")
        conn.exec_driver_sql("DELETE FROM accounts WHERE role = 'partner'")
        conn.exec_driver_sql("DELETE FROM settings")
    seed(RS_DSN, n_users=60)

    fake = FakeRemnawave()
    with psycopg.connect(RS_DSN) as conn:
        # a referrer with a subscription, its Remnawave twin; and an expired one
        subs = conn.execute(
            """SELECT u.telegram_id, s.id, s.user_remna_id::text, s.expire_at FROM users u
               JOIN subscriptions s ON s.id = u.current_subscription_id ORDER BY u.id LIMIT 2"""
        ).fetchall()
        no_sub = conn.execute(
            "SELECT telegram_id FROM users WHERE current_subscription_id IS NULL LIMIT 1"
        ).fetchone()[0]
        past = datetime.now(timezone.utc) - timedelta(days=5)
        future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=20)
        conn.execute("UPDATE subscriptions SET status = 'ACTIVE', expire_at = %s WHERE id = %s", (future, subs[0][1]))
        conn.execute("UPDATE subscriptions SET status = 'EXPIRED', expire_at = %s WHERE id = %s", (past, subs[1][1]))
        subs[0] = (*subs[0][:3], future)
    for tg, sub_id, uuid, expire in subs:
        fake.users[uuid] = {"expireAt": expire.isoformat().replace("+00:00", "Z"), "status": "ACTIVE"}
    fake.users[subs[1][2]].update(status="EXPIRED", expireAt=past.isoformat())

    sender = remnashop_sync.Sender("/api/ingest", token)
    sender.client = app_client
    app_client.headers["Authorization"] = f"Bearer {token}"
    rw = remnashop_sync.Remnawave("http://rw", "rw-token", client=httpx.Client(
        base_url="http://rw", transport=httpx.MockTransport(fake.handler),
        headers={"Authorization": "Bearer rw-token"},
    ))
    yield {"fake": fake, "sender": sender, "rw": rw, "subs": subs, "no_sub": no_sub, "sync": remnashop_sync}
    del app_client.headers["Authorization"]


def sub_expire(sub_id):
    import psycopg

    with psycopg.connect(RS_DSN) as conn:
        return conn.execute("SELECT expire_at, status::text FROM subscriptions WHERE id = %s", (sub_id,)).fetchone()


def test_payout_delivery(env):
    from app import payouts as po
    from app.bot import service as svc
    from app.db import SessionLocal

    sync, sender, rw, fake = env["sync"], env["sender"], env["rw"], env["fake"]
    (tg1, sub1, uuid1, exp1), (tg2, sub2, uuid2, _) = env["subs"]

    ref1, _ = svc.create_referrer({"telegram_id": tg1, "username": None, "name": "One"})
    ref2, _ = svc.create_referrer({"telegram_id": tg2, "username": None, "name": "Two"})
    ref3, _ = svc.create_referrer({"telegram_id": env["no_sub"], "username": None, "name": "NoSub"})

    # manual mode: the payout is a record, kago-sync gets nothing
    _, p = svc.add_payout(ref1.partner.id, 2)
    assert p.status == "done"
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 0 and not fake.calls

    svc.set_delivery("sync", False)
    _, p1 = svc.add_payout(ref1.partner.id, 10)
    _, p2 = svc.add_payout(ref2.partner.id, 7)
    _, p3 = svc.add_payout(ref3.partner.id, 5)
    _, neg = svc.add_payout(ref1.partner.id, -1)  # corrections are never sent
    assert (p1.status, neg.status) == ("pending", "done")
    assert svc.get_referrer(ref1.partner.id).balance.issued == 2 + 10 - 1  # queued counts as issued

    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 2

    # active subscription: + 10 days from its current end, in Remnawave and in Remnashop
    expire, status = sub_expire(sub1)
    assert expire == exp1 + timedelta(days=10) and status == "ACTIVE"
    assert fake.users[uuid1]["expireAt"].startswith((exp1 + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S"))
    # expired subscription: + 7 days from now, re-activated
    expire, status = sub_expire(sub2)
    assert before + timedelta(days=7) <= expire <= datetime.now(timezone.utc) + timedelta(days=7)
    assert status == "ACTIVE" and fake.users[uuid2]["status"] == "ACTIVE"

    with SessionLocal() as db:
        got = {p.id: p for p, _ in po.unannounced(db)}
    assert got[p1.id].status == "done" and got[p1.id].expire_at == exp1 + timedelta(days=10)
    assert got[p3.id].status == "failed" and "нет подписки" in got[p3.id].error
    assert p2.id in got

    # the bot announces them once
    announced = svc.take_announcements()
    assert {p.id for p, _, _ in announced} == {p1.id, p2.id, p3.id}
    assert svc.take_announcements() == []

    # a Remnawave error changes nothing in Remnashop; the failed payout blocks auto payouts
    fake.fail_patch = True
    _, p4 = svc.add_payout(ref1.partner.id, 3)
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 0
    assert sub_expire(sub1)[0] == exp1 + timedelta(days=10)
    ref = svc.get_referrer(ref1.partner.id)
    assert [p.status for p in ref.open_payouts] == ["failed"] and ref.balance.issued == 2 + 10 - 1
    fake.fail_patch = False
    svc.payout_action(p4.id, "retry")
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 1
    assert sub_expire(sub1)[0] == exp1 + timedelta(days=13)

    # a claimed payout is never handed out twice
    _, p5 = svc.add_payout(ref1.partner.id, 1)
    with SessionLocal() as db:
        first = po.claim(db)
        assert [x["id"] for x in first] == [p5.id] and po.claim(db) == []
    svc.payout_action(p5.id, "done")  # stuck → the admin added it by hand

    # auto mode: everything due is queued when kago-sync asks, except for a partner whose payout
    # failed and waits for the admin
    from app.models import Invite

    svc.set_delivery("sync", True)
    svc.payout_action(p3.id, "cancel")
    svc.add_payout(ref3.partner.id, 1)
    sync.deliver_payouts(sender, RS_DSN, rw)  # fails again: still no subscription
    now = datetime.now(timezone.utc)
    events = []
    for ref, invited_tg in ((ref2, 999001), (ref3, 999002)):
        svc.set_rates(ref.partner.id, signup_days=20)  # one invite = 20 days
        with SessionLocal() as db:
            db.add(Invite(telegram_id=invited_tg, partner_id=ref.partner.id, created_at=now - timedelta(hours=1)))
            db.commit()
        events.append({"id": f"reg-{invited_tg}", "type": "user.registered", "ts": now.isoformat(),
                       "data": {"user_id": invited_tg, "telegram_id": invited_tg}})
    client = sender.client
    assert client.post("/api/ingest", json=events).json()["accepted"] == 2
    due2 = svc.get_referrer(ref2.partner.id).balance.due
    assert due2 == 20 - 7 and svc.get_referrer(ref3.partner.id).balance.due == 20

    before2 = sub_expire(sub2)[0]
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 1
    assert sub_expire(sub2)[0] == before2 + timedelta(days=due2)
    assert svc.get_referrer(ref2.partner.id).balance.due == 0
    assert [p.status for p in svc.get_referrer(ref3.partner.id).open_payouts] == ["failed"]
    assert sync.deliver_payouts(sender, RS_DSN, rw) == 0  # nothing new is due

    # unauthorized agents are refused
    r = client.post("/api/payouts/claim", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
