"""Management commands.

    python -m app.cli create-admin LOGIN [--password PASS]
    python -m app.cli set-password LOGIN [--password PASS]
    python -m app.cli list
    python -m app.cli has-admin   (exit code 0 if an admin exists)
    python -m app.cli check-db
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from . import remnashop as rs
from .db import SessionLocal, init_db
from .models import ROLE_ADMIN, Account
from .security import hash_password


def _ask_password(given: str | None) -> str:
    if given:
        return given
    while True:
        p1 = getpass.getpass("Пароль: ")
        if len(p1) < 8:
            print("Минимум 8 символов")
            continue
        if p1 == getpass.getpass("Повторите: "):
            return p1
        print("Пароли не совпадают")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "set-password"):
        sp = sub.add_parser(name)
        sp.add_argument("login")
        sp.add_argument("--password")
    sub.add_parser("list")
    sub.add_parser("has-admin")
    sub.add_parser("check-db")
    args = parser.parse_args(argv)

    init_db()
    with SessionLocal() as db:
        if args.cmd == "create-admin":
            login = args.login.strip().lower()
            account = db.scalar(select(Account).where(Account.login == login))
            if account:
                account.role = ROLE_ADMIN
                account.is_active = True
            else:
                account = Account(login=login, name="Администратор", role=ROLE_ADMIN, password_hash="")
                db.add(account)
            account.password_hash = hash_password(_ask_password(args.password))
            account.session_version = (account.session_version or 0) + 1
            db.commit()
            print(f"Администратор '{login}' готов")
        elif args.cmd == "set-password":
            account = db.scalar(select(Account).where(Account.login == args.login.strip().lower()))
            if not account:
                print("Аккаунт не найден", file=sys.stderr)
                return 1
            account.password_hash = hash_password(_ask_password(args.password))
            account.session_version += 1
            db.commit()
            print("Пароль обновлён")
        elif args.cmd == "list":
            for a in db.scalars(select(Account).order_by(Account.role, Account.login)):
                print(f"{a.id:>4}  {a.role:<8} {a.login:<24} {'on ' if a.is_active else 'off'}  {a.name}")
        elif args.cmd == "has-admin":
            return 0 if db.scalar(select(Account).where(Account.role == ROLE_ADMIN)) else 1
        elif args.cmd == "check-db":
            try:
                row = rs.fetch_one(
                    "SELECT (SELECT COUNT(*) FROM users) AS users,"
                    " (SELECT COUNT(*) FROM referrals) AS referrals,"
                    " (SELECT COUNT(*) FROM transactions) AS transactions"
                )
            except Exception as exc:
                print(f"Ошибка подключения к Remnashop: {exc}", file=sys.stderr)
                return 1
            print(f"Remnashop OK: {row}")
    rs.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
