"""Message texts of the Telegram bot (HTML parse mode)."""

from html import escape

from ..config import get_settings
from ..services import RewardRules
from .service import Change, Referrer, Totals


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
    what = "каждой купленной подписки" if r.renewals else "первой купленной подписки"
    return f"{days(r.signup_days)} за каждого приглашённого + {r.percent}% от срока {what}"


def panel_url() -> str:
    url = get_settings().public_url.rstrip("/")
    if not url.startswith("https://") or "127.0.0.1" in url or "localhost" in url:
        return ""
    return url


def menu(t: Totals, rules: RewardRules) -> str:
    return (
        "<b>KAGO REFERRER</b> · управление реферерами\n\n"
        f"👤 Рефереров: <b>{t.referrers}</b> (активных {t.active})\n"
        f"👥 Приглашено: <b>{t.referrals}</b> · купили: <b>{t.buyers}</b>\n"
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
        f"👥 Приглашено: <b>{b.referrals}</b> × {days(b.rules.signup_days)} = {days(b.signup_reward)}",
        f"💳 Покупок: <b>{b.purchases}</b> (купили {b.buyers}) · {b.rules.percent}% от {days(b.plan_days)}"
        f" = {days(b.purchase_reward)}",
    ]
    if b.purchases_no_duration:
        lines.append(f"⚠️ Покупок без известного срока тарифа: {b.purchases_no_duration} (не начисляются)")
    lines.append(
        f"\n🎁 Начислено: <b>{days(b.accrued)}</b> · выдано: {days(b.issued)}\n"
        f"⏳ К выдаче: <b>{days(b.due)}</b>"
    )
    return "\n".join(lines)


def link_line(r: Referrer) -> str:
    link = r.partner.referral_link
    return f"🔗 <code>{escape(link)}</code>" if link else "🔗 ссылка не задана"


def referrer_card(r: Referrer) -> str:
    p = r.partner
    status = "" if p.is_active else "\n⏸ <b>Отключён</b> — не видит статистику и не получает уведомления"
    own = []
    if p.reward_signup_days is not None or p.reward_percent is not None:
        own.append(f"Индивидуальные ставки: {escape(rules_line(r.balance.rules))}")
    if not r.bot_user_id:
        own.append("⚠️ Не привязан пользователь VPN-бота — приглашённые не считаются")
    return "\n".join(
        [_who(r) + status, link_line(r), "", _balance(r)] + ([""] + own if own else [])
    )


def referrer_home(r: Referrer) -> str:
    p = r.partner
    link = (
        f"Ваша реферальная ссылка:\n<code>{escape(p.referral_link)}</code>"
        if p.referral_link
        else "Реферальная ссылка ещё не выдана — администратор пришлёт её сюда."
    )
    return (
        f"<b>KAGO REFERRER</b>\n\nПривет, {escape(p.name)}!\n\n{link}\n\n"
        f"{_balance(r)}\n\n"
        f"<i>Как начисляется: {escape(rules_line(r.balance.rules))}. Возврат платежа отменяет начисление.</i>"
    )


def link_message(r: Referrer) -> str:
    return (
        "🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"<code>{escape(r.partner.referral_link)}</code>\n\n"
        f"Начисляем: {escape(rules_line(r.balance.rules))}."
    )


def payout_message(days_n: int, r: Referrer) -> str:
    if days_n > 0:
        head = f"🎁 Вам выдано <b>{days(days_n)}</b> подписки. Спасибо, что приглашаете друзей!"
    else:
        head = f"Корректировка начислений: {days(days_n)}."
    return f"{head}\n\nОсталось к выдаче: {days(max(r.balance.due, 0))}"


def change_message(c: Change) -> str:
    parts = []
    if c.new_referrals:
        parts.append(f"👥 Новых приглашённых: <b>+{c.new_referrals}</b>")
    if c.new_purchases:
        parts.append(f"💳 Новых покупок: <b>+{c.new_purchases}</b>")
    gained = f"\n🎁 Начислено: <b>+{days(c.new_days)}</b>" if c.new_days > 0 else ""
    return (
        "\n".join(parts)
        + gained
        + f"\n⏳ Всего к выдаче: <b>{days(max(c.referrer.balance.due, 0))}</b>"
    )


def access_message(login: str, password: str) -> str:
    url = panel_url()
    where = f"\nКабинет: {escape(url)}" if url else ""
    return (
        "🔑 <b>Доступ в веб-кабинет</b>\n\n"
        f"Логин: <code>{escape(login)}</code>\nПароль: <code>{escape(password)}</code>{where}"
    )


def not_registered(user_id: int) -> str:
    return (
        "<b>KAGO REFERRER</b>\n\nЭтот бот — для партнёров KAGO VPN.\n"
        f"Ваш Telegram ID: <code>{user_id}</code> — отправьте его администратору, "
        "чтобы получить реферальную ссылку."
    )
