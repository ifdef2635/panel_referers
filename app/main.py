import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import SessionLocal, engine, init_db
from .models import ROLE_ADMIN, Account
from . import botdb, ingest
from .routes import admin, auth, partner
from .security import hash_password
from .web import BASE_DIR, LoginRequired, render

log = logging.getLogger("panel")


def bootstrap_admin() -> None:
    s = get_settings()
    if not (s.admin_login and s.admin_password):
        return
    with SessionLocal() as db:
        if db.scalar(select(Account).where(Account.role == ROLE_ADMIN)):
            return
        db.add(
            Account(
                login=s.admin_login.strip().lower(),
                name="Администратор",
                password_hash=hash_password(s.admin_password),
                role=ROLE_ADMIN,
            )
        )
        db.commit()
        log.warning("Bootstrap admin '%s' created", s.admin_login)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bootstrap_admin()
    botdb.start()
    yield
    botdb.stop()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    )
    if not request.url.path.startswith("/static"):
        h.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="rp_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.cookie_secure,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(ingest.router)
app.include_router(auth.router)
app.include_router(partner.router)
app.include_router(admin.router)


@app.get("/healthz")
def healthz():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        return JSONResponse({"status": "error", "db": "unavailable"}, 503)
    return JSONResponse({"status": "ok"})


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Ошибка"
    if exc.status_code == 404 and detail == "Not Found":
        detail = "Страница не найдена"
    resp = render(request, "error.html", None, code=exc.status_code, message=detail)
    resp.status_code = exc.status_code
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    resp = render(request, "error.html", None, code=400, message="Некорректные данные формы")
    resp.status_code = 400
    return resp


@app.exception_handler(OperationalError)
async def _db_error(request: Request, exc: Exception):
    log.exception("Database error")
    resp = render(
        request,
        "error.html",
        None,
        code=503,
        message="Нет связи с базой данных панели. Проверьте DATABASE_URL и что PostgreSQL запущен.",
    )
    resp.status_code = 503
    return resp
