"""Telegram handlers: admin menu (referrers, payouts, rules), the referrer's own screen and
the "someone should get days" notifications."""

import asyncio
import logging
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import get_settings
from . import service as svc
from . import texts

log = logging.getLogger("kago.bot")

PAGE = 8


class Nav(CallbackData, prefix="n"):
    to: str  # menu | list | add | rules | sync
    page: int = 0


class Ref(CallbackData, prefix="r"):
    act: str
    id: int
    arg: int = 0


class Rule(CallbackData, prefix="g"):
    act: str  # signup


class Input(StatesGroup):
    add_query = State()
    payout_days = State()
    signup_days = State()
    rule_signup = State()


def run(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    """Edit the message the button belongs to, or answer with a new one."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                await target.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def notify(bot: Bot, telegram_id: int | None, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    if not telegram_id:
        return False
    try:
        await bot.send_message(telegram_id, text, reply_markup=kb, disable_web_page_preview=True)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("can't message %s: %s", telegram_id, exc)
        return False


def back(to: str = "menu", text: str = "« Меню") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=Nav(to=to).pack())
    return kb.as_markup()


def to_card(partner_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« К рефереру", callback_data=Ref(act="card", id=partner_id).pack())
    return kb.as_markup()


def cancel_kb(partner_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if partner_id:
        kb.button(text="Отмена", callback_data=Ref(act="card", id=partner_id).pack())
    else:
        kb.button(text="Отмена", callback_data=Nav(to="menu").pack())
    return kb.as_markup()


def parse_int(text: str | None, lo: int, hi: int) -> int | None:
    try:
        n = int((text or "").strip().replace(" ", ""))
    except ValueError:
        return None
    return n if lo <= n <= hi else None


# =========================================================================== admin


def build_admin_router(admin_ids: set[int]) -> Router:
    r = Router(name="admin")
    r.message.filter(F.from_user.id.in_(admin_ids))
    r.callback_query.filter(F.from_user.id.in_(admin_ids))

    # ----------------------------------------------------------------- menu

    def menu_kb() -> InlineKeyboardMarkup:
        kb = InlineKeyboardBuilder()
        kb.button(text="👥 Рефереры", callback_data=Nav(to="list").pack())
        kb.button(text="➕ Добавить", callback_data=Nav(to="add").pack())
        kb.button(text="⚙️ Правила начисления", callback_data=Nav(to="rules").pack())
        if texts.panel_url():
            kb.button(text="🌐 Веб-панель", url=texts.panel_url() + "/admin")
        kb.adjust(2, 1, 1)
        return kb.as_markup()

    async def menu(target, state: FSMContext):
        await state.clear()
        rows = await run(svc.list_referrers)
        rules = await run(svc.get_rules)
        await show(target, texts.menu(svc.totals(rows), rules), menu_kb())

    @r.message(CommandStart())
    @r.message(Command("menu"))
    async def cmd_start(m: Message, state: FSMContext):
        await menu(m, state)

    @r.callback_query(Nav.filter(F.to == "menu"))
    async def cb_menu(cb: CallbackQuery, state: FSMContext):
        await menu(cb, state)

    @r.message(Command("cancel"))
    async def cmd_cancel(m: Message, state: FSMContext):
        await menu(m, state)

    # ----------------------------------------------------------------- list

    @r.callback_query(Nav.filter(F.to == "list"))
    async def cb_list(cb: CallbackQuery, callback_data: Nav, state: FSMContext):
        await state.clear()
        rows = await run(svc.list_referrers)
        pages = max(1, -(-len(rows) // PAGE))
        page = min(max(callback_data.page, 0), pages - 1)
        kb = InlineKeyboardBuilder()
        for x in rows[page * PAGE : (page + 1) * PAGE]:
            b = x.balance
            mark = "" if x.partner.is_active else "⏸ "
            due = f" · ⏳{b.due}" if b.due > 0 else ""
            kb.button(
                text=f"{mark}{x.partner.name[:24]} · 🔗{x.clicks} · 👥{b.referrals}{due}",
                callback_data=Ref(act="card", id=x.partner.id).pack(),
            )
        nav = []
        if page > 0:
            nav.append(("«", Nav(to="list", page=page - 1)))
        if page < pages - 1:
            nav.append(("»", Nav(to="list", page=page + 1)))
        for text, data in nav:
            kb.button(text=text, callback_data=data.pack())
        kb.button(text="➕ Добавить", callback_data=Nav(to="add").pack())
        kb.button(text="« Меню", callback_data=Nav(to="menu").pack())
        kb.adjust(*([1] * len(rows[page * PAGE : (page + 1) * PAGE])), len(nav) or 1, 2)
        if rows:
            head = (
                f"<b>Рефереры</b> ({len(rows)}), стр. {page + 1}/{pages}\n"
                "🔗 переходов · 👥 рефералов · ⏳ к выдаче, дн."
            )
        else:
            head = "<b>Рефереров пока нет.</b> Нажмите «Добавить»."
        await show(cb, head, kb.as_markup())

    # ----------------------------------------------------------------- add

    @r.callback_query(Nav.filter(F.to == "add"))
    async def cb_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Input.add_query)
        await show(
            cb,
            "<b>Новый реферер</b>\n\nПришлите его <b>Telegram ID</b>, <b>@username</b> или перешлите сюда "
            "любое его сообщение.\n\n@username находится, только если человек уже писал этому боту. "
            "Telegram ID подходит всегда (узнать его можно командой /id в этом боте).",
            cancel_kb(),
        )

    @r.message(Input.add_query)
    async def on_add_query(m: Message, state: FSMContext):
        origin = m.forward_origin
        if isinstance(origin, MessageOriginUser):
            query = str(origin.sender_user.id)
        elif isinstance(origin, MessageOriginHiddenUser):
            await m.answer(
                "У этого человека скрыт аккаунт при пересылке. Пришлите его Telegram ID или @username.",
                reply_markup=cancel_kb(),
            )
            return
        else:
            query = (m.text or "").strip()
        if not query or len(query) > 64:
            await m.answer("Пришлите Telegram ID, @username или пересланное сообщение.", reply_markup=cancel_kb())
            return
        person = await run(svc.find_person, query)
        if not person:
            await m.answer(
                f"<code>{escape(query)}</code> не найден. Попросите человека написать этому боту /start "
                "и попробуйте снова — или пришлите его Telegram ID.",
                reply_markup=cancel_kb(),
            )
            return
        existing = await run(svc.referrer_by_telegram, person["telegram_id"])
        if existing:
            await state.clear()
            await m.answer("Этот человек уже реферер:", reply_markup=to_card(existing.partner.id))
            return
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Добавить", callback_data=Ref(act="create", id=int(person["telegram_id"])).pack())
        kb.button(text="Отмена", callback_data=Nav(to="menu").pack())
        await m.answer(
            f"<b>{escape(svc.person_label(person))}</b>\n\nСделать его реферером?",
            reply_markup=kb.as_markup(),
        )

    @r.callback_query(Ref.filter(F.act == "create"))
    async def cb_create(cb: CallbackQuery, callback_data: Ref, bot: Bot):
        person = await run(svc.find_person, str(callback_data.id))
        try:
            ref, password = await run(svc.create_referrer, person)
        except svc.BotError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        sent = await notify(bot, ref.partner.telegram_id, texts.link_message(ref)) if ref.link else False
        info = (
            "Ссылка отправлена рефереру."
            if sent
            else "Рефереру пока не написать: пусть запустит этого бота, затем нажмите «Отправить ссылку»."
        )
        await show(
            cb,
            f"✅ Реферер добавлен.\n\n{texts.link_line(ref)}\n{info}\n\n"
            f"{texts.access_message(ref.partner.login, password)}\n"
            "<i>Пароль показан один раз; кнопка «Доступ в панель» выпустит новый.</i>",
            to_card(ref.partner.id),
        )

    # ----------------------------------------------------------------- card

    def card_kb(x: svc.Referrer) -> InlineKeyboardMarkup:
        pid, b = x.partner.id, x.balance
        kb = InlineKeyboardBuilder()
        if b.due > 0:
            kb.button(text=f"🎁 Выдать {texts.days(b.due)}", callback_data=Ref(act="pay", id=pid, arg=b.due).pack())
        kb.button(text="✍️ Выдать другое кол-во", callback_data=Ref(act="pay_custom", id=pid).pack())
        kb.button(text="👥 Переходы", callback_data=Ref(act="visits", id=pid).pack())
        kb.button(text="📤 Отправить ссылку", callback_data=Ref(act="send_link", id=pid).pack())
        kb.button(text="🔄 Новая ссылка", callback_data=Ref(act="new_link", id=pid).pack())
        kb.button(text="⚙️ Ставки", callback_data=Ref(act="rates", id=pid).pack())
        kb.button(text="🔑 Доступ в панель", callback_data=Ref(act="access", id=pid).pack())
        kb.button(text="🧾 Выдачи", callback_data=Ref(act="history", id=pid).pack())
        kb.button(
            text="⏸ Отключить" if x.partner.is_active else "▶️ Включить",
            callback_data=Ref(act="toggle", id=pid).pack(),
        )
        kb.button(text="🗑 Удалить", callback_data=Ref(act="delete", id=pid).pack())
        kb.button(text="« К списку", callback_data=Nav(to="list").pack())
        kb.adjust(*([1] if b.due > 0 else []), 1, 2, 2, 2, 2, 2)
        return kb.as_markup()

    async def card(target, partner_id: int, prefix: str = ""):
        try:
            x = await run(svc.get_referrer, partner_id)
        except svc.BotError as exc:
            await show(target, str(exc), back("list", "« К списку"))
            return
        await show(target, prefix + texts.referrer_card(x), card_kb(x))

    @r.callback_query(Ref.filter(F.act == "card"))
    async def cb_card(cb: CallbackQuery, callback_data: Ref, state: FSMContext):
        await state.clear()
        await card(cb, callback_data.id)

    # ----------------------------------------------------------------- payouts

    @r.callback_query(Ref.filter(F.act == "pay"))
    async def cb_pay(cb: CallbackQuery, callback_data: Ref):
        x = await run(svc.get_referrer, callback_data.id)
        kb = InlineKeyboardBuilder()
        kb.button(
            text=f"✅ Да, выдано {texts.days(callback_data.arg)}",
            callback_data=Ref(act="pay_ok", id=callback_data.id, arg=callback_data.arg).pack(),
        )
        kb.button(text="Отмена", callback_data=Ref(act="card", id=callback_data.id).pack())
        kb.adjust(1)
        await show(
            cb,
            f"Выдать <b>{texts.days(callback_data.arg)}</b> рефереру {escape(x.partner.name)}?\n\n"
            "Сначала продлите ему подписку, затем подтвердите — "
            "бот запишет выдачу и уведомит реферера.",
            kb.as_markup(),
        )

    async def do_payout(target, bot: Bot, partner_id: int, days_n: int):
        try:
            x = await run(svc.add_payout, partner_id, days_n)
        except svc.BotError as exc:
            await show(target, str(exc), to_card(partner_id))
            return
        sent = await notify(bot, x.partner.telegram_id, texts.payout_message(days_n, x))
        info = "реферер уведомлён" if sent else "уведомить реферера не удалось — он ещё не запускал этого бота"
        await card(target, partner_id, f"✅ Записана выдача {texts.days(days_n)} ({info}).\n\n")

    @r.callback_query(Ref.filter(F.act == "pay_ok"))
    async def cb_pay_ok(cb: CallbackQuery, callback_data: Ref, bot: Bot):
        await do_payout(cb, bot, callback_data.id, callback_data.arg)

    @r.callback_query(Ref.filter(F.act == "pay_custom"))
    async def cb_pay_custom(cb: CallbackQuery, callback_data: Ref, state: FSMContext):
        await state.set_state(Input.payout_days)
        await state.update_data(pid=callback_data.id)
        await show(
            cb,
            "Сколько дней выдать? Пришлите число.\nОтрицательное число — корректировка (например, -3).",
            cancel_kb(callback_data.id),
        )

    @r.message(Input.payout_days)
    async def on_payout_days(m: Message, state: FSMContext, bot: Bot):
        pid = (await state.get_data())["pid"]
        n = parse_int(m.text, -100000, 100000)
        if not n:
            await m.answer("Нужно целое число, не ноль.", reply_markup=cancel_kb(pid))
            return
        await state.clear()
        await do_payout(m, bot, pid, n)

    @r.callback_query(Ref.filter(F.act == "history"))
    async def cb_history(cb: CallbackQuery, callback_data: Ref):
        rows = await run(svc.last_payouts, callback_data.id, 10)
        lines = [
            f"{p.created_at.astimezone(get_settings().tz):%d.%m.%Y %H:%M} · <b>{p.days:+d}</b> · {escape(p.note or '—')}"
            for p in rows
        ]
        await show(
            cb,
            "<b>Последние выдачи</b>\n\n" + ("\n".join(lines) if lines else "Выдач ещё не было."),
            to_card(callback_data.id),
        )

    # ----------------------------------------------------------------- link

    @r.callback_query(Ref.filter(F.act == "send_link"))
    async def cb_send_link(cb: CallbackQuery, callback_data: Ref, bot: Bot):
        x = await run(svc.get_referrer, callback_data.id)
        sent = await notify(bot, x.partner.telegram_id, texts.link_message(x))
        if sent:
            await cb.answer("Ссылка отправлена рефереру")
        else:
            me = await bot.get_me()
            await cb.answer(
                f"Не удалось: реферер должен сначала запустить @{me.username}. Перешлите ему ссылку на бота.",
                show_alert=True,
            )

    @r.callback_query(Ref.filter(F.act == "new_link"))
    async def cb_new_link(cb: CallbackQuery, callback_data: Ref):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Да, выпустить новую", callback_data=Ref(act="new_link_ok", id=callback_data.id).pack())
        kb.button(text="Отмена", callback_data=Ref(act="card", id=callback_data.id).pack())
        kb.adjust(1)
        await show(
            cb,
            "Выпустить новую ссылку? Старая перестанет работать (например, если её распространяют без "
            "спроса). Уже пришедшие по ней люди останутся за реферером.",
            kb.as_markup(),
        )

    @r.callback_query(Ref.filter(F.act == "new_link_ok"))
    async def cb_new_link_ok(cb: CallbackQuery, callback_data: Ref):
        await run(svc.new_link, callback_data.id)
        await card(cb, callback_data.id, "✅ Новая ссылка выпущена — отправьте её рефереру.\n\n")

    @r.callback_query(Ref.filter(F.act == "visits"))
    async def cb_visits(cb: CallbackQuery, callback_data: Ref):
        x = await run(svc.get_referrer, callback_data.id)
        rows, total, counted = await run(svc.visits, callback_data.id, 30)
        await show(cb, texts.visits_list(rows, total, counted, x.partner.name), to_card(callback_data.id))

    # ----------------------------------------------------------------- rates

    @r.callback_query(Ref.filter(F.act == "rates"))
    async def cb_rates(cb: CallbackQuery, callback_data: Ref, state: FSMContext):
        await state.clear()
        x = await run(svc.get_referrer, callback_data.id)
        defaults = await run(svc.get_rules)
        p, pid = x.partner, x.partner.id

        own = (
            f"<b>{texts.days(p.reward_signup_days)}</b> (индивидуальная)"
            if p.reward_signup_days is not None
            else f"{texts.days(defaults.signup_days)} (общая)"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="Изменить", callback_data=Ref(act="set_signup", id=pid).pack())
        if p.reward_signup_days is not None:
            kb.button(text="↩️ Сбросить к общей", callback_data=Ref(act="rates_reset", id=pid).pack())
        kb.button(text="« К рефереру", callback_data=Ref(act="card", id=pid).pack())
        kb.adjust(1)
        await show(cb, f"<b>Ставка: {escape(p.name)}</b>\n\nЗа каждого реферала: {own}", kb.as_markup())

    @r.callback_query(Ref.filter(F.act == "set_signup"))
    async def cb_set_rate(cb: CallbackQuery, callback_data: Ref, state: FSMContext):
        await state.set_state(Input.signup_days)
        await state.update_data(pid=callback_data.id)
        await show(cb, "Сколько дней за каждого реферала? Пришлите число.", cancel_kb(callback_data.id))

    @r.message(Input.signup_days)
    async def on_rate(m: Message, state: FSMContext):
        pid = (await state.get_data())["pid"]
        n = parse_int(m.text, 0, 3650)
        if n is None:
            await m.answer("Нужно целое неотрицательное число.", reply_markup=cancel_kb(pid))
            return
        await state.clear()
        await run(svc.set_rates, pid, signup_days=n)
        await card(m, pid, "✅ Ставка сохранена.\n\n")

    @r.callback_query(Ref.filter(F.act == "rates_reset"))
    async def cb_rates_reset(cb: CallbackQuery, callback_data: Ref):
        await run(svc.set_rates, callback_data.id, signup_days=None)
        await card(cb, callback_data.id, "✅ Ставка сброшена к общей.\n\n")

    # ----------------------------------------------------------------- access, toggle, delete

    @r.callback_query(Ref.filter(F.act == "access"))
    async def cb_access(cb: CallbackQuery, callback_data: Ref):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔑 Выпустить новый пароль", callback_data=Ref(act="access_ok", id=callback_data.id).pack())
        kb.button(text="Отмена", callback_data=Ref(act="card", id=callback_data.id).pack())
        kb.adjust(1)
        await show(
            cb,
            "Реферер может смотреть подробную статистику в веб-панели.\n"
            "Новый пароль заменит старый; бот отправит его рефереру и покажет вам.",
            kb.as_markup(),
        )

    @r.callback_query(Ref.filter(F.act == "access_ok"))
    async def cb_access_ok(cb: CallbackQuery, callback_data: Ref, bot: Bot):
        login, password = await run(svc.reset_password, callback_data.id)
        x = await run(svc.get_referrer, callback_data.id)
        sent = await notify(bot, x.partner.telegram_id, texts.access_message(login, password))
        info = "Отправлено рефереру." if sent else "Рефереру отправить не удалось — передайте сами."
        await show(cb, f"{texts.access_message(login, password)}\n\n{info}", to_card(callback_data.id))

    @r.callback_query(Ref.filter(F.act == "toggle"))
    async def cb_toggle(cb: CallbackQuery, callback_data: Ref):
        active = await run(svc.toggle_active, callback_data.id)
        await card(cb, callback_data.id, "▶️ Включён.\n\n" if active else "⏸ Отключён.\n\n")

    @r.callback_query(Ref.filter(F.act == "delete"))
    async def cb_delete(cb: CallbackQuery, callback_data: Ref):
        x = await run(svc.get_referrer, callback_data.id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Да, удалить", callback_data=Ref(act="delete_ok", id=callback_data.id).pack())
        kb.button(text="Отмена", callback_data=Ref(act="card", id=callback_data.id).pack())
        kb.adjust(1)
        await show(
            cb,
            f"Удалить реферера <b>{escape(x.partner.name)}</b>?\n\nИстория выдач и доступ в панель будут удалены. "
            "Засчитанные рефералы тоже удалятся. Если нужно просто приостановить — используйте «Отключить».",
            kb.as_markup(),
        )

    @r.callback_query(Ref.filter(F.act == "delete_ok"))
    async def cb_delete_ok(cb: CallbackQuery, callback_data: Ref):
        try:
            name = await run(svc.delete_referrer, callback_data.id)
        except svc.BotError as exc:
            await show(cb, str(exc), back("list", "« К списку"))
            return
        await show(cb, f"🗑 Реферер «{escape(name)}» удалён.", back("list", "« К списку"))

    # ----------------------------------------------------------------- global rules

    async def rules_screen(target):
        rules = await run(svc.get_rules)
        kb = InlineKeyboardBuilder()
        kb.button(text=f"За реферала: {texts.days(rules.signup_days)} — изменить", callback_data=Rule(act="signup").pack())
        kb.button(text="« Меню", callback_data=Nav(to="menu").pack())
        kb.adjust(1)
        await show(
            target,
            "<b>Правила начисления</b> (для всех, у кого нет индивидуальной ставки)\n\n"
            f"{escape(texts.rules_line(rules))}.\n\n"
            "Изменение пересчитывает и уже начисленные дни.",
            kb.as_markup(),
        )

    @r.callback_query(Nav.filter(F.to == "rules"))
    async def cb_rules(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await rules_screen(cb)

    @r.callback_query(Rule.filter(F.act == "signup"))
    async def cb_rule_value(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Input.rule_signup)
        await show(cb, "Сколько дней за каждого реферала? Пришлите число.", back("rules", "Отмена"))

    @r.message(Input.rule_signup)
    async def on_rule_value(m: Message, state: FSMContext):
        n = parse_int(m.text, 0, 3650)
        if n is None:
            await m.answer("Нужно целое неотрицательное число.", reply_markup=back("rules", "Отмена"))
            return
        await state.clear()
        await run(svc.update_rules, signup_days=n)
        await rules_screen(m)

    @r.message(Command("id"))
    async def cmd_id(m: Message):
        await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")

    @r.message()
    async def fallback(m: Message, state: FSMContext):
        await menu(m, state)

    return r


# =========================================================================== referrers


def build_referrer_router() -> Router:
    r = Router(name="referrer")

    def site_kb() -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        b.button(text="🌐 KAGO VPN", url=get_settings().referral_redirect_url)
        return b.as_markup()

    def kb() -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        b.button(text="🔄 Обновить", callback_data="me")
        if texts.panel_url():
            b.button(text="🌐 Подробная статистика", url=texts.panel_url() + "/login")
        b.adjust(1)
        return b.as_markup()

    async def home(target, user_id: int):
        x = await run(svc.referrer_by_telegram, user_id)
        if not x or not x.partner.is_active:
            await show(target, texts.not_registered(user_id), site_kb())
            return
        await show(target, texts.referrer_home(x), kb())

    @r.message(CommandStart())
    @r.message(Command("stats"))
    async def cmd_start(m: Message):
        await home(m, m.from_user.id)

    @r.message(Command("link"))
    async def cmd_link(m: Message):
        x = await run(svc.referrer_by_telegram, m.from_user.id)
        if not x or not x.partner.is_active or not x.link:
            await home(m, m.from_user.id)
            return
        await m.answer(texts.link_message(x), disable_web_page_preview=True)

    @r.callback_query(F.data == "me")
    async def cb_me(cb: CallbackQuery):
        await home(cb, cb.from_user.id)

    @r.message(Command("id"))
    async def cmd_id(m: Message):
        await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")

    @r.message()
    async def fallback(m: Message):
        await home(m, m.from_user.id)

    return r


# =========================================================================== setup


class ContactMiddleware(BaseMiddleware):
    """Remember everyone who writes to the bot, so referrers can be found by @username."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            name = " ".join(x for x in (u.first_name, u.last_name) if x)
            try:
                await run(svc.touch_contact, u.id, u.username, name)
            except Exception:
                log.exception("can't save contact %s", u.id)
        return await handler(event, data)


def setup(dp: Dispatcher, admin_ids: set[int]) -> None:
    dp.update.outer_middleware(ContactMiddleware())
    dp.include_router(build_admin_router(admin_ids))
    dp.include_router(build_referrer_router())


# =========================================================================== notifications


def due_kb(x: svc.Referrer) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    due = x.balance.due
    kb.button(text=f"✅ Выдал {texts.days(due)}", callback_data=Ref(act="pay_ok", id=x.partner.id, arg=due).pack())
    kb.button(text="Открыть карточку", callback_data=Ref(act="card", id=x.partner.id).pack())
    kb.adjust(1)
    return kb.as_markup()


async def notify_loop(bot: Bot, interval: int, admin_ids: set[int]) -> None:
    """New referrals: the referrer is told, the admins get "issue N days" with a button."""
    await asyncio.sleep(5)
    while True:
        try:
            for change in await run(svc.collect_changes):
                await notify(bot, change.telegram_id, texts.change_message(change))
                if change.referrer.balance.due > 0:
                    for admin_id in admin_ids:
                        await notify(bot, admin_id, texts.due_message(change), due_kb(change.referrer))
                await asyncio.sleep(0.05)  # stay far below Telegram's rate limits
        except Exception:
            log.exception("notification round failed")
        await asyncio.sleep(interval)
