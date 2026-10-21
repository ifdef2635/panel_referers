from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import analytics as rs
from ..db import get_db
from ..models import Account
from ..services import chart_payload, earned_in_period, partner_scope, resolve_period, reward_balance
from ..web import render, require_partner

router = APIRouter(prefix="/p")

PER_PAGE = 50


def partner_dashboard_context(db: Session, partner: Account, request: Request) -> dict:
    q = request.query_params
    period = resolve_period(q.get("period"), q.get("from"), q.get("to"))
    scope = partner_scope(partner)
    return {
        "partner": partner,
        "period": period,
        "stats": rs.overview(scope, period.start, period.end),
        "chart": chart_payload(scope, period) if not scope.empty else None,
        "balance": reward_balance(db, partner),
        "earned": earned_in_period(db, partner, period.start, period.end),
        "recent": rs.recent_payments(scope, 15),
        "geo": rs.geography(scope, period.start, period.end),
        "platforms": rs.platforms(scope),
        "no_sources": scope.empty,
    }


def partner_users_context(partner: Account, request: Request) -> dict:
    q = request.query_params
    try:
        page = max(1, int(q.get("page", 1)))
    except ValueError:
        page = 1
    flt = q.get("filter", "all")
    flt = flt if flt in rs.USER_FILTERS else "all"
    search = q.get("q", "").strip()[:64]
    rows, total = rs.users_page(
        partner_scope(partner), page=page, per_page=PER_PAGE, search=search, flt=flt
    )
    return {
        "partner": partner,
        "rows": rows,
        "total": total,
        "page": page,
        "pages": max(1, -(-total // PER_PAGE)),
        "flt": flt,
        "search": search,
    }


@router.get("")
def dashboard(
    request: Request, me: Account = Depends(require_partner), db: Session = Depends(get_db)
):
    return render(request, "partner/dashboard.html", me, **partner_dashboard_context(db, me, request))


@router.get("/users")
def users(request: Request, me: Account = Depends(require_partner)):
    return render(request, "partner/users.html", me, **partner_users_context(me, request))


@router.get("/rewards")
def rewards(request: Request, me: Account = Depends(require_partner), db: Session = Depends(get_db)):
    return render(
        request, "partner/rewards.html", me, partner=me, balance=reward_balance(db, me)
    )
