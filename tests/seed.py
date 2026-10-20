"""Fill a test database (tests/remnashop_schema.sql applied) with synthetic Remnashop data.

    python tests/seed.py postgresql://user:pass@host/db
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg

PLANS = [("Месяц", 199, 30), ("3 месяца", 499, 90), ("Год", 1690, 365)]


def seed(dsn: str, n_users: int = 600, seed_value: int = 42) -> None:
    rnd = random.Random(seed_value)
    now = datetime.now(timezone.utc)
    with psycopg.connect(dsn, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute(
            "TRUNCATE transactions, referrals, subscriptions, users, ad_links RESTART IDENTITY CASCADE"
        )
        cur.execute(
            "INSERT INTO ad_links (name, code) VALUES ('Канал A', 'chan_a'), ('Блогер B', 'blog_b'),"
            " ('Таргет', 'target')"
        )
        ids = []
        for i in range(1, n_users + 1):
            created = now - timedelta(days=rnd.uniform(0, 120), hours=rnd.uniform(0, 24))
            ad = rnd.choice([None] * 8 + [1, 2, 3])
            cur.execute(
                "INSERT INTO users (telegram_id, username, referral_code, name, ad_link_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (100000 + i, f"user{i}" if rnd.random() > 0.3 else None, f"ref{i}", f"Юзер {i}", ad, created),
            )
            ids.append((cur.fetchone()[0], created))

        # Referral trees: users 1..5 are active referrers.
        referred = set()
        for uid, created in ids[5:]:
            if rnd.random() < 0.45:
                referrer = rnd.choice([1, 2, 3, 4, 5] * 3 + [u for u, _ in ids[5:60] if u < uid])
                if referrer == uid or uid in referred:
                    continue
                parent = cur.execute(
                    "SELECT level FROM referrals WHERE referred_id = %s", (referrer,)
                ).fetchone()
                level = "SECOND" if parent else "FIRST"
                cur.execute(
                    "INSERT INTO referrals (referrer_id, referred_id, level, created_at)"
                    " VALUES (%s, %s, %s, %s)",
                    (referrer, uid, level, created),
                )
                referred.add(uid)

        for uid, created in ids:
            sub_id = None
            if rnd.random() < 0.6:  # trial
                t_at = created + timedelta(minutes=5)
                cur.execute(
                    "INSERT INTO subscriptions (user_id, status, is_trial, expire_at, plan_snapshot, created_at)"
                    " VALUES (%s, %s, true, %s, %s, %s) RETURNING id",
                    (uid, "ACTIVE" if t_at + timedelta(days=3) > now else "EXPIRED",
                     t_at + timedelta(days=3), json.dumps({"name": "Пробный"}), t_at),
                )
                sub_id = cur.fetchone()[0]
                cur.execute("UPDATE users SET is_trial_available = false WHERE id = %s", (uid,))
            if rnd.random() < 0.35:  # buyer
                pay_at = created + timedelta(days=rnd.uniform(0, 5))
                while pay_at < now:
                    name, price, days = rnd.choice(PLANS)
                    gw, curr = rnd.choice([("YOOKASSA", "RUB")] * 4 + [("TELEGRAM_STARS", "XTR"), ("CRYPTOPAY", "USD")])
                    amount = {"RUB": price, "XTR": price // 2, "USD": round(price / 90, 2)}[curr]
                    status = rnd.choice(["COMPLETED"] * 12 + ["CANCELED", "REFUNDED", "PENDING"])
                    cur.execute(
                        "INSERT INTO transactions (user_id, status, is_test, purchase_type, gateway_type,"
                        " pricing, currency, plan_snapshot, created_at) VALUES (%s,%s,false,%s,%s,%s,%s,%s,%s)",
                        (uid, status, "NEW" if sub_id is None else "RENEW", gw,
                         json.dumps({"original_amount": str(amount), "discount_percent": 0, "final_amount": str(amount)}),
                         curr, json.dumps({"name": name, "duration": days}), pay_at),
                    )
                    if status == "COMPLETED":
                        exp = pay_at + timedelta(days=days)
                        cur.execute(
                            "INSERT INTO subscriptions (user_id, status, is_trial, expire_at, plan_snapshot, created_at)"
                            " VALUES (%s, %s, false, %s, %s, %s) RETURNING id",
                            (uid, "ACTIVE" if exp > now else "EXPIRED", exp, json.dumps({"name": name}), pay_at),
                        )
                        sub_id = cur.fetchone()[0]
                    pay_at += timedelta(days=days if rnd.random() < 0.6 else 1000)
            if sub_id:
                cur.execute("UPDATE users SET current_subscription_id = %s WHERE id = %s", (sub_id, uid))
        # A test payment and a free (100% discount) one must never count.
        cur.execute(
            "INSERT INTO transactions (user_id, status, is_test, purchase_type, gateway_type, pricing,"
            " currency, plan_snapshot) VALUES (6, 'COMPLETED', true, 'NEW', 'YOOKASSA',"
            " '{\"final_amount\": \"999\"}', 'RUB', '{\"name\": \"test\"}'),"
            " (6, 'COMPLETED', false, 'NEW', 'YOOKASSA', '{\"final_amount\": \"0\"}', 'RUB', '{\"name\": \"free\"}')"
        )
        conn.commit()


if __name__ == "__main__":
    seed(sys.argv[1])
    print("seeded")
