"""Telegram bot end to end: real dispatcher and handlers, a fake Telegram API, the real panel
database. VPN-bot data arrives as events on /api/ingest, the way kago-sync sends it.

Needs TEST_DATABASE_URL (see test_app.py); reuses its app_client and token fixtures.
"""

import asyncio
import itertools
import re
from datetime import datetime, timedelta, timezone

import pytest

from test_app import DB_URL, app_client, ev, login, post, token  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL is not set")

ADMIN = 5689226201
REFERRER = 500001
ALICE, BOB, CAROL, DAVE = 600001, 600002, 600003, 600004  # people who open the referrer link


class FakeTelegram:
    """Answers Bot API calls locally and records what the bot sent."""

    def __init__(self):
        from aiogram.client.session.base import BaseSession

        outer = self
        self.sent: list[tuple[int, str, object]] = []  # (chat_id, text, reply_markup)
        self.ids = itertools.count(1000)

        class Session(BaseSession):
            async def make_request(self, bot, method, timeout=None):
                return outer.answer(method)

            async def close(self):
                pass

            async def stream_content(self, *a, **kw):  # pragma: no cover
                raise NotImplementedError

        self.session = Session()

    def answer(self, method):
        from aiogram.methods import EditMessageText, GetMe, SendMessage
        from aiogram.types import Chat, Message, User

        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Kago", username="kago_referrer_bot")
        if isinstance(method, (SendMessage, EditMessageText)):
            self.sent.append((int(method.chat_id), method.text, method.reply_markup))
            return Message(
                message_id=next(self.ids),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    def last(self, chat_id: int) -> tuple[str, object]:
        for cid, text, kb in reversed(self.sent):
            if cid == chat_id:
                return text, kb
        raise AssertionError(f"nothing sent to {chat_id}")


def buttons(kb) -> dict[str, str]:
    return {b.text: b.callback_data or b.url for row in kb.inline_keyboard for b in row}


def button(kb, prefix: str) -> str:
    found = [data for text, data in buttons(kb).items() if text.startswith(prefix)]
    assert found, f"no button {prefix!r} in {list(buttons(kb))}"
    return found[0]


@pytest.fixture(scope="module")
def tg(app_client):  # noqa: F811
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties

    from app.bot import service as svc
    from app.bot.handlers import setup
    from app.config import get_settings
    from app.db import engine

    settings = get_settings()
    settings.vpn_bot_username = "kago_vpn_bot"
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices, invites, bot_contacts")
        conn.exec_driver_sql("DELETE FROM accounts WHERE role = 'partner'")
        conn.exec_driver_sql("DELETE FROM settings")
    svc.set_bot_username("kago_referrer_bot")  # done by app/bot/__main__.py on start

    fake = FakeTelegram()
    bot = Bot("42:TEST", session=fake.session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    setup(dp, {ADMIN})
    loop = asyncio.new_event_loop()
    counter = itertools.count(1)

    def user(uid, username=None):
        from aiogram.types import User

        return User(id=uid, is_bot=False, first_name=f"Name{uid}", username=username)

    def text(uid: int, body: str, username: str | None = None):
        from aiogram.types import Chat, Message, Update

        msg = Message(
            message_id=next(counter),
            date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type="private"),
            from_user=user(uid, username),
            text=body,
            entities=[{"type": "bot_command", "offset": 0, "length": len(body.split()[0])}]
            if body.startswith("/") else None,
        )
        loop.run_until_complete(dp.feed_update(bot, Update(update_id=next(counter), message=msg)))
        return fake.last(uid)

    def press(uid: int, data: str):
        from aiogram.types import CallbackQuery, Chat, Message, Update

        cq = CallbackQuery(
            id=str(next(counter)),
            from_user=user(uid),
            chat_instance="x",
            data=data,
            message=Message(
                message_id=next(counter), date=datetime.now(timezone.utc),
                chat=Chat(id=uid, type="private"), text="old",
            ),
        )
        loop.run_until_complete(dp.feed_update(bot, Update(update_id=next(counter), callback_query=cq)))
        return fake.last(uid)

    yield fake, text, press
    loop.close()
    settings.vpn_bot_username = ""


def test_bot_referrer_flow(tg, app_client, token):  # noqa: F811
    from app.bot import service as svc

    fake, text, press = tg
    now = datetime.now(timezone.utc)

    # ---- the referrer writes to the bot first: only gets their ID
    body, _ = text(REFERRER, "/start", "best_blogger")
    assert f"<code>{REFERRER}</code>" in body

    body, kb = text(ADMIN, "/start")
    assert "KAGO REFERRER" in body and "Рефереров: <b>0</b>" in body
    assert "данных ещё не было" in body

    # ---- add the referrer by @username (known from the contact)
    press(ADMIN, button(kb, "➕"))
    body, _ = text(ADMIN, "@nobody_here")
    assert "не найден" in body
    body, kb = text(ADMIN, "@best_blogger")
    assert "@best_blogger" in body and f"tg {REFERRER}" in body
    body, kb = press(ADMIN, button(kb, "✅ Добавить"))
    assert "Реферер добавлен" in body and "Ссылка отправлена рефереру" in body
    link = re.search(r"https://t\.me/kago_referrer_bot\?start=(r_\w+)", body)
    assert link, body
    start = link.group(1)
    assert start in fake.last(REFERRER)[0]  # the referrer got the link
    login_name = re.search(r"Логин: <code>([^<]+)</code>", body).group(1)

    ref = svc.referrer_by_telegram(REFERRER)
    pid = ref.partner.id

    # the same person can't be added twice
    press(ADMIN, "n:add:0")
    body, _ = text(ADMIN, str(REFERRER))
    assert "уже реферер" in body

    # ---- people open the link
    body, kb = text(REFERRER, f"/start {start}")
    assert "собственная ссылка" in fake.sent[-2][1] and "Ваша реферальная ссылка" in body
    # bob is an existing VPN customer: registered a month ago
    post(app_client, token, [ev("b-reg", "user.registered", {"user_id": 9102, "telegram_id": BOB}, now - timedelta(days=30))])
    for who in (ALICE, BOB, CAROL):
        body, kb = text(who, f"/start {start}", "alice_a" if who == ALICE else None)
        assert "Вас пригласил(а)" in body and buttons(kb)["🚀 Перейти в KAGO VPN"] == "https://t.me/kago_vpn_bot"
    body, _ = text(ALICE, f"/start {start}")  # second time: still welcomed, nothing changes
    assert "KAGO VPN" in body
    body, _ = text(DAVE, "/start r_unknown")
    assert "Добро пожаловать" in body and "пригласил" not in body

    # ---- kago-sync delivers VPN-bot data: alice registers and buys 30 days, bob buys 90 days
    later = datetime.now(timezone.utc)
    r = post(app_client, token, [
        ev("a-reg", "user.registered", {"user_id": 9101, "telegram_id": ALICE, "username": "alice_a"}, later),
        ev("a-pay", "payment.completed", {"payment_id": "pa1", "user_id": 9101, "amount": 199,
                                          "currency": "RUB", "duration_days": 30}, later),
        ev("b-pay", "payment.completed", {"payment_id": "pb1", "user_id": 9102, "amount": 499,
                                          "currency": "RUB", "duration_days": 90}, later),
    ])
    assert r.json()["accepted"] == 3

    ref = svc.get_referrer(pid)
    b = ref.balance
    assert (ref.clicks, b.referrals, b.purchases, b.plan_days) == (3, 1, 1, 30)
    assert b.accrued == 1 + 15 and b.due == 16

    body, kb = press(ADMIN, f"r:card:{pid}:0")
    assert "Перешли по ссылке: <b>3</b>" in body and "К выдаче: <b>16 дней</b>" in body
    body, _ = press(ADMIN, button(kb, "👥 Приглашённые"))
    assert "💳×1" in body and "уже был клиентом" in body and "не в VPN" in body

    # ---- payout of everything due, then a correction
    body, kb = press(ADMIN, f"r:card:{pid}:0")
    body, kb = press(ADMIN, button(kb, "🎁 Выдать"))
    body, kb = press(ADMIN, button(kb, "✅ Да"))
    assert "Записана выдача 16 дней (реферер уведомлён)" in body and "К выдаче: <b>0 дней</b>" in body
    assert "Вам выдано <b>16 дней</b>" in fake.last(REFERRER)[0]
    press(ADMIN, f"r:pay_custom:{pid}:0")
    body, _ = text(ADMIN, "abc")
    assert "целое число" in body
    body, _ = text(ADMIN, "-3")
    assert "Записана выдача -3 дня" in body and "К выдаче: <b>3 дня</b>" in body

    # ---- rates: individual 100%, then global 2 days per invite and only first purchases
    press(ADMIN, f"r:set_percent:{pid}:0")
    text(ADMIN, "100")
    assert svc.get_referrer(pid).balance.accrued == 1 + 30
    press(ADMIN, f"r:rates_reset:{pid}:0")
    body, kb = press(ADMIN, "n:rules:0")
    assert "1 день за каждого приглашённого + 50%" in body
    press(ADMIN, button(kb, "За приглашённого"))
    body, kb = text(ADMIN, "2")
    assert "2 дня за каждого приглашённого" in body
    assert svc.get_referrer(pid).balance.accrued == 2 + 15
    svc.update_rules(signup_days=1)

    # ---- the referrer's own screen and the web panel
    body, _ = text(REFERRER, "/start")
    assert start in body and "Зарегистрировались в VPN: <b>1</b>" in body
    body, _ = press(ADMIN, f"r:access_ok:{pid}:0")
    password = re.search(r"Пароль: <code>([^<]+)</code>", body).group(1)
    page = login(app_client, login_name, password)
    assert "Заработано за период" in page.text and start in page.text

    # ---- notifications: the first round remembers, a new registration is reported
    assert svc.collect_changes() == []
    text(DAVE, f"/start {start}")
    post(app_client, token, [ev("d-reg", "user.registered", {"user_id": 9104, "telegram_id": DAVE},
                                datetime.now(timezone.utc))])
    changes = svc.collect_changes()
    assert [(c.telegram_id, c.new_referrals, c.new_days) for c in changes] == [(REFERRER, 1, 1)]
    assert svc.collect_changes() == []

    # ---- a new link: the old one stops working, people already invited stay
    press(ADMIN, f"r:new_link_ok:{pid}:0")
    new_start = re.search(r"start=(r_\w+)", svc.get_referrer(pid).link).group(1)
    assert new_start != start
    body, _ = text(700001, f"/start {start}")
    assert "пригласил" not in body
    assert svc.get_referrer(pid).balance.referrals == 2

    # ---- sync screen shows when data arrived
    body, _ = press(ADMIN, "n:sync:0")
    assert "только что" in body and "kago-sync" in body

    # ---- disable, then delete
    press(ADMIN, f"r:toggle:{pid}:0")
    body, _ = text(REFERRER, "/start")
    assert "Ваш Telegram ID" in body
    body, _ = text(800001, f"/start {new_start}")
    assert "пригласил" not in body  # a disabled referrer's link doesn't count
    body, _ = press(ADMIN, f"r:delete_ok:{pid}:0")
    assert "удалён" in body and svc.referrer_by_telegram(REFERRER) is None

    # unknown admin text falls back to the menu
    body, _ = text(ADMIN, "hello")
    assert "KAGO REFERRER" in body
