import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import analytics as rs
from .. import botdb
from ..config import get_settings
from ..db import get_db
from ..ingest import active_token, issue_token
from ..models import (
    ROLE_PARTNER,
    SOURCE_AD,
    SOURCE_REFERRER,
    Account,
    PartnerSource,
    Payout,
)
from ..security import hash_password
from ..services import (
    chart_payload,
    default_reward_days,
    partner_scope,
    partners,
    resolve_period,
    reward_balance,
    set_setting,
)
from ..web import flash, render, require_admin, verify_csrf
from .partner import partner_dashboard_context, partner_users_context

router = APIRouter(prefix="/admin")

LOGIN_RE = re.compile(r"^[a-z0-9_.-]{3,64}$")
PER_PAGE = 50


def _partner(db: Session, partner_id: int) -> Account:
    p = db.get(
        Account,
        partner_id,
        options=[selectinload(Account.sources), selectinload(Account.payouts)],
    )
    if not p or p.role != ROLE_PARTNER:
        raise HTTPException(404, "Партнёр не найден")
    return p


def _back(partner_id: int, tab: str = "settings") -> RedirectResponse:
    suffix = "" if tab == "stats" else f"/{tab}"
    return RedirectResponse(f"/admin/partners/{partner_id}{suffix}", 303)


def _opt_int(value: str, *, minimum: int | None = None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    n = int(value)
    if minimum is not None and n < minimum:
        raise ValueError
    return n


# --------------------------------------------------------------------------- dashboard


@router.get("")
def dashboard(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    q = request.query_params
    period = resolve_period(q.get("period"), q.get("from"), q.get("to"))
    scope = rs.Scope(all_users=True)

    board = []
    for p in partners(db):
        ps = partner_scope(p)
        board.append(
            {
                "partner": p,
                "stats": rs.overview(ps, period.start, period.end),
                "balance": reward_balance(db, p),
            }
        )
    board.sort(key=lambda r: (r["stats"]["new_buyers"], r["stats"]["registrations"]), reverse=True)

    return render(
        request,
        "admin/dashboard.html",
        me,
        period=period,
        stats=rs.overview(scope, period.start, period.end),
        chart=chart_payload(scope, period),
        sources=rs.sources_breakdown(period.start, period.end),
        gateways=rs.breakdown(scope, period.start, period.end, "gateway"),
        plans=rs.breakdown(scope, period.start, period.end, "plan"),
        purchase_types=rs.breakdown(scope, period.start, period.end, "purchase_type"),
        board=board,
        recent=rs.recent_payments(scope, 20),
        geo=rs.geography(scope, period.start, period.end),
        platforms=rs.platforms(scope),
        due_total=sum(max(0, r["balance"].due) for r in board),
    )


@router.get("/users")
def all_users(request: Request, me: Account = Depends(require_admin)):
    q = request.query_params
    try:
        page = max(1, int(q.get("page", 1)))
    except ValueError:
        page = 1
    flt = q.get("filter", "all")
    flt = flt if flt in rs.USER_FILTERS else "all"
    search = q.get("q", "").strip()[:64]
    rows, total = rs.users_page(
        rs.Scope(all_users=True), page=page, per_page=PER_PAGE, search=search, flt=flt
    )
    return render(
        request,
        "admin/users.html",
        me,
        rows=rows,
        total=total,
        page=page,
        pages=max(1, -(-total // PER_PAGE)),
        flt=flt,
        search=search,
    )


@router.post("/cache/clear", dependencies=[Depends(verify_csrf)])
def clear_cache(request: Request, me: Account = Depends(require_admin)):
    rs.clear_cache()
    flash(request, "Кэш статистики сброшен")
    return RedirectResponse(request.headers.get("referer") or "/admin", 303)


# --------------------------------------------------------------------------- partners


@router.get("/partners")
def partner_list(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    rows = [{"partner": p, "balance": reward_balance(db, p)} for p in partners(db)]
    return render(request, "admin/partners.html", me, rows=rows)


@router.get("/partners/new")
def partner_new_form(
    request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)
):
    prefill = {}
    ref = request.query_params.get("referrer", "").strip()[:64]
    if ref:
        u = rs.users_by_ids([ref]).get(ref) or {}
        login = re.sub(r"[^a-z0-9_.-]", "", (u.get("username") or "").lower())
        prefill = {
            "referrer": ref,
            "name": u.get("name") or u.get("username") or "",
            "login": login if len(login) >= 3 else f"partner{ref}"[:64].lower(),
            "telegram_id": u.get("telegram_id") or "",
        }
    return render(
        request,
        "admin/partner_new.html",
        me,
        prefill=prefill,
        default_days=default_reward_days(db),
        suggested_password=secrets.token_urlsafe(9),
    )


@router.post("/partners/new", dependencies=[Depends(verify_csrf)])
def partner_create(
    request: Request,
    login: str = Form(...),
    name: str = Form(""),
    password: str = Form(...),
    telegram_id: str = Form(""),
    referrer: str = Form(""),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    login = login.strip().lower()
    if not LOGIN_RE.match(login):
        flash(request, "Логин: 3–64 символа, латиница, цифры, _ . -", "error")
        return RedirectResponse("/admin/partners/new", 303)
    if len(password) < 8:
        flash(request, "Пароль должен быть не короче 8 символов", "error")
        return RedirectResponse("/admin/partners/new", 303)
    try:
        tg = _opt_int(telegram_id)
    except ValueError:
        flash(request, "Telegram ID должен быть числом", "error")
        return RedirectResponse("/admin/partners/new", 303)

    partner = Account(
        login=login,
        name=name.strip()[:128] or login,
        password_hash=hash_password(password),
        role=ROLE_PARTNER,
        telegram_id=tg,
    )
    db.add(partner)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        flash(request, f"Логин «{login}» уже занят", "error")
        return RedirectResponse("/admin/partners/new", 303)

    referrer = referrer.strip()[:64]
    if referrer:
        u = rs.users_by_ids([referrer]).get(referrer) or {"user_id": referrer}
        db.add(
            PartnerSource(
                partner_id=partner.id, kind=SOURCE_REFERRER, ref=referrer, label=_user_label(u)
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "Этот реферер уже привязан к другому партнёру", "error")
        return RedirectResponse("/admin/partners/new", 303)
    rs.clear_cache()
    flash(request, f"Партнёр создан. Логин: {login}, пароль: {password} — передайте его партнёру.")
    return _back(partner.id)


@router.get("/partners/{partner_id}")
def partner_view(
    partner_id: int,
    request: Request,
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    return render(
        request,
        "admin/partner_stats.html",
        me,
        tab="stats",
        **partner_dashboard_context(db, p, request),
    )


@router.get("/partners/{partner_id}/users")
def partner_users(
    partner_id: int,
    request: Request,
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    return render(
        request, "admin/partner_users.html", me, tab="users", **partner_users_context(p, request)
    )


@router.get("/partners/{partner_id}/settings")
def partner_settings(
    partner_id: int,
    request: Request,
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    taken = {
        s.ref: s.partner_id
        for s in db.scalars(select(PartnerSource).where(PartnerSource.kind == SOURCE_AD))
    }
    return render(
        request,
        "admin/partner_settings.html",
        me,
        tab="settings",
        partner=p,
        balance=reward_balance(db, p),
        default_days=default_reward_days(db),
        source_list=rs.source_list(),
        taken_sources=taken,
    )


@router.post("/partners/{partner_id}/edit", dependencies=[Depends(verify_csrf)])
def partner_edit(
    partner_id: int,
    request: Request,
    name: str = Form(""),
    telegram_id: str = Form(""),
    reward_days: str = Form(""),
    note: str = Form(""),
    is_active: bool = Form(False),
    include_second_level: bool = Form(False),
    show_contacts: bool = Form(False),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    try:
        p.telegram_id = _opt_int(telegram_id)
        p.reward_days = _opt_int(reward_days, minimum=0)
    except ValueError:
        flash(request, "Telegram ID и дни должны быть целыми неотрицательными числами", "error")
        return _back(p.id)
    p.name = name.strip()[:128] or p.login
    p.note = note.strip()[:2000]
    if p.is_active and not is_active:
        p.session_version += 1  # kick out
    p.is_active = is_active
    p.include_second_level = include_second_level
    p.show_contacts = show_contacts
    db.commit()
    rs.clear_cache()
    flash(request, "Сохранено")
    return _back(p.id)


@router.post("/partners/{partner_id}/password", dependencies=[Depends(verify_csrf)])
def partner_password(
    partner_id: int,
    request: Request,
    password: str = Form(...),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    if len(password) < 8:
        flash(request, "Пароль должен быть не короче 8 символов", "error")
        return _back(p.id)
    p.password_hash = hash_password(password)
    p.session_version += 1
    db.commit()
    flash(request, f"Новый пароль для {p.login}: {password}")
    return _back(p.id)


@router.post("/partners/{partner_id}/delete", dependencies=[Depends(verify_csrf)])
def partner_delete(
    partner_id: int,
    request: Request,
    confirm: str = Form(""),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    if confirm.strip().lower() != p.login:
        flash(request, "Для удаления введите логин партнёра", "error")
        return _back(p.id)
    db.delete(p)
    db.commit()
    rs.clear_cache()
    flash(request, f"Партнёр {p.login} удалён")
    return RedirectResponse("/admin/partners", 303)


# --------------------------------------------------------------------------- sources


def _user_label(u: dict) -> str:
    parts = [u.get("name") or f"id {u.get('user_id')}"]
    if u.get("username"):
        parts.append(f"@{u['username']}")
    if u.get("telegram_id"):
        parts.append(f"tg {u['telegram_id']}")
    return " · ".join(x for x in parts if x)[:255]


@router.post("/partners/{partner_id}/sources", dependencies=[Depends(verify_csrf)])
def source_add(
    partner_id: int,
    request: Request,
    kind: str = Form(...),
    query: str = Form(""),
    source_name: str = Form(""),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    if kind == SOURCE_REFERRER:
        u = rs.find_user(query)
        if not u:
            flash(request, f"Пользователь «{query}» не найден среди полученных от бота", "error")
            return _back(p.id)
        source = PartnerSource(partner_id=p.id, kind=kind, ref=u["user_id"], label=_user_label(u))
    elif kind == SOURCE_AD and source_name.strip():
        name = source_name.strip()[:128]
        source = PartnerSource(partner_id=p.id, kind=kind, ref=name, label=name)
    else:
        flash(request, "Не выбран источник", "error")
        return _back(p.id)

    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        owner = db.scalar(
            select(PartnerSource).where(
                PartnerSource.kind == source.kind, PartnerSource.ref == source.ref
            )
        )
        who = owner.partner.login if owner else "?"
        flash(request, f"Этот источник уже привязан к партнёру {who}", "error")
        return _back(p.id)
    rs.clear_cache()
    flash(request, f"Источник добавлен: {source.label}")
    return _back(p.id)


@router.post(
    "/partners/{partner_id}/sources/{source_id}/delete", dependencies=[Depends(verify_csrf)]
)
def source_delete(
    partner_id: int,
    source_id: int,
    request: Request,
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    source = db.get(PartnerSource, source_id)
    if source and source.partner_id == partner_id:
        db.delete(source)
        db.commit()
        rs.clear_cache()
        flash(request, "Источник отвязан")
    return _back(partner_id)


# --------------------------------------------------------------------------- payouts


@router.post("/partners/{partner_id}/payouts", dependencies=[Depends(verify_csrf)])
def payout_add(
    partner_id: int,
    request: Request,
    days: str = Form(...),
    note: str = Form(""),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = _partner(db, partner_id)
    try:
        n = int(days)
        if n == 0 or abs(n) > 100000:
            raise ValueError
    except ValueError:
        flash(request, "Укажите количество дней (целое, не ноль)", "error")
        return _back(p.id)
    db.add(Payout(partner_id=p.id, days=n, note=note.strip()[:255], created_by_id=me.id))
    db.commit()
    flash(request, f"Записана выдача: {n} дн.")
    return _back(p.id)


@router.post("/payouts/{payout_id}/delete", dependencies=[Depends(verify_csrf)])
def payout_delete(
    payout_id: int,
    request: Request,
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payout = db.get(Payout, payout_id)
    if not payout:
        raise HTTPException(404)
    pid = payout.partner_id
    db.delete(payout)
    db.commit()
    flash(request, "Запись о выдаче удалена")
    return _back(pid)


@router.get("/payouts")
def payouts(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    rows = [{"partner": p, "balance": reward_balance(db, p)} for p in partners(db)]
    rows.sort(key=lambda r: r["balance"].due, reverse=True)
    log = list(
        db.scalars(
            select(Payout)
            .options(selectinload(Payout.partner))
            .order_by(Payout.created_at.desc())
            .limit(200)
        )
    )
    return render(request, "admin/payouts.html", me, rows=rows, log=log)


# --------------------------------------------------------------------------- referrers & settings


@router.get("/referrers")
def referrers(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    assigned = {
        s.ref: s.partner
        for s in db.scalars(
            select(PartnerSource)
            .options(selectinload(PartnerSource.partner))
            .where(PartnerSource.kind == SOURCE_REFERRER)
        )
    }
    return render(
        request,
        "admin/referrers.html",
        me,
        rows=rs.top_referrers(100),
        assigned=assigned,
        source_list=rs.source_list(),
    )


@router.get("/settings")
def settings_page(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    return render(
        request,
        "admin/settings.html",
        me,
        default_days=default_reward_days(db),
    )


@router.post("/settings", dependencies=[Depends(verify_csrf)])
def settings_save(
    request: Request,
    reward_days: str = Form(...),
    me: Account = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        n = int(reward_days)
        if n < 0 or n > 3650:
            raise ValueError
    except ValueError:
        flash(request, "Дни: целое число от 0 до 3650", "error")
        return RedirectResponse("/admin/settings", 303)
    set_setting(db, "reward_days", str(n))
    db.commit()
    flash(request, "Настройки сохранены")
    return RedirectResponse("/admin/settings", 303)


# --------------------------------------------------------------------------- integrations


@router.get("/integrations")
def integrations(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    base = get_settings().public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return render(
        request,
        "admin/integrations.html",
        me,
        ingest_url=f"{base}/api/ingest",
        token=active_token(db),
        new_token=request.session.pop("new_token", None),
        stats=rs.ingest_stats(),
        botdb=botdb.status(),
    )


@router.post("/integrations/token", dependencies=[Depends(verify_csrf)])
def integrations_token(request: Request, me: Account = Depends(require_admin), db: Session = Depends(get_db)):
    # Shown once on the next page view, then dropped from the session.
    request.session["new_token"] = issue_token(db)
    return RedirectResponse("/admin/integrations", 303)


INTEGRATION_DIR = Path(__file__).resolve().parents[2] / "integration"
INTEGRATION_FILES = {"events_api.md", "kago_emitter.py", "remnashop_sync.py"}


@router.get("/integrations/files/{name}")
def integration_file(name: str, me: Account = Depends(require_admin)):
    path = INTEGRATION_DIR / name
    if name not in INTEGRATION_FILES or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)


# --------------------------------------------------------------------------- bot database


@router.post("/integrations/botdb/check", dependencies=[Depends(verify_csrf)])
def botdb_check(request: Request, me: Account = Depends(require_admin)):
    if not botdb.enabled():
        flash(request, "Подключение к БД бота не настроено (REMNASHOP_DSN)", "error")
        return RedirectResponse("/admin/integrations", 303)
    try:
        info = botdb.check()
    except Exception as exc:
        flash(request, f"Не удалось подключиться к БД бота: {type(exc).__name__}: {exc}", "error")
        return RedirectResponse("/admin/integrations", 303)
    if info["problems"]:
        flash(request, "БД доступна, но схема не подходит: " + "; ".join(info["problems"]), "error")
    else:
        counts = ", ".join(f"{k}: {v}" for k, v in info["counts"].items())
        migration = f", миграция {info['migration']}" if info.get("migration") else ""
        flash(request, f"Подключение работает ({info['user']}@{info['db']}{migration}). Записей — {counts}")
    return RedirectResponse("/admin/integrations", 303)


@router.post("/integrations/botdb/sync", dependencies=[Depends(verify_csrf)])
def botdb_sync(
    request: Request, full: bool = Form(False), me: Account = Depends(require_admin)
):
    if not botdb.enabled():
        flash(request, "Подключение к БД бота не настроено (REMNASHOP_DSN)", "error")
        return RedirectResponse("/admin/integrations", 303)
    if full:
        botdb.reset_cursors()
    botdb.trigger()
    flash(request, "Полная пересинхронизация запущена" if full else "Синхронизация запущена")
    return RedirectResponse("/admin/integrations", 303)
