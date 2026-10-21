"""Telegram bot end to end: real dispatcher and handlers, a fake Telegram API, real databases.

Needs TEST_DATABASE_URL and TEST_REMNASHOP_DSN (see test_app.py); runs after test_app.py
and reuses its app_client fixture for the panel setup.
"""

import asyncio
import itertools
import re
from datetime import datetime, timezone

import pytest

from test_app import DB_URL, RS_DSN, app_client, csrf, ev, login, post, token  # noqa: F401

pytestmark = pytest.mark.skipif(not (DB_URL and RS_DSN), reason="test databases are not set")

ADMIN = 5689226201
REFERRER_TG = 100002  # bot user 2 in tests/seed.py, an active referrer
STRANGER = 777


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
    return {b.text: b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data}


def button(kb, prefix: str) -> str:
    found = [data for text, data in buttons(kb).items() if text.startswith(prefix)]
    assert found, f"no button {prefix!r} in {list(buttons(kb))}"
    return found[0]


@pytest.fixture(scope="module")
def tg(app_client):  # noqa: F811
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties

    from app import botdb
    from app.bot.handlers import build_admin_router, build_referrer_router
    from app.config import get_settings
    from app.db import engine
    from seed import seed

    settings = get_settings()
    settings.remnashop_dsn = RS_DSN
    settings.vpn_bot_username = "kago_vpn_bot"
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices")
        conn.exec_driver_sql("DELETE FROM accounts WHERE role = 'partner'")
        conn.exec_driver_sql("DELETE FROM settings")
    seed(RS_DSN)
    assert botdb.sync_once()["error"] is None

    fake = FakeTelegram()
    bot = Bot("42:TEST", session=fake.session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_admin_router({ADMIN}))
    dp.include_router(build_referrer_router())
    loop = asyncio.new_event_loop()
    counter = itertools.count(1)

    def feed(update):
        loop.run_until_complete(dp.feed_update(bot, update))

    def text(user_id: int, body: str):
        from aiogram.types import Chat, Message, Update, User

        msg = Message(
            message_id=next(counter),
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="U"),
            text=body,
            entities=[{"type": "bot_command", "offset": 0, "length": len(body.split()[0])}]
            if body.startswith("/") else None,
        )
        feed(Update(update_id=next(counter), message=msg))
        return fake.last(user_id)

    def press(user_id: int, data: str):
        from aiogram.types import CallbackQuery, Chat, Message, Update, User

        cq = CallbackQuery(
            id=str(next(counter)),
            from_user=User(id=user_id, is_bot=False, first_name="U"),
            chat_instance="x",
            data=data,
            message=Message(
                message_id=next(counter),
                date=datetime.now(timezone.utc),
                chat=Chat(id=user_id, type="private"),
                text="old",
            ),
        )
        feed(Update(update_id=next(counter), callback_query=cq))
        return fake.last(user_id)

    yield fake, text, press
    loop.close()
    settings.remnashop_dsn = ""
    settings.vpn_bot_username = ""


def expected_due(referrer_id: int, percent: int = 50, signup: int = 1) -> int:
    import psycopg

    with psycopg.connect(RS_DSN) as conn:
        attracted, plan_days = conn.execute(
            """WITH s AS (SELECT referred_id AS id FROM referrals WHERE referrer_id = %(r)s)
               SELECT (SELECT COUNT(*) FROM s),
                      (SELECT COALESCE(SUM((t.plan_snapshot->>'duration')::int), 0) FROM transactions t
                       WHERE t.user_id IN (SELECT id FROM s) AND t.status = 'COMPLETED' AND NOT t.is_test
                         AND (t.pricing->>'final_amount')::numeric > 0)""",
            {"r": referrer_id},
        ).fetchone()
    return attracted * signup + plan_days * percent // 100


def test_bot_admin_and_referrer_flow(tg, app_client, token):  # noqa: F811
    from app.bot import service as svc

    fake, text, press = tg

    # strangers only get their Telegram ID
    body, _ = text(STRANGER, "/start")
    assert f"<code>{STRANGER}</code>" in body and "администратору" in body

    body, kb = text(ADMIN, "/start")
    assert "KAGO REFERRER" in body and "Рефереров: <b>0</b>" in body

    # ---- add a referrer by Telegram ID
    body, kb = press(ADMIN, button(kb, "➕"))
    assert "Новый реферер" in body
    body, kb = text(ADMIN, "999999999")
    assert "не найден" in body
    body, kb = text(ADMIN, str(REFERRER_TG))
    assert "Найден пользователь VPN-бота" in body and "Юзер 2" in body
    body, kb = press(ADMIN, button(kb, "✅ Добавить"))
    assert "Реферер добавлен" in body
    password = re.search(r"Пароль: <code>([^<]+)</code>", body).group(1)
    login_name = re.search(r"Логин: <code>([^<]+)</code>", body).group(1)

    ref = svc.referrer_by_telegram(REFERRER_TG)
    assert ref.partner.referral_link == "https://t.me/kago_vpn_bot?start=ref_ref2"
    due = expected_due(2)
    assert ref.balance.due == due > 0

    # the same person can't be added twice
    body, kb = press(ADMIN, "n:add:0")
    body, kb = text(ADMIN, "@user2" if ref.username else str(REFERRER_TG))
    assert "уже реферер" in body

    # ---- card, payout of everything due
    pid = ref.partner.id
    body, kb = press(ADMIN, f"r:card:{pid}:0")
    assert f"К выдаче: <b>{due} " in body and "ref_ref2" in body
    body, kb = press(ADMIN, button(kb, "🎁 Выдать"))
    assert "Сначала продлите" in body
    body, kb = press(ADMIN, button(kb, "✅ Да"))
    assert "Записана выдача" in body and "реферер уведомлён" in body
    assert "К выдаче: <b>0 " in body
    note, _ = fake.last(REFERRER_TG)
    assert "Вам выдано" in note

    # custom correction
    press(ADMIN, f"r:pay_custom:{pid}:0")
    body, kb = text(ADMIN, "abc")
    assert "целое число" in body
    body, kb = text(ADMIN, "-3")
    assert "Записана выдача -3" in body and "К выдаче: <b>3 " in body

    # ---- individual rate: 100% of purchases
    press(ADMIN, f"r:set_percent:{pid}:0")
    body, kb = text(ADMIN, "100")
    assert "Ставка сохранена" in body
    assert svc.get_referrer(pid).balance.due == expected_due(2, percent=100) - due + 3
    press(ADMIN, f"r:rates_reset:{pid}:0")
    assert svc.get_referrer(pid).balance.due == 3

    # ---- global rules: 2 days per invite, only first purchases
    body, kb = press(ADMIN, "n:rules:0")
    assert "1 день за каждого приглашённого + 50%" in body
    press(ADMIN, button(kb, "За приглашённого"))
    body, kb = text(ADMIN, "2")
    assert "2 дня за каждого приглашённого" in body
    body, kb = press(ADMIN, button(kb, "Продления"))
    assert "первой купленной" in body
    assert svc.get_referrer(pid).balance.accrued < expected_due(2, signup=2)
    svc.update_rules(signup_days=1, renewals=True)

    # ---- link, access
    press(ADMIN, f"r:link:{pid}:0")
    body, _ = text(ADMIN, "not a link")
    assert "https://" in body
    body, _ = text(ADMIN, "https://t.me/kago_vpn_bot?start=custom")
    assert "Ссылка сохранена" in body
    body, _ = press(ADMIN, f"r:link_refresh:{pid}:0")
    assert "ref_ref2" in body
    body, _ = press(ADMIN, f"r:access_ok:{pid}:0")
    new_password = re.search(r"Пароль: <code>([^<]+)</code>", body).group(1)
    assert new_password != password and "Отправлено рефереру" in body
    r = login(app_client, login_name, new_password)
    assert "Заработано за период" in r.text

    # ---- the referrer's own screen
    body, kb = text(REFERRER_TG, "/start")
    assert "ref_ref2" in body and "Приглашено" in body and "К выдаче" in body
    body, _ = text(REFERRER_TG, "/link")
    assert "Ваша реферальная ссылка" in body

    # ---- notifications: the first round remembers, a new invite is reported
    assert svc.collect_changes() == []
    post(app_client, token, [ev("bot-new-ref", "user.registered", {"user_id": 990001, "referrer_id": 2})])
    changes = svc.collect_changes()
    assert [(c.telegram_id, c.new_referrals, c.new_days) for c in changes] == [(REFERRER_TG, 1, 1)]
    assert svc.collect_changes() == []

    # ---- disable, then delete
    press(ADMIN, f"r:toggle:{pid}:0")
    body, _ = text(REFERRER_TG, "/start")
    assert "Ваш Telegram ID" in body
    body, _ = press(ADMIN, f"r:delete_ok:{pid}:0")
    assert "удалён" in body and svc.referrer_by_telegram(REFERRER_TG) is None

    # unknown admin text falls back to the menu
    body, _ = text(ADMIN, "hello")
    assert "KAGO REFERRER" in body
