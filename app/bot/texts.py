"""Message texts of the Telegram bot (HTML parse mode)."""

from html import escape

from ..config import get_settings
from ..models import ReferralVisit
from ..services import RewardRules
from .service import REASONS, Change, Referrer, Totals


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def rules_line(r: RewardRules) -> str:
    return f"{days(r.signup_days)} подписки за каждого приглашённого"


def panel_url() -> str:
    url = get_settings().public_url.rstrip("/")
    if not url.startswith("https://") or "127.0.0.1" in url or "localhost" in url:
        return ""
    return url


def menu(t: Totals, rules: RewardRules) -> str:
    return (
        "<b>KAGO REFERRER</b> · управление реферерами\n\n"
        f"👤 Рефереров: <b>{t.referrers}</b> (активных {t.active})\n"
        f"🔗 Переходов по ссылкам: <b>{t.clicks}</b>\n"
        f"👥 Засчитано рефералов: <b>{t.referrals}</b>\n"
        f"🎁 Начислено: {days(t.accrued)} · выдано: {days(t.issued)}\n"
        f"⏳ К выдаче: <b>{days(t.due)}</b>\n\n"
        f"Правила: {escape(rules_line(rules))}"
    )


def _who(r: Referrer) -> str:
    p = r.partner
    parts = [f"<b>{escape(p.name)}</b>"]
    if r.username:
        parts.append(f"@{escape(r.username)}")
    if p.telegram_id:
        parts.append(f"tg <code>{p.telegram_id}</code>")
    return " · ".join(parts)


def _balance(r: Referrer) -> str:
    b = r.balance
    lines = [
        f"🔗 Переходов по ссылке: <b>{r.clicks}</b>",
        f"👥 Рефералов: <b>{b.referrals}</b> × {days(b.rules.signup_days)} = {days(b.signup_reward)}",
    ]
    if b.purchases:  # only when purchase data comes from the VPN bot
        lines.append(
            f"💳 Покупок: <b>{b.purchases}</b> · {b.rules.percent}% от {days(b.plan_days)} = {days(b.purchase_reward)}"
        )
    lines.append(
        f"\n🎁 Начислено: <b>{days(b.accrued)}</b> · выдано: {days(b.issued)}\n"
        f"⏳ К выдаче: <b>{days(b.due)}</b>"
    )
    return "\n".join(lines)


def link_line(r: Referrer) -> str:
    return f"Ссылка: <code>{escape(r.link)}</code>"


def referrer_card(r: Referrer) -> str:
    p = r.partner
    status = "" if p.is_active else "\n⏸ <b>Отключён</b> — переходы по ссылке не засчитываются"
    own = []
    if p.reward_signup_days is not None:
        own.append(f"Индивидуальная ставка: {escape(rules_line(r.balance.rules))}")
    return "\n".join([_who(r) + status, link_line(r), "", _balance(r)] + ([""] + own if own else []))


def referrer_home(r: Referrer) -> str:
    return (
        f"<b>KAGO REFERRER</b>\n\nПривет, {escape(r.partner.name)}!\n\n"
        f"Ваша реферальная ссылка:\n<code>{escape(r.link)}</code>\n\n"
        f"{_balance(r)}\n\n"
        f"<i>Как начисляется: {escape(rules_line(r.balance.rules))}. Засчитывается каждый новый человек, "
        "открывший вашу ссылку; повторные открытия и превью ссылок в мессенджерах не считаются.</i>"
    )


def link_message(r: Referrer) -> str:
    return (
        "🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{escape(r.link)}</code>\n\n"
        f"Начисляем: {escape(rules_line(r.balance.rules))}."
    )


def payout_message(days_n: int, r: Referrer) -> str:
    if days_n > 0:
        head = f"🎁 Вам выдано <b>{days(days_n)}</b> подписки. Спасибо, что приглашаете друзей!"
    else:
        head = f"Корректировка начислений: {days(days_n)}."
    return f"{head}\n\nОсталось к выдаче: {days(max(r.balance.due, 0))}"


def change_message(c: Change) -> str:
    """To the referrer."""
    parts = [f"👥 Новых рефералов: <b>+{c.new_referrals}</b>"] if c.new_referrals else []
    if c.new_purchases:
        parts.append(f"💳 Новых покупок: <b>+{c.new_purchases}</b>")
    gained = f"\n🎁 Начислено: <b>+{days(c.new_days)}</b>" if c.new_days > 0 else ""
    return "\n".join(parts) + gained + f"\n⏳ Всего к выдаче: <b>{days(max(c.referrer.balance.due, 0))}</b>"


def due_message(c: Change) -> str:
    """To the admins: someone should get days."""
    r = c.referrer
    return (
        f"🎁 {_who(r)}\n"
        f"Новых рефералов: <b>+{c.new_referrals}</b> (+{days(c.new_days)})\n\n"
        f"Нужно выдать: <b>{days(max(r.balance.due, 0))}</b>\n"
        "Продлите подписку и нажмите кнопку — реферер получит уведомление."
    )


def access_message(login: str, password: str) -> str:
    url = panel_url()
    where = f"\nКабинет: {escape(url)}" if url else ""
    return (
        "🔑 <b>Доступ в веб-кабинет</b>\n\n"
        f"Логин: <code>{escape(login)}</code>\nПароль: <code>{escape(password)}</code>{where}"
    )


def visits_list(rows: list[ReferralVisit], total: int, counted: int, name: str) -> str:
    if not rows:
        return f"<b>Переходы: {escape(name)}</b>\n\nПо ссылке ещё никто не переходил."
    tz = get_settings().tz
    lines = [f"{v.created_at.astimezone(tz):%d.%m %H:%M} · {REASONS.get(v.reason, v.reason)}" for v in rows]
    more = f"\n…и ещё {total - len(rows)}" if total > len(rows) else ""
    return (
        f"<b>Переходы: {escape(name)}</b> — всего {total}, засчитано {counted}\n"
        "✅ засчитан · 🔁 этот браузер уже приходил · 🌐 этот адрес уже засчитан за 30 дней · "
        "🤖 превью ссылки или бот\n\n" + "\n".join(lines) + more
    )


def not_registered(user_id: int) -> str:
    return (
        "<b>KAGO REFERRER</b>\n\nЭтот бот — партнёрская программа KAGO VPN.\n"
        f"Ваш Telegram ID: <code>{user_id}</code> — отправьте его администратору, "
        "чтобы получить реферальную ссылку."
    )
