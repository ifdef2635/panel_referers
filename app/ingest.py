"""POST /api/ingest — events pushed by the bot (see docs/events_api.md)."""

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import analytics
from .db import SessionLocal
from .models import IngestToken, utcnow

router = APIRouter()

MAX_EVENTS = 500
MAX_BYTES = 1024 * 1024
FUTURE_TOLERANCE = timedelta(hours=24)
TOKEN_PREFIX = "kago_"

SUB_STATUSES = {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED", "DELETED"}
PURCHASE_TYPES = {"NEW", "RENEW", "CHANGE"}


class EventError(ValueError):
    pass


# --------------------------------------------------------------------------- tokens


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session) -> str:
    """Create a new token and revoke all previous ones. The plain token is returned once."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.execute(
        update(IngestToken).where(IngestToken.revoked_at.is_(None)).values(revoked_at=utcnow())
    )
    db.add(IngestToken(token_hash=hash_token(token), hint=token[:12] + "…"))
    db.commit()
    return token


def active_token(db: Session) -> IngestToken | None:
    return db.scalar(
        select(IngestToken).where(IngestToken.revoked_at.is_(None)).order_by(IngestToken.id.desc())
    )


def _authorize(db: Session, request: Request) -> IngestToken | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    row = db.scalar(
        select(IngestToken).where(
            IngestToken.token_hash == hash_token(token.strip()), IngestToken.revoked_at.is_(None)
        )
    )
    return row


# --------------------------------------------------------------------------- validation helpers


def _str(data: dict, key: str, *, required: bool = False, max_len: int = 128) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise EventError(f"data.{key}: обязательное поле")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise EventError(f"data.{key}: ожидается строка")
    value = str(value).strip()
    if len(value) > max_len:
        raise EventError(f"data.{key}: длиннее {max_len} символов")
    return value


def _user_id(data: dict, key: str = "user_id", *, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise EventError(f"data.{key}: обязательное поле")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise EventError(f"data.{key}: id пользователя — число или строка")
    value = str(value).strip()
    if not value or len(value) > 64:
        raise EventError(f"data.{key}: id пользователя — до 64 символов")
    return value


def _bool(data: dict, key: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EventError(f"data.{key}: ожидается true/false")
    return value


def parse_ts(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EventError(f"{field}: ожидается ISO 8601 с часовым поясом")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EventError(f"{field}: ожидается ISO 8601 с часовым поясом") from None
    if dt.tzinfo is None:
        raise EventError(f"{field}: укажите часовой пояс (например +00:00)")
    return dt.astimezone(timezone.utc)


COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")


def _validate(event: Any) -> tuple[str, str, datetime, dict]:
    if not isinstance(event, dict):
        raise EventError("событие должно быть объектом")
    eid = event.get("id")
    if not isinstance(eid, str) or not eid.strip() or len(eid) > 128:
        raise EventError("id: строка от 1 до 128 символов")
    etype = event.get("type")
    if not isinstance(etype, str) or not etype or len(etype) > 64:
        raise EventError("type: обязательная строка")
    ts = parse_ts(event.get("ts"), "ts")
    if ts > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
        raise EventError("ts: время события из будущего")
    data = event.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise EventError("data: ожидается объект")
    return eid, etype, ts, data


# --------------------------------------------------------------------------- handlers
# Each handler validates `data` and applies it to the projections. They are written so that
# the delivery order of events doesn't matter.


def _upsert_user(db: Session, ts: datetime, data: dict, *, registered: bool) -> None:
    user_id = _user_id(data)
    country = _str(data, "country", max_len=8)
    if country is not None:
        if not COUNTRY_RE.match(country):
            raise EventError("data.country: country — двухбуквенный код (RU)")
        country = country.upper()
    language = _str(data, "language", max_len=16)
    referrer_id = _user_id(data, "referrer_id", required=False)
    if referrer_id == user_id:
        referrer_id = None  # self-referral
    telegram_id = data.get("telegram_id")
    if telegram_id is not None:
        try:
            telegram_id = int(telegram_id)
        except (TypeError, ValueError):
            raise EventError("data.telegram_id: ожидается число") from None
    username = _str(data, "username", max_len=64)
    params = {
        "user_id": user_id,
        "registered_at": ts if registered else None,
        "referrer_id": referrer_id,
        "source": _str(data, "source"),
        "country": country,
        "language": language.lower() if language else None,
        "telegram_id": telegram_id,
        "username": username.lstrip("@") if username else None,
        "name": _str(data, "name", max_len=255),
    }
    # The earliest registration date wins; the first known referrer / source wins
    # (attribution never moves); profile fields take the latest non-empty value.
    db.connection().exec_driver_sql(
        """
        INSERT INTO bot_users (user_id, registered_at, referrer_id, source, country, language,
                               telegram_id, username, name, updated_at)
        VALUES (%(user_id)s, %(registered_at)s, %(referrer_id)s, %(source)s, %(country)s,
                %(language)s, %(telegram_id)s, %(username)s, %(name)s, now())
        ON CONFLICT (user_id) DO UPDATE SET
          registered_at = LEAST(bot_users.registered_at, EXCLUDED.registered_at),  -- NULLs ignored
          referrer_id = COALESCE(bot_users.referrer_id, EXCLUDED.referrer_id),
          source = COALESCE(bot_users.source, EXCLUDED.source),
          country = COALESCE(EXCLUDED.country, bot_users.country),
          language = COALESCE(EXCLUDED.language, bot_users.language),
          telegram_id = COALESCE(EXCLUDED.telegram_id, bot_users.telegram_id),
          username = COALESCE(EXCLUDED.username, bot_users.username),
          name = COALESCE(EXCLUDED.name, bot_users.name),
          updated_at = now()
        """,
        params,
    )


def h_user_registered(db: Session, ts: datetime, data: dict) -> None:
    _upsert_user(db, ts, data, registered=True)


def h_user_updated(db: Session, ts: datetime, data: dict) -> None:
    _upsert_user(db, ts, data, registered=False)


def h_referral_attached(db: Session, ts: datetime, data: dict) -> None:
    user_id = _user_id(data)
    referrer_id = _user_id(data, "referrer_id")
    if user_id == referrer_id:
        return
    db.connection().exec_driver_sql(
        """
        INSERT INTO bot_users (user_id, referrer_id, updated_at) VALUES (%(u)s, %(r)s, now())
        ON CONFLICT (user_id) DO UPDATE
          SET referrer_id = COALESCE(bot_users.referrer_id, EXCLUDED.referrer_id), updated_at = now()
        """,
        {"u": user_id, "r": referrer_id},
    )


def h_payment_completed(db: Session, ts: datetime, data: dict) -> None:
    payment_id = _str(data, "payment_id", required=True)
    user_id = _user_id(data)
    raw = data.get("amount")
    if raw is None or isinstance(raw, bool):
        raise EventError("data.amount: обязательное число ≥ 0")
    try:
        amount = Decimal(str(raw))
    except InvalidOperation:
        raise EventError("data.amount: обязательное число ≥ 0") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1e12"):
        raise EventError("data.amount: обязательное число ≥ 0")
    currency = _str(data, "currency", required=True, max_len=8).upper()
    purchase_type = _str(data, "purchase_type", max_len=16)
    if purchase_type is not None:
        purchase_type = purchase_type.upper()
        if purchase_type not in PURCHASE_TYPES:
            raise EventError("data.purchase_type: NEW / RENEW / CHANGE")
    db.connection().exec_driver_sql(
        """
        INSERT INTO payments (payment_id, user_id, paid_at, amount, currency, purchase_type,
                              plan, gateway, is_test)
        VALUES (%(payment_id)s, %(user_id)s, %(ts)s, %(amount)s, %(currency)s, %(purchase_type)s,
                %(plan)s, %(gateway)s, %(is_test)s)
        ON CONFLICT (payment_id) DO UPDATE SET
          user_id = EXCLUDED.user_id, paid_at = EXCLUDED.paid_at, amount = EXCLUDED.amount,
          currency = EXCLUDED.currency, purchase_type = EXCLUDED.purchase_type,
          plan = EXCLUDED.plan, gateway = EXCLUDED.gateway, is_test = EXCLUDED.is_test
        """,
        {
            "payment_id": payment_id,
            "user_id": user_id,
            "ts": ts,
            "amount": amount,
            "currency": currency,
            "purchase_type": purchase_type,
            "plan": _str(data, "plan"),
            "gateway": _str(data, "gateway", max_len=64),
            "is_test": bool(_bool(data, "is_test")),
        },
    )


def h_payment_refunded(db: Session, ts: datetime, data: dict) -> None:
    payment_id = _str(data, "payment_id", required=True)
    db.connection().exec_driver_sql(
        """
        INSERT INTO payments (payment_id, refunded_at, is_test) VALUES (%(p)s, %(ts)s, false)
        ON CONFLICT (payment_id) DO UPDATE
          SET refunded_at = COALESCE(payments.refunded_at, EXCLUDED.refunded_at)
        """,
        {"p": payment_id, "ts": ts},
    )


def h_subscription_updated(db: Session, ts: datetime, data: dict) -> None:
    user_id = _user_id(data)
    status = _str(data, "status", required=True, max_len=16).upper()
    if status not in SUB_STATUSES:
        raise EventError("data.status: ACTIVE, DISABLED, LIMITED, EXPIRED или DELETED")
    expire_at = parse_ts(data.get("expire_at"), "data.expire_at")
    is_trial = bool(_bool(data, "is_trial"))
    created_at = parse_ts(data["created_at"], "data.created_at") if data.get("created_at") else ts
    db.connection().exec_driver_sql(
        """
        INSERT INTO subscriptions (user_id, status, expire_at, is_trial, created_at, state_ts)
        VALUES (%(u)s, %(status)s, %(expire_at)s, %(is_trial)s, %(created_at)s, %(ts)s)
        ON CONFLICT (user_id) DO UPDATE SET
          status = EXCLUDED.status, expire_at = EXCLUDED.expire_at, is_trial = EXCLUDED.is_trial,
          created_at = EXCLUDED.created_at, state_ts = EXCLUDED.state_ts
        WHERE subscriptions.state_ts <= EXCLUDED.state_ts
        """,
        {
            "u": user_id,
            "status": status,
            "expire_at": expire_at,
            "is_trial": is_trial,
            "created_at": created_at,
            "ts": ts,
        },
    )
    if is_trial:
        # Remember that the user took a trial, even after the state is overwritten by a purchase.
        db.connection().exec_driver_sql(
            """
            INSERT INTO bot_users (user_id, trial_started_at, updated_at) VALUES (%(u)s, %(t)s, now())
            ON CONFLICT (user_id) DO UPDATE
              SET trial_started_at = LEAST(bot_users.trial_started_at, EXCLUDED.trial_started_at)
            """,
            {"u": user_id, "t": created_at},
        )


def h_device_added(db: Session, ts: datetime, data: dict) -> None:
    user_id = _user_id(data)
    platform = _str(data, "platform", required=True, max_len=32)
    device_id = _str(data, "device_id") or ""
    db.connection().exec_driver_sql(
        """
        INSERT INTO devices (user_id, platform, device_key, first_seen)
        VALUES (%(u)s, %(p)s, %(d)s, %(ts)s)
        ON CONFLICT (user_id, platform, device_key)
          DO UPDATE SET first_seen = LEAST(devices.first_seen, EXCLUDED.first_seen)
        """,
        {"u": user_id, "p": platform, "d": device_id, "ts": ts},
    )


HANDLERS = {
    "user.registered": h_user_registered,
    "user.updated": h_user_updated,
    "referral.attached": h_referral_attached,
    "payment.completed": h_payment_completed,
    "payment.refunded": h_payment_refunded,
    "subscription.updated": h_subscription_updated,
    "device.added": h_device_added,
}


# --------------------------------------------------------------------------- processing


def process(db: Session, events: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"accepted": 0, "duplicates": 0, "ignored": 0, "errors": []}
    for index, event in enumerate(events):
        eid = event.get("id") if isinstance(event, dict) else None
        try:
            eid, etype, ts, data = _validate(event)
            handler = HANDLERS.get(etype)
            with db.begin_nested():
                inserted = db.connection().exec_driver_sql(
                    """
                    INSERT INTO events (id, type, ts, data, received_at, is_ignored)
                    VALUES (%(id)s, %(type)s, %(ts)s, %(data)s, now(), %(ignored)s)
                    ON CONFLICT (id) DO NOTHING RETURNING id
                    """,
                    {
                        "id": eid,
                        "type": etype,
                        "ts": ts,
                        "data": json.dumps(data, ensure_ascii=False, default=str),
                        "ignored": handler is None,
                    },
                ).first()
                if inserted is None:
                    result["duplicates"] += 1
                    continue
                if handler is None:
                    result["ignored"] += 1
                    continue
                handler(db, ts, data)
            result["accepted"] += 1
        except EventError as exc:
            result["errors"].append(
                {"index": index, "id": eid if isinstance(eid, str) else None,
                 "status": "error", "error": str(exc)}
            )
    return result


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.post("/api/ingest")
async def ingest(request: Request):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BYTES:
        return _error(413, "Тело запроса больше 1 МБ")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BYTES:
            return _error(413, "Тело запроса больше 1 МБ")

    return await run_in_threadpool(_handle, request, body)


def _handle(request: Request, body: bytes):
    with SessionLocal() as db:
        token = _authorize(db, request)
        if token is None:
            return _error(401, "Нет или неверный токен (Authorization: Bearer <токен>)")
        if not body.strip():
            return _error(400, "Пустой запрос")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return _error(400, "Тело запроса — не JSON")

        if isinstance(payload, dict) and "events" in payload:
            events = payload["events"]
        elif isinstance(payload, dict):
            events = [payload]
        else:
            events = payload
        if not isinstance(events, list) or not events:
            return _error(400, "Нет событий")
        if len(events) > MAX_EVENTS:
            return _error(400, f"Больше {MAX_EVENTS} событий в запросе")

        result = process(db, events)
        token.last_used_at = utcnow()
        db.commit()
    if result["accepted"]:
        analytics.clear_cache()
    return result
