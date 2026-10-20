from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, utcnow
from ..security import DUMMY_HASH, hash_password, ip_throttle, login_throttle, verify_password
from ..web import current_account, flash, render, verify_csrf

router = APIRouter()


def _client_ip(request: Request) -> str:
    # Nginx in front sets X-Real-IP; uvicorn listens on localhost only.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    try:
        account = current_account(request, db)
    except Exception:
        return RedirectResponse("/login", 303)
    return RedirectResponse("/admin" if account.is_admin else "/p", 303)


@router.get("/login")
def login_form(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", 303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    login = login.strip().lower()
    ip = _client_ip(request)
    key = f"{ip}|{login}"
    if login_throttle.blocked(key) or ip_throttle.blocked(ip):
        flash(request, "Слишком много попыток. Подождите 15 минут.", "error")
        return RedirectResponse("/login", 303)

    account = db.scalar(select(Account).where(Account.login == login))
    ok = verify_password(password, account.password_hash if account else DUMMY_HASH)
    if not account or not ok or not account.is_active:
        login_throttle.fail(key)
        ip_throttle.fail(ip)
        flash(request, "Неверный логин или пароль", "error")
        return RedirectResponse("/login", 303)

    login_throttle.reset(key)
    account.last_login_at = utcnow()
    db.commit()
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update(uid=account.id, sv=account.session_version, csrf=csrf)
    return RedirectResponse("/", 303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@router.get("/account")
def account_page(request: Request, me: Account = Depends(current_account)):
    return render(request, "account.html", me)


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    repeat: str = Form(...),
    me: Account = Depends(current_account),
    db: Session = Depends(get_db),
):
    if not verify_password(current, me.password_hash):
        flash(request, "Текущий пароль указан неверно", "error")
    elif len(new) < 8:
        flash(request, "Новый пароль должен быть не короче 8 символов", "error")
    elif new != repeat:
        flash(request, "Пароли не совпадают", "error")
    else:
        me.password_hash = hash_password(new)
        me.session_version += 1
        db.commit()
        request.session["sv"] = me.session_version
        flash(request, "Пароль изменён. Другие сессии завершены.")
    return RedirectResponse("/account", 303)
