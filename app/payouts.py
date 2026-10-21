"""Payout delivery: the days can be added in the VPN service by kago-sync instead of by hand.

Modes (setting "payout_mode"):
  manual — the admin adds the days in the VPN bot and records the payout (status done);
  sync   — payouts made in the bot are queued (pending); kago-sync on the VPN-bot server claims
           them (processing), extends the referrer's subscription in Remnawave and Remnashop and
           reports back (done / failed).
With "payout_auto" on (sync mode only) every accrued day is queued automatically.

A claimed payout is never handed out again: if kago-sync dies before reporting, it stays
"processing" for the admin to check, so days are never added twice.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .db import SessionLocal
from .ingest import _authorize
from .models import (
    PAYOUT_CANCELLED,
    PAYOUT_DONE,
    PAYOUT_FAILED,
    PAYOUT_PENDING,
    PAYOUT_PROCESSING,
    ROLE_PARTNER,
    Account,
    Payout,
    utcnow,
)
from .services import default_rules, get_setting, reward_balance, set_setting

router = APIRouter()

MODE_KEY, AUTO_KEY, AGENT_KEY = "payout_mode", "payout_auto", "payout_agent_seen"
# A blocking payout (queued, in progress or failed) stops automatic payouts for that partner.
BLOCKING = (PAYOUT_PENDING, PAYOUT_PROCESSING, PAYOUT_FAILED)


class PayoutError(ValueError):
    pass


# --------------------------------------------------------------------------- settings


def mode(db: Session) -> str:
    return "sync" if get_setting(db, MODE_KEY, "manual") == "sync" else "manual"


def auto(db: Session) -> bool:
    return mode(db) == "sync" and get_setting(db, AUTO_KEY, "0") == "1"


def set_mode(db: Session, new_mode: str, auto_on: bool) -> None:
    set_setting(db, MODE_KEY, "sync" if new_mode == "sync" else "manual")
    set_setting(db, AUTO_KEY, "1" if auto_on else "0")


def agent_seen(db: Session) -> datetime | None:
    value = get_setting(db, AGENT_KEY, "")
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def agent_online(db: Session) -> bool:
    seen = agent_seen(db)
    return bool(seen and utcnow() - seen < timedelta(minutes=5))


# --------------------------------------------------------------------------- payouts


def request_payout(db: Session, partner: Account, days: int, note: str = "") -> Payout:
    """A payout from the admin: queued for kago-sync in sync mode, otherwise recorded as done.

    Negative corrections are always just records.
    """
    if days == 0 or abs(days) > 100000:
        raise PayoutError("Количество дней: целое число, не ноль")
    queued = days > 0 and mode(db) == "sync"
    if queued and not partner.telegram_id:
        raise PayoutError("У реферера не указан Telegram ID — kago-sync не найдёт его в VPN-боте")
    payout = Payout(
        partner_id=partner.id,
        days=days,
        note=note[:255],
        status=PAYOUT_PENDING if queued else PAYOUT_DONE,
        notified_at=None if queued else utcnow(),  # a manual payout is announced right away
    )
    db.add(payout)
    db.commit()
    return payout


def retry(db: Session, payout_id: int) -> Payout:
    p = db.get(Payout, payout_id)
    if not p or p.status not in (PAYOUT_FAILED, PAYOUT_PROCESSING):
        raise PayoutError("Эту выдачу нельзя повторить")
    p.status, p.error, p.notified_at = PAYOUT_PENDING, "", None
    db.commit()
    return p


def cancel(db: Session, payout_id: int) -> Payout:
    p = db.get(Payout, payout_id)
    if not p or p.status not in (PAYOUT_PENDING, PAYOUT_FAILED, PAYOUT_PROCESSING):
        raise PayoutError("Эту выдачу нельзя отменить")
    p.status = PAYOUT_CANCELLED
    db.commit()
    return p


def mark_done(db: Session, payout_id: int) -> Payout:
    """The admin checked a stuck / failed payout and added the days by hand."""
    p = db.get(Payout, payout_id)
    if not p or p.status not in (PAYOUT_PROCESSING, PAYOUT_FAILED):
        raise PayoutError("Эту выдачу нельзя отметить выполненной")
    p.status, p.notified_at = PAYOUT_DONE, utcnow()
    db.commit()
    return p


def open_payouts(db: Session, partner_id: int) -> list[Payout]:
    return list(
        db.scalars(
            select(Payout)
            .where(Payout.partner_id == partner_id, Payout.status.in_(BLOCKING))
            .order_by(Payout.created_at)
        )
    )


def queue_auto(db: Session) -> int:
    """Queue everything due for partners without an open payout. Returns how many were queued."""
    if not auto(db):
        return 0
    defaults = default_rules(db)
    blocked = set(db.scalars(select(Payout.partner_id).where(Payout.status.in_(BLOCKING))))
    partners = db.scalars(
        select(Account).where(
            Account.role == ROLE_PARTNER, Account.is_active.is_(True), Account.telegram_id.is_not(None)
        )
    )
    n = 0
    for p in partners:
        if p.id in blocked:
            continue
        due = reward_balance(db, p, defaults).due
        if due > 0:
            db.add(Payout(partner_id=p.id, days=due, note="автовыдача", status=PAYOUT_PENDING))
            n += 1
    db.commit()
    return n


def claim(db: Session, limit: int = 20) -> list[dict]:
    """Hand pending payouts to kago-sync, marking them processing in the same transaction."""
    rows = db.execute(
        select(Payout, Account.telegram_id)
        .join(Account, Account.id == Payout.partner_id)
        .where(Payout.status == PAYOUT_PENDING)
        .order_by(Payout.created_at)
        .limit(limit)
        .with_for_update(of=Payout, skip_locked=True)
    ).all()
    out = []
    for payout, telegram_id in rows:
        payout.status = PAYOUT_PROCESSING
        out.append({"id": payout.id, "telegram_id": telegram_id, "days": payout.days})
    db.commit()
    return out


def report(db: Session, payout_id: int, ok: bool, error: str = "", expire_at: datetime | None = None) -> bool:
    result = db.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == PAYOUT_PROCESSING)
        .values(
            status=PAYOUT_DONE if ok else PAYOUT_FAILED,
            applied_at=utcnow() if ok else None,
            expire_at=expire_at,
            error="" if ok else (error or "неизвестная ошибка")[:2000],
            notified_at=None,
        )
    )
    db.commit()
    return result.rowcount == 1


def unannounced(db: Session) -> list[tuple[Payout, Account]]:
    """Payouts kago-sync applied or failed that the bot has not announced yet."""
    return list(
        db.execute(
            select(Payout, Account)
            .join(Account, Account.id == Payout.partner_id)
            .where(
                Payout.notified_at.is_(None),
                or_(
                    Payout.status == PAYOUT_FAILED,
                    and_(Payout.status == PAYOUT_DONE, Payout.applied_at.is_not(None)),
                ),
            )
            .order_by(Payout.id)
            .limit(50)
        ).all()
    )


def mark_announced(db: Session, payout_ids: list[int]) -> None:
    if payout_ids:
        db.execute(update(Payout).where(Payout.id.in_(payout_ids)).values(notified_at=utcnow()))
        db.commit()


# --------------------------------------------------------------------------- API for kago-sync


class ClaimIn(BaseModel):
    limit: int = Field(20, ge=1, le=100)


class ResultIn(BaseModel):
    ok: bool
    error: str = ""
    expire_at: datetime | None = None


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "Нет или неверный токен (Authorization: Bearer <токен>)"}, status_code=401)


@router.post("/api/payouts/claim")
def api_claim(request: Request, body: ClaimIn | None = None):
    with SessionLocal() as db:
        if _authorize(db, request) is None:
            return _unauthorized()
        set_setting(db, AGENT_KEY, utcnow().isoformat())
        db.commit()
        if mode(db) != "sync":
            return {"enabled": False, "payouts": []}
        queue_auto(db)
        return {"enabled": True, "payouts": claim(db, (body or ClaimIn()).limit)}


@router.post("/api/payouts/{payout_id}/result")
def api_result(payout_id: int, body: ResultIn, request: Request):
    with SessionLocal() as db:
        if _authorize(db, request) is None:
            return _unauthorized()
        if not report(db, payout_id, body.ok, body.error, body.expire_at):
            return JSONResponse({"error": "Выдача не найдена или уже обработана"}, status_code=409)
        return {"ok": True}

