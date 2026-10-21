"""Referral links and the Telegram bot end to end: real dispatcher and handlers, a fake
Telegram API, the real panel database; people open the referral link through the panel app.

Needs TEST_DATABASE_URL (see test_app.py); reuses its app_client fixture.
"""

import asyncio
import itertools
import re
from datetime import datetime, timezone

import pytest

from test_app import DB_URL, app_client, login  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL is not set")

ADMIN = 5689226201
REFERRER = 500001
BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


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

    from app.bot.handlers import setup
    from app.config import get_settings
    from app.db import engine

    settings = get_settings()
    settings.public_url = "https://panel.example"
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE events, bot_users, payments, subscriptions, devices, referral_visits, bot_contacts")
        conn.exec_driver_sql("DELETE FROM accounts WHERE role = 'partner'")
        conn.exec_driver_sql("DELETE FROM settings")

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
    settings.public_url = ""


def open_link(client, path, ua=BROWSER, cookie=None):
    """Someone opens the referral link: the panel answers with a redirect to the site.

    The test client always comes from the address "testclient"."""
    client.cookies.clear()
    headers = {"User-Agent": ua}
    if cookie:
        headers["Cookie"] = f"kago_ref={cookie}"
    r = client.get(path, headers=headers, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://usekago.net"
    return r.cookies.get("kago_ref")


def test_bot_referrer_flow(tg, app_client):  # noqa: F811
    from app.bot import service as svc
    from app.bot.handlers import due_kb
    from app.bot.texts import due_message

    fake, text, press = tg
    client = app_client

    # ---- the referrer writes to the bot first: only gets their ID
    body, _ = text(REFERRER, "/start", "best_blogger")
    assert f"<code>{REFERRER}</code>" in body

    body, kb = text(ADMIN, "/start")
    assert "KAGO REFERRER" in body and "Рефереров: <b>0</b>" in body

    # ---- add the referrer by @username (known from the contact)
    press(ADMIN, button(kb, "➕"))
    body, _ = text(ADMIN, "@nobody_here")
    assert "не найден" in body
    body, kb = text(ADMIN, "@best_blogger")
    assert "@best_blogger" in body and f"tg {REFERRER}" in body
    body, kb = press(ADMIN, button(kb, "✅ Добавить"))
    assert "Реферер добавлен" in body and "Ссылка отправлена рефереру" in body
    m = re.search(r"https://panel\.example(/r/\w+)", body)
    assert m, body
    path = m.group(1)
    assert path in fake.last(REFERRER)[0]  # the referrer got the link
    login_name = re.search(r"Логин: <code>([^<]+)</code>", body).group(1)
    pid = svc.referrer_by_telegram(REFERRER).partner.id

    # the same person can't be added twice
    press(ADMIN, "n:add:0")
    body, _ = text(ADMIN, str(REFERRER))
    assert "уже реферер" in body

    # ---- people open the link
    assert svc.collect_changes() == []  # notifications: the first round only remembers
    alice = open_link(client, path)
    assert alice
    open_link(client, path, cookie=alice)  # alice again: repeat
    open_link(client, path, ua="TelegramBot (like TwitterBot)")  # link preview
    open_link(client, path, ua="")  # no user agent: a script
    open_link(client, "/r/unknown123")  # unknown code: just redirected
    open_link(client, "/r/bad%20code!")  # malformed code: just redirected

    ref = svc.get_referrer(pid)
    assert (ref.clicks, ref.balance.referrals, ref.balance.due) == (4, 1, 1)

    # same address, new browser within 30 days: not counted; then a counted visit from a new address
    from app.db import SessionLocal
    from app.referrals import record_visit

    code = path.rsplit("/", 1)[1]
    with SessionLocal() as db:
        v, _ = record_visit(db, code, "testclient", BROWSER, "", None)  # the address of alice above
        assert (v.counted, v.reason) == (False, "ip")
        v, _ = record_visit(db, code, "2001:db8:1:2::1", BROWSER, "", None)
        assert v.counted
        v, _ = record_visit(db, code, "2001:db8:1:2::ffff", BROWSER, "", None)  # same /64
        assert (v.counted, v.reason) == (False, "ip")
        v, _ = record_visit(db, code, "8.8.8.8", BROWSER, "https://t.me/", None)
        assert v.counted
    ref = svc.get_referrer(pid)
    assert (ref.clicks, ref.balance.referrals, ref.balance.due) == (8, 3, 3)

    # ---- notifications: the referrer hears about it, the admins get "issue N days"
    changes = svc.collect_changes()
    assert [(c.telegram_id, c.new_referrals, c.new_days) for c in changes] == [(REFERRER, 3, 3)]
    assert "Нужно выдать: <b>3 дня</b>" in due_message(changes[0])
    issue = button(due_kb(changes[0].referrer), "✅ Выдал")
    assert svc.collect_changes() == []

    body, kb = press(ADMIN, f"r:card:{pid}:0")
    assert "Переходов по ссылке: <b>8</b>" in body and "Рефералов: <b>3</b>" in body and "К выдаче: <b>3 дня</b>" in body
    body, _ = press(ADMIN, button(kb, "👥 Переходы"))
    assert "всего 8, засчитано 3" in body and "🔁 повтор" in body and "🤖" in body and "🌐" in body

    # ---- "issued" from the notification button
    body, kb = press(ADMIN, issue)
    assert "Записана выдача 3 дня (реферер уведомлён)" in body and "К выдаче: <b>0 дней</b>" in body
    assert "Вам выдано <b>3 дня</b>" in fake.last(REFERRER)[0]
    press(ADMIN, f"r:pay_custom:{pid}:0")
    body, _ = text(ADMIN, "abc")
    assert "целое число" in body
    body, _ = text(ADMIN, "-1")
    assert "Записана выдача -1 день" in body and "К выдаче: <b>1 день</b>" in body

    # ---- rates: individual 5 days per referral, then the global rule
    body, kb = press(ADMIN, f"r:rates:{pid}:0")
    press(ADMIN, button(kb, "Изменить"))
    body, _ = text(ADMIN, "5")
    assert "Ставка сохранена" in body and svc.get_referrer(pid).balance.accrued == 15
    press(ADMIN, f"r:rates_reset:{pid}:0")
    body, kb = press(ADMIN, "n:rules:0")
    assert "1 день подписки за каждого приглашённого" in body
    press(ADMIN, button(kb, "За реферала"))
    body, kb = text(ADMIN, "2")
    assert "2 дня подписки за каждого приглашённого" in body
    assert svc.get_referrer(pid).balance.accrued == 6
    svc.update_rules(signup_days=1)

    # ---- the referrer's own screen and the web panel
    body, _ = text(REFERRER, "/start")
    assert path in body and "Рефералов: <b>3</b>" in body
    body, _ = text(REFERRER, "/link")
    assert "Ваша реферальная ссылка" in body
    body, _ = press(ADMIN, f"r:access_ok:{pid}:0")
    password = re.search(r"Пароль: <code>([^<]+)</code>", body).group(1)
    page = login(client, login_name, password)
    assert path in page.text and "засчитано рефералов: 3" in page.text

    # ---- a new link: the old one stops counting, referrals stay
    press(ADMIN, f"r:new_link_ok:{pid}:0")
    new_path = svc.get_referrer(pid).link.removeprefix("https://panel.example")
    assert new_path != path
    new_code = new_path.rsplit("/", 1)[1]
    with SessionLocal() as db:
        assert record_visit(db, code, "9.9.9.9", BROWSER, "", None)[0] is None  # the old code is unknown
        assert record_visit(db, new_code, "9.9.9.9", BROWSER, "", None)[0].counted
    assert svc.get_referrer(pid).balance.referrals == 4

    # ---- disable: the link stops counting and the referrer loses access; then delete
    press(ADMIN, f"r:toggle:{pid}:0")
    with SessionLocal() as db:
        assert record_visit(db, new_code, "7.7.7.7", BROWSER, "", None)[0].reason == "off"
    assert svc.get_referrer(pid).balance.referrals == 4
    body, _ = text(REFERRER, "/start")
    assert "Ваш Telegram ID" in body
    body, _ = press(ADMIN, f"r:delete_ok:{pid}:0")
    assert "удалён" in body and svc.referrer_by_telegram(REFERRER) is None

    # unknown admin text falls back to the menu
    body, _ = text(ADMIN, "hello")
    assert "KAGO REFERRER" in body


def test_link_preview_filter():
    from app.referrals import BOT_UA

    people = [
        "Mozilla/5.0 (Linux; Android 13; wv) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36 Telegram-Android/10.5.0",
        "Mozilla/5.0 (Linux; Android 12; Cubot X30) AppleWebKit/537.36 Chrome/119 Mobile Safari/537.36",
        BROWSER,
    ]
    previews = ["TelegramBot (like TwitterBot)", "WhatsApp/2.23.20.0 A", "facebookexternalhit/1.1",
                "Mozilla/5.0 (compatible; Googlebot/2.1)", "Discordbot/2.0", "curl/8.4.0"]
    assert not any(BOT_UA.search(ua) for ua in people)
    assert all(BOT_UA.search(ua) for ua in previews)
