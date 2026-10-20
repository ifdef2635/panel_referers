"""Shared web plumbing: templates, auth dependencies, CSRF, flash messages."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from .config import get_settings
from .db import get_db
from .models import Account
from .security import new_csrf_token
from .services import PRESETS

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "XTR": "★"}
SUB_STATUS = {
    "ACTIVE": "Активна",
    "DISABLED": "Отключена",
    "LIMITED": "Лимит",
    "EXPIRED": "Истекла",
    "DELETED": "Удалена",
}
VIA = {
    "ref1": "Реф. ссылка",
    "ref2": "Реф. 2 уровня",
    "ad_link": "Рекл. ссылка",
    "referral": "Реферал",
    "organic": "Органика",
}
GATEWAYS = {
    "TELEGRAM_STARS": "Telegram Stars",
    "YOOKASSA": "ЮKassa",
    "YOOMONEY": "ЮMoney",
    "CRYPTOPAY": "CryptoPay",
}
PURCHASE_TYPES = {"NEW": "Новая", "RENEW": "Продление", "CHANGE": "Смена тарифа"}


class LoginRequired(Exception):
    pass


# --------------------------------------------------------------------------- filters


def fmt_num(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, (Decimal, float)):
        v = float(value)
        s = f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    else:
        s = f"{int(value):,}"
    return s.replace(",", " ")


def fmt_money(amount: Any, currency: str) -> str:
    return f"{fmt_num(amount)} {CURRENCY_SYMBOLS.get(currency, currency)}"


def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not value:
        return "—"
    return value.astimezone(get_settings().tz).strftime(fmt)


def fmt_isodt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return fmt_dt(datetime.fromisoformat(value))
    except ValueError:
        return value


def fmt_date(value: datetime | None) -> str:
    return fmt_dt(value, "%d.%m.%Y")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def days_word(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'день', 'дня', 'дней')}"


def mask_name(name: str | None) -> str:
    if not name:
        return "—"
    name = name.strip()
    return (name[0] + "•••") if name else "—"


def who(row: dict, reveal: bool) -> dict:
    """Display fields for a bot user, hiding contacts from partners if required."""
    if reveal:
        return {
            "name": row.get("name") or "—",
            "username": row.get("username"),
            "tg": row.get("telegram_id"),
        }
    return {"name": mask_name(row.get("name")), "username": None, "tg": None}


templates.env.filters.update(
    num=fmt_num, money=fmt_money, dt=fmt_dt, isodt=fmt_isodt, date=fmt_date, days=days_word
)
templates.env.globals.update(
    SUB_STATUS=SUB_STATUS,
    VIA=VIA,
    GATEWAYS=GATEWAYS,
    PURCHASE_TYPES=PURCHASE_TYPES,
    PRESETS=PRESETS,
    CURRENCY_SYMBOLS=CURRENCY_SYMBOLS,
    who=who,
    plural=plural,
)


# --------------------------------------------------------------------------- session helpers


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_csrf_token()
        request.session["csrf"] = token
    return token


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])
    request.session["flash"] = request.session["flash"]  # mark modified


def pop_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("flash", [])


def render(request: Request, template: str, account: Account | None = None, **ctx):
    ctx.update(
        request=request,
        me=account,
        csrf=csrf_token(request),
        flashes=pop_flashes(request),
        app_name=get_settings().app_name,
        query=dict(request.query_params),
    )
    return templates.TemplateResponse(request, template, ctx)


def verify_csrf(request: Request, csrf: str = Form("")) -> None:
    expected = request.session.get("csrf")
    if not expected or not csrf or csrf != expected:
        raise HTTPException(400, "Сессия устарела, обновите страницу и повторите.")


# --------------------------------------------------------------------------- auth


def current_account(request: Request, db: Session = Depends(get_db)) -> Account:
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired()
    account = db.get(Account, uid, options=[selectinload(Account.sources)])
    if (
        not account
        or not account.is_active
        or request.session.get("sv") != account.session_version
    ):
        request.session.clear()
        raise LoginRequired()
    return account


def require_admin(account: Account = Depends(current_account)) -> Account:
    if not account.is_admin:
        raise HTTPException(403, "Недостаточно прав")
    return account


def require_partner(account: Account = Depends(current_account)) -> Account:
    if account.is_admin:
        raise HTTPException(403, "Этот раздел для партнёров")
    return account
