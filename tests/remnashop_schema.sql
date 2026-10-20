-- Minimal subset of the Remnashop schema (as of migration 0040) used by the panel.
CREATE TYPE currency AS ENUM ('USD', 'XTR', 'RUB');
CREATE TYPE user_role AS ENUM ('DEV', 'ADMIN', 'USER', 'PREVIEW', 'OWNER', 'SYSTEM');
CREATE TYPE transaction_status AS ENUM ('PENDING', 'COMPLETED', 'CANCELED', 'REFUNDED', 'FAILED');
CREATE TYPE subscription_status AS ENUM ('ACTIVE', 'DISABLED', 'LIMITED', 'EXPIRED', 'DELETED');
CREATE TYPE purchase_type AS ENUM ('NEW', 'RENEW', 'CHANGE');
CREATE TYPE payment_gateway_type AS ENUM ('TELEGRAM_STARS', 'YOOKASSA', 'CRYPTOPAY');
CREATE TYPE referral_level AS ENUM ('FIRST', 'SECOND');

CREATE TABLE ad_links (
  id serial PRIMARY KEY, name varchar(128) NOT NULL, code varchar(64) UNIQUE NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE users (
  id serial PRIMARY KEY, telegram_id bigint UNIQUE, username varchar(32),
  referral_code varchar(64) UNIQUE NOT NULL, name varchar NOT NULL,
  language varchar(8) NOT NULL DEFAULT 'RU',
  role user_role NOT NULL DEFAULT 'USER', is_blocked boolean NOT NULL DEFAULT false,
  is_trial_available boolean NOT NULL DEFAULT true,
  ad_link_id int REFERENCES ad_links(id) ON DELETE SET NULL,
  current_subscription_id int,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE subscriptions (
  id serial PRIMARY KEY, user_remna_id uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status subscription_status NOT NULL, is_trial boolean NOT NULL,
  expire_at timestamptz NOT NULL, url varchar NOT NULL DEFAULT '',
  plan_snapshot jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ADD FOREIGN KEY (current_subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL;
CREATE TABLE referrals (
  id serial PRIMARY KEY,
  referrer_id int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  referred_id int NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  level referral_level NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE transactions (
  id serial PRIMARY KEY, payment_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  user_id int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status transaction_status NOT NULL, is_test boolean NOT NULL DEFAULT false,
  purchase_type purchase_type NOT NULL, gateway_type payment_gateway_type NOT NULL,
  pricing jsonb NOT NULL, currency currency NOT NULL, plan_snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
