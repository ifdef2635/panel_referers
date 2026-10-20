"""Kago Analytics event emitter for the bot.

Copy this file into the bot, set KAGO_URL / KAGO_TOKEN in its .env, start the sender once
(`asyncio.create_task(kago_emitter.sender())` on startup) and call `emit(...)` / the helpers
from the business logic. Calls never block and never raise: analytics must not break the bot.

Events are kept in memory, so the ones still queued are lost on restart. Run
remnashop_sync.py (backfill) from time to time, or with --watch, to fill such gaps;
event ids are deterministic, so re-sending creates no duplicates.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("kago")

KAGO_URL = os.getenv("KAGO_URL", "")  # https://<panel>/api/ingest
KAGO_TOKEN = os.getenv("KAGO_TOKEN", "")
# While the panel has no domain it uses a self-signed certificate. Pin it (recommended):
#   KAGO_CA_FILE=/path/to/kago.crt   (copy of /etc/kago/tls/selfsigned.crt from the panel server)
# or, as a last resort, switch verification off with KAGO_INSECURE=1.
KAGO_CA_FILE = os.getenv("KAGO_CA_FILE", "")
KAGO_INSECURE = os.getenv("KAGO_INSECURE", "0") == "1"


def tls_verify() -> bool | str:
    if KAGO_CA_FILE:
        return KAGO_CA_FILE
    return not KAGO_INSECURE

_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)


def _iso(ts: datetime | None) -> str:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def emit(type_: str, event_id: str, data: dict[str, Any], ts: datetime | None = None) -> None:
    """Queue an event. Safe to call from anywhere in the bot."""
    if not KAGO_URL:
        return
    try:
        _queue.put_nowait({"id": event_id, "type": type_, "ts": _iso(ts), "data": data})
    except asyncio.QueueFull:
        log.warning("Kago queue is full, event %s dropped", event_id)


async def sender() -> None:
    """Background task: sends queued events in batches with retries."""
    if not KAGO_URL:
        log.info("KAGO_URL is not set, analytics disabled")
        return
    headers = {"Authorization": f"Bearer {KAGO_TOKEN}"}
    async with httpx.AsyncClient(verify=tls_verify(), timeout=10, headers=headers) as client:
        while True:
            batch = [await _queue.get()]
            await asyncio.sleep(1)  # collect a batch
            while not _queue.empty() and len(batch) < 500:
                batch.append(_queue.get_nowait())
            for attempt in range(8):
                try:
                    r = await client.post(KAGO_URL, json={"events": batch})
                    if r.status_code == 200:
                        errors = r.json().get("errors") or []
                        for e in errors:
                            log.warning("Kago rejected event %s: %s", e.get("id"), e.get("error"))
                        break
                    if r.status_code < 500 and r.status_code != 429:
                        log.error("Kago ingest %s: %s", r.status_code, r.text[:300])
                        break  # 4xx: retrying won't help
                except httpx.HTTPError as exc:
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                        log.error("Kago: panel certificate rejected, set KAGO_CA_FILE (see kago_emitter.py)")
                        break
                    log.warning("Kago ingest failed: %s", exc)
                await asyncio.sleep(min(2**attempt, 60))


# --------------------------------------------------------------------------- Remnashop helpers
# Take Remnashop DTO/ORM objects; attribute names follow src/infrastructure/database/models.


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def user_registered(user: Any, referrer_id: int | None = None, ad_link_name: str | None = None) -> None:
    emit(
        "user.registered",
        f"reg:{user.id}",
        {
            "user_id": user.id,
            "telegram_id": getattr(user, "telegram_id", None),
            "username": getattr(user, "username", None),
            "name": getattr(user, "name", None),
            "language": str(_val(getattr(user, "language", "")) or "").lower() or None,
            "referrer_id": referrer_id,
            "source": ad_link_name,
        },
        getattr(user, "created_at", None),
    )


def referral_attached(referred_id: int, referrer_id: int) -> None:
    emit("referral.attached", f"ref:{referred_id}", {"user_id": referred_id, "referrer_id": referrer_id})


def payment_completed(tx: Any) -> None:
    pricing = tx.pricing if isinstance(tx.pricing, dict) else vars(tx.pricing)
    plan = tx.plan_snapshot if isinstance(tx.plan_snapshot, dict) else vars(tx.plan_snapshot)
    emit(
        "payment.completed",
        f"payment:{tx.payment_id}",
        {
            "payment_id": str(tx.payment_id),
            "user_id": tx.user_id,
            "amount": float(pricing.get("final_amount") or 0),
            "currency": str(_val(tx.currency)),
            "purchase_type": str(_val(tx.purchase_type)),
            "plan": plan.get("name"),
            "gateway": str(_val(tx.gateway_type)),
            "is_test": bool(tx.is_test),
        },
        getattr(tx, "updated_at", None),
    )


def payment_refunded(payment_id: Any) -> None:
    emit("payment.refunded", f"refund:{payment_id}", {"payment_id": str(payment_id)})


def subscription_updated(sub: Any) -> None:
    updated = getattr(sub, "updated_at", None) or datetime.now(timezone.utc)
    emit(
        "subscription.updated",
        f"sub:{sub.user_id}:{updated.timestamp()}",
        {
            "user_id": sub.user_id,
            "status": str(_val(sub.status)),
            "expire_at": _iso(sub.expire_at),
            "is_trial": bool(sub.is_trial),
            "created_at": _iso(getattr(sub, "created_at", None)),
        },
        updated,
    )
