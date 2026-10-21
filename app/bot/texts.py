"""Message texts of the Telegram bot (HTML parse mode)."""

from datetime import datetime, timedelta, timezone
from html import escape

from ..config import get_settings
from ..services import RewardRules
from .service import Change, InvitedPerson, Referrer, Totals


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


def _ago(ts: datetime | None) -> str:
    if not ts:
        return "данных ещё не было"
    delta = datetime.now(timezone.utc) - ts
    if delta < timedelta(minutes=2):
        return "только что"
    if delta < timedelta(hours=2):
        return f"{int(delta.total_seconds() // 60)} мин. назад"
    return ts.astimezone(get_settings().tz).strftime("%d.%m.%Y %H:%M")


def menu(t: Totals, rules: RewardRules, data: dict) -> str:
    warn = ""
    if not data["last"] or datetime.now(timezone.utc) - data["last"] > timedelta(minutes=30):
        warn = "\n⚠️ Данные из VPN-бота давно не приходили — проверьте синхронизацию на его сервере."
    return (
        "<b>KAGO REFERRER</b> · управление реферерами\n\n"
        f"👤 Рефереров: <b>{t.referrers}</b> (активных {t.active})\n"
        f"🔗 Перешли по ссылкам: <b>{t.clicks}</b>\n"
        f"👥 Зарегистрировались в VPN: <b>{t.referrals}</b> · купили: <b>{t.buyers}</b>\n"
        f"🎁 Начислено: {days(t.accrued)} · выдано: {days(t.issued)}\n"
        f"⏳ К выдаче: <b>{days(t.due)}</b>\n\n"
        f"Правила: {escape(rules_line(rules))}\n"
        f"Данные VPN-бота: {_ago(data['last'])}{warn}"
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
        f"🔗 Перешли по ссылке: <b>{r.clicks}</b>",
        f"👥 Зарегистрировались в VPN: <b>{b.referrals}</b> × {days(b.rules.signup_days)} = {days(b.signup_reward)}",
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
    return f"Ссылка: <code>{escape(r.link)}</code>" if r.link else "Ссылка появится после запуска бота"


def referrer_card(r: Referrer) -> str:
    p = r.partner
    status = "" if p.is_active else "\n⏸ <b>Отключён</b> — ссылка не работает, статистика скрыта"
    own = []
    if p.reward_signup_days is not None or p.reward_percent is not None:
        own.append(f"Индивидуальные ставки: {escape(rules_line(r.balance.rules))}")
    return "\n".join(
        [_who(r) + status, link_line(r), "", _balance(r)] + ([""] + own if own else [])
    )


def referrer_home(r: Referrer) -> str:
    p = r.partner
    link = f"Ваша реферальная ссылка:\n<code>{escape(r.link)}</code>"
    return (
        f"<b>KAGO REFERRER</b>\n\nПривет, {escape(p.name)}!\n\n{link}\n\n"
        f"{_balance(r)}\n\n"
        f"<i>Как начисляется: {escape(rules_line(r.balance.rules))}. Засчитываются новые пользователи "
        "VPN, которые пришли по вашей ссылке. Возврат платежа отменяет начисление.</i>"
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


def invited_list(people: list[InvitedPerson], total: int, name: str) -> str:
    if not people:
        return f"<b>Приглашённые: {escape(name)}</b>\n\nПо ссылке ещё никто не переходил."
    tz = get_settings().tz
    lines = []
    for x in people:
        who = escape(x.name or "") + (f" @{escape(x.username)}" if x.username else "")
        if not x.registered_at:
            mark = "⏳ не в VPN"
        elif not x.counted:
            mark = "🚫 уже был клиентом"
        elif x.purchases:
            mark = f"💳×{x.purchases}"
        else:
            mark = "✅"
        lines.append(f"{x.clicked_at.astimezone(tz):%d.%m} · {who.strip() or '—'} · <code>{x.telegram_id}</code> · {mark}")
    more = f"\n…и ещё {total - len(people)}" if total > len(people) else ""
    return (
        f"<b>Приглашённые: {escape(name)}</b> (всего {total})\n"
        "💳 купил · ✅ зарегистрировался в VPN · ⏳ ещё нет · 🚫 уже был клиентом (не засчитан)\n\n" + "\n".join(lines) + more
    )


def invite_welcome(partner_name: str | None) -> str:
    who = f" Вас пригласил(а) <b>{escape(partner_name)}</b>." if partner_name else ""
    return (
        f"👋 Добро пожаловать в <b>KAGO VPN</b>!{who}\n\n"
        "Нажмите кнопку ниже, чтобы открыть VPN-бот и подключиться."
    )


def not_registered(user_id: int) -> str:
    return (
        "<b>KAGO REFERRER</b>\n\nЭтот бот — партнёрская программа KAGO VPN.\n"
        f"Ваш Telegram ID: <code>{user_id}</code> — отправьте его администратору, "
        "чтобы получить реферальную ссылку."
    )
