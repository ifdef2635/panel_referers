"""Partner referral links on the panel domain: <PUBLIC_URL>/r/<code> → REFERRAL_REDIRECT_URL.

Every open is logged; a referral is counted once per person, as far as a link can tell:
- link previews and crawlers (Telegram, WhatsApp, curl…) never count;
- a browser that already came through any partner link doesn't count again (cookie, the first
  partner wins);
- an address already counted in the last 30 days doesn't count again (stored only as a salted
  hash; IPv6 by /64, which one home connection usually gets).
"""

import hashlib
import ipaddress
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analytics
from .config import get_settings
from .db import SessionLocal
from .models import ROLE_PARTNER, Account, ReferralVisit, utcnow

router = APIRouter()

COOKIE = "kago_ref"
CODE_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
IP_WINDOW = timedelta(days=30)
# Link previews and crawlers. Not "telegram" alone: Telegram's in-app browser on Android sends
# "Telegram-Android", and those are real people; "bot" only as Googlebot/, Slackbot-… (not Cubot).
BOT_UA = re.compile(
    r"bot[/\-]|telegrambot|twitterbot|crawl|spider|preview|whatsapp/|facebookexternalhit|vkshare"
    r"|curl/|wget/|python-|httpx/|go-http|java/|okhttp|headless",
    re.I,
)


def new_code() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:8] or secrets.token_hex(4)


def referral_link(db: Session, partner: Account) -> str:
    if not partner.invite_code:
        partner.invite_code = new_code()
        db.commit()
    return f"{get_settings().public_url.rstrip('/')}/r/{partner.invite_code}"


def _ip_hash(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            ip = str(ipaddress.ip_network(f"{addr}/64", strict=False).network_address)
    except ValueError:
        pass
    return hashlib.sha256(f"{get_settings().secret_key}|{ip}".encode()).hexdigest()


def record_visit(
    db: Session, code: str, ip: str, user_agent: str, referer: str, visitor_id: str | None
) -> tuple[ReferralVisit | None, str]:
    """Log an open of a partner link. Returns the visit (None for an unknown code) and the
    visitor id to keep in the cookie."""
    visitor_id = visitor_id if visitor_id and re.fullmatch(r"[A-Za-z0-9_-]{8,32}", visitor_id) else None
    p = db.scalar(select(Account).where(Account.invite_code == code, Account.role == ROLE_PARTNER))
    if not p:
        return None, visitor_id or secrets.token_urlsafe(12)
    ip_hash = _ip_hash(ip)
    if BOT_UA.search(user_agent or "") or not user_agent:
        reason = "bot"
    elif not p.is_active:
        reason = "off"
    elif visitor_id and db.scalar(
        select(ReferralVisit.id).where(ReferralVisit.visitor_id == visitor_id, ReferralVisit.counted).limit(1)
    ):
        reason = "repeat"
    elif db.scalar(
        select(ReferralVisit.id)
        .where(
            ReferralVisit.ip_hash == ip_hash,
            ReferralVisit.counted,
            ReferralVisit.created_at > utcnow() - IP_WINDOW,
        )
        .limit(1)
    ):
        reason = "ip"
    else:
        reason = ""
    visit = ReferralVisit(
        partner_id=p.id,
        visitor_id=visitor_id or secrets.token_urlsafe(12),
        ip_hash=ip_hash,
        user_agent=(user_agent or "")[:255],
        referer=(referer or "")[:255],
        counted=not reason,
        reason=reason,
    )
    db.add(visit)
    db.commit()
    if visit.counted:
        analytics.clear_cache()
    return visit, visit.visitor_id


def counted_referrals(db: Session, partner_id: int, start: datetime | None = None, end: datetime | None = None) -> int:
    q = select(func.count()).where(ReferralVisit.partner_id == partner_id, ReferralVisit.counted)
    if start is not None:
        q = q.where(ReferralVisit.created_at >= start)
    if end is not None:
        q = q.where(ReferralVisit.created_at < end)
    return int(db.scalar(q) or 0)


def visit_counts(db: Session, partner_id: int) -> dict[str, int]:
    total, counted = db.execute(
        select(func.count(), func.count().filter(ReferralVisit.counted)).where(
            ReferralVisit.partner_id == partner_id
        )
    ).one()
    return {"total": int(total), "counted": int(counted)}


@router.get("/r/{code}")
def open_link(code: str, request: Request):
    target = get_settings().referral_redirect_url
    response = RedirectResponse(target, 302)
    if not CODE_RE.match(code):
        return response
    with SessionLocal() as db:
        _, visitor_id = record_visit(
            db,
            code,
            request.client.host if request.client else "",
            request.headers.get("user-agent", ""),
            request.headers.get("referer", ""),
            request.cookies.get(COOKIE),
        )
    response.set_cookie(
        COOKIE, visitor_id, max_age=3 * 365 * 24 * 3600, httponly=True, samesite="lax",
        secure=get_settings().cookie_secure,
    )
    return response
