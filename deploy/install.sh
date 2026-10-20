#!/usr/bin/env bash
# Full deployment of the Referral Panel on Ubuntu 22.04 / 24.04.
#
# Run as root on the server where Remnashop runs (or any server that can reach its PostgreSQL):
#   sudo bash deploy/install.sh
#
# Re-running the script updates the code and dependencies and keeps existing settings,
# so it doubles as the update command.
#
# Non-interactive use: set any of these variables beforehand.
#   DOMAIN=panel.example.com          domain pointed at this server (empty = plain HTTP by IP)
#   LE_EMAIL=me@example.com           e-mail for Let's Encrypt (empty = no certificate)
#   REMNASHOP_DSN=postgresql://...    skip auto-detection and use this connection string
#   REMNASHOP_CONTAINER=remnashop-db  name of the Remnashop PostgreSQL container
#   ADMIN_LOGIN=admin                 first admin login (password is generated)
#   APP_NAME="Referral Panel"         title shown in the panel
#   TIMEZONE=Europe/Moscow            timezone for daily statistics
#   REPO_URL=https://github.com/...   clone from git instead of using the script's directory
#   BRANCH=main
#   ASSUME_YES=1                      do not ask questions, use defaults / variables above

set -Eeuo pipefail

APP_USER="refpanel"
APP_DIR="/opt/referral-panel"
DATA_DIR="/var/lib/referral-panel"
CONF_DIR="/etc/referral-panel"
ENV_FILE="$CONF_DIR/panel.env"
SERVICE="referral-panel"
APP_PORT="${APP_PORT:-8090}"
REMNASHOP_CONTAINER="${REMNASHOP_CONTAINER:-remnashop-db}"
RO_ROLE="panel_ro"

c_green=$'\e[32m'; c_yellow=$'\e[33m'; c_red=$'\e[31m'; c_bold=$'\e[1m'; c_off=$'\e[0m'
info() { echo "${c_green}==>${c_off} $*"; }
warn() { echo "${c_yellow}[!]${c_off} $*" >&2; }
die()  { echo "${c_red}[x]${c_off} $*" >&2; exit 1; }
trap 'die "Ошибка в строке $LINENO: $BASH_COMMAND"' ERR

ask() {  # ask VAR "question" "default"
    local __var="$1" __q="$2" __def="${3:-}" __ans
    if [[ -n "${!__var:-}" ]]; then return; fi
    if [[ "${ASSUME_YES:-0}" == "1" || ! -t 0 ]]; then
        printf -v "$__var" '%s' "$__def"; return
    fi
    read -r -p "$__q${__def:+ [$__def]}: " __ans || true
    printf -v "$__var" '%s' "${__ans:-$__def}"
}

env_get() {  # env_get KEY -> value from existing env file
    [[ -f "$ENV_FILE" ]] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -E "s/^['\"](.*)['\"]$/\1/" || true
}

rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
randhex() { python3 -c "import secrets; print(secrets.token_hex($1))"; }

# --------------------------------------------------------------------------- checks

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash $0"
[[ -r /etc/os-release ]] && . /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    warn "Скрипт рассчитан на Ubuntu (обнаружено: ${PRETTY_NAME:-unknown}). Продолжаю на свой риск."
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRST_INSTALL=1
[[ -f "$ENV_FILE" ]] && FIRST_INSTALL=0

# --------------------------------------------------------------------------- packages

info "Установка системных пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx git curl ca-certificates \
    certbot python3-certbot-nginx tzdata >/dev/null

PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
(( PYV >= 310 )) || die "Нужен Python 3.10+, установлен $(python3 --version)"

# --------------------------------------------------------------------------- settings

if (( FIRST_INSTALL )); then
    info "Первичная настройка"
    ask DOMAIN "Домен панели (A-запись должна указывать на этот сервер; пусто — доступ по IP без HTTPS)" ""
    if [[ -n "$DOMAIN" ]]; then
        ask LE_EMAIL "E-mail для сертификата Let's Encrypt (пусто — без HTTPS)" ""
    fi
    ask APP_NAME "Название панели" "Referral Panel"
    ask TIMEZONE "Часовой пояс для статистики" "Europe/Moscow"
    ask ADMIN_LOGIN "Логин администратора" "admin"
else
    info "Найдена существующая установка — обновление (настройки в $ENV_FILE сохраняются)"
    DOMAIN="${DOMAIN:-$(env_get PANEL_DOMAIN)}"
fi

# --------------------------------------------------------------------------- Remnashop DB access

setup_readonly_role() {
    # Creates (or re-keys) a read-only role inside the Remnashop PostgreSQL container.
    local db_user db_name pass
    db_user=$(docker exec "$REMNASHOP_CONTAINER" printenv POSTGRES_USER 2>/dev/null || echo remnashop)
    db_name=$(docker exec "$REMNASHOP_CONTAINER" printenv POSTGRES_DB 2>/dev/null || echo remnashop)
    pass=$(randhex 24)

    info "Создаю read-only роль '$RO_ROLE' в базе '$db_name' (контейнер $REMNASHOP_CONTAINER)"
    docker exec -i "$REMNASHOP_CONTAINER" psql -v ON_ERROR_STOP=1 -q -U "$db_user" -d "$db_name" >/dev/null <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$RO_ROLE') THEN
    CREATE ROLE $RO_ROLE LOGIN PASSWORD '$pass';
  ELSE
    ALTER ROLE $RO_ROLE LOGIN PASSWORD '$pass';
  END IF;
END
\$\$;
ALTER ROLE $RO_ROLE SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE "$db_name" TO $RO_ROLE;
GRANT USAGE ON SCHEMA public TO $RO_ROLE;
GRANT SELECT ON referrals, transactions, subscriptions, ad_links TO $RO_ROLE;
-- Only non-sensitive columns of users (no e-mails / password hashes).
GRANT SELECT (id, telegram_id, username, name, referral_code, created_at, ad_link_id,
              current_subscription_id, is_blocked) ON users TO $RO_ROLE;
SQL

    # Where is the database reachable from the host?
    local host port
    port=$(docker port "$REMNASHOP_CONTAINER" 5432/tcp 2>/dev/null | head -1 | awk -F: '{print $NF}')
    if [[ -n "$port" ]]; then
        host="127.0.0.1"
    else
        host=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
            "$REMNASHOP_CONTAINER" | awk '{print $1}')
        port=5432
        warn "Порт PostgreSQL не проброшен на хост, использую IP контейнера $host."
        warn "IP может смениться после пересоздания контейнера — тогда перезапустите этот скрипт."
    fi
    [[ -n "$host" ]] || die "Не удалось определить адрес БД Remnashop"
    REMNASHOP_DSN="postgresql://$RO_ROLE:$pass@$host:$port/$db_name"
}

if [[ -z "${REMNASHOP_DSN:-}" ]]; then
    existing_dsn=$(env_get REMNASHOP_DSN)
    if [[ -n "$existing_dsn" && "${RESET_DB_ACCESS:-0}" != "1" ]]; then
        REMNASHOP_DSN="$existing_dsn"
    elif command -v docker >/dev/null && docker ps --format '{{.Names}}' | grep -qx "$REMNASHOP_CONTAINER"; then
        setup_readonly_role
    else
        warn "Контейнер '$REMNASHOP_CONTAINER' не найден на этом сервере."
        ask REMNASHOP_DSN "Строка подключения к БД Remnashop (postgresql://user:pass@host:port/db)" ""
        [[ -n "$REMNASHOP_DSN" ]] || die "Без доступа к БД Remnashop панель работать не будет"
    fi
fi

# --------------------------------------------------------------------------- user & code

info "Системный пользователь и каталоги"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g "$APP_USER" -m 750 "$CONF_DIR"
install -d -m 755 "$APP_DIR"

info "Копирование кода в $APP_DIR"
if [[ -n "${REPO_URL:-}" ]]; then
    tmp=$(mktemp -d)
    git clone --depth 1 ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$tmp/src" -q
    SRC_DIR="$tmp/src"
fi
[[ -d "$SRC_DIR/app" && -f "$SRC_DIR/requirements.txt" ]] || die "Не найден код панели в $SRC_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    rm -rf "$APP_DIR/app"
    cp -a "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
    install -d "$APP_DIR/deploy" && cp -a "$SRC_DIR/deploy/." "$APP_DIR/deploy/"
fi
chown -R root:root "$APP_DIR/app" "$APP_DIR/deploy" "$APP_DIR/requirements.txt"
find "$APP_DIR/app" "$APP_DIR/deploy" -type d -exec chmod 755 {} +
find "$APP_DIR/app" "$APP_DIR/deploy" "$APP_DIR/requirements.txt" -type f -exec chmod 644 {} +

info "Python-окружение"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -m compileall -q "$APP_DIR/app"  # the service can't write to /opt

# --------------------------------------------------------------------------- env file

# Secure cookies are switched on only after a certificate is actually installed (below).
COOKIE_SECURE=$(env_get COOKIE_SECURE); COOKIE_SECURE=${COOKIE_SECURE:-false}

write_env() {
    # Read everything from the old file first: the redirection below truncates it.
    local secret app_name tz cache email tmp
    secret=$(env_get SECRET_KEY); secret=${secret:-$(rand 48)}
    app_name=${APP_NAME:-$(env_get APP_NAME)}
    tz=${TIMEZONE:-$(env_get TIMEZONE)}
    cache=${STATS_CACHE_SECONDS:-$(env_get STATS_CACHE_SECONDS)}
    email=${LE_EMAIL:-$(env_get LE_EMAIL)}
    tmp=$(mktemp "$CONF_DIR/.panel.env.XXXXXX")
    cat > "$tmp" <<EOF
# Referral Panel settings. After editing: systemctl restart $SERVICE
APP_NAME="${app_name//\"/}"
SECRET_KEY=$secret
PANEL_DB_URL=sqlite:///$DATA_DIR/panel.db
TIMEZONE=$tz
COOKIE_SECURE=$COOKIE_SECURE
STATS_CACHE_SECONDS=$cache
REMNASHOP_DSN='$REMNASHOP_DSN'
PANEL_DOMAIN=${DOMAIN:-}
LE_EMAIL=$email
EOF
    sed -i -e '/^[A-Z_]*=$/d' -e '/^[A-Z_]*=""$/d' "$tmp"  # drop empty values -> defaults apply
    chown root:"$APP_USER" "$tmp"; chmod 640 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
}
write_env

# Management command wrapper: refpanel list | create-admin LOGIN | set-password LOGIN | check-db
cat > /usr/local/bin/refpanel <<EOF
#!/bin/sh
[ "\$(id -u)" -eq 0 ] || { echo "Run as root: sudo refpanel \$*" >&2; exit 1; }
set -a; . $ENV_FILE; set +a
cd $APP_DIR && exec runuser -u $APP_USER -- $APP_DIR/venv/bin/python -m app.cli "\$@"
EOF
chmod 755 /usr/local/bin/refpanel

run_cli() { /usr/local/bin/refpanel "$@"; }

info "Проверка доступа к БД Remnashop"
if ! run_cli check-db; then
    die "Нет доступа к БД Remnashop. Проверьте REMNASHOP_DSN в $ENV_FILE и запустите скрипт снова."
fi

ADMIN_PASSWORD_SHOWN=""
if ! run_cli has-admin; then
    ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"
    ADMIN_PASSWORD_SHOWN=$(rand 12)
    run_cli create-admin "$ADMIN_LOGIN" --password "$ADMIN_PASSWORD_SHOWN" >/dev/null
fi

# --------------------------------------------------------------------------- systemd

info "Служба systemd"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Referral Panel (Remnashop statistics)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $APP_PORT \\
    --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header --log-level warning
Restart=always
RestartSec=3

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" -q
systemctl restart "$SERVICE"

# Daily backup of the panel database (partners, sources, payouts), 14 copies kept.
cat > "/etc/cron.daily/$SERVICE-backup" <<EOF
#!/bin/sh
cd $DATA_DIR && runuser -u $APP_USER -- $APP_DIR/venv/bin/python -c "
import sqlite3, datetime
src = sqlite3.connect('panel.db'); dst = sqlite3.connect('backups/panel-' + datetime.date.today().isoformat() + '.db')
src.backup(dst); dst.close(); src.close()"
find $DATA_DIR/backups -name 'panel-*.db' -mtime +14 -delete
EOF
chmod 755 "/etc/cron.daily/$SERVICE-backup"

# --------------------------------------------------------------------------- nginx

info "Nginx"
SERVER_NAME="${DOMAIN:-_}"
LISTEN_V6=""
[[ -s /proc/net/if_inet6 ]] && LISTEN_V6="listen [::]:80;"  # some VPS have IPv6 disabled
NGINX_SITE="/etc/nginx/sites-available/$SERVICE.conf"
if [[ ! -f "$NGINX_SITE" || "${REWRITE_NGINX:-0}" == "1" ]]; then
cat > "$NGINX_SITE" <<EOF
limit_req_zone \$binary_remote_addr zone=refpanel_login:10m rate=10r/m;

server {
    listen 80;
    $LISTEN_V6
    server_name $SERVER_NAME;

    client_max_body_size 1m;
    server_tokens off;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location /static/ {
        alias $APP_DIR/app/static/;
        expires 7d;
        access_log off;
    }

    location = /login {
        limit_req zone=refpanel_login burst=10 nodelay;
        include /etc/nginx/snippets/refpanel-proxy.conf;
    }

    location / {
        include /etc/nginx/snippets/refpanel-proxy.conf;
    }
}
EOF
cat > /etc/nginx/snippets/refpanel-proxy.conf <<EOF
proxy_pass http://127.0.0.1:$APP_PORT;
proxy_http_version 1.1;
proxy_set_header Host \$host;
proxy_set_header X-Real-IP \$remote_addr;
proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto \$scheme;
proxy_read_timeout 60s;
EOF
fi
ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/$SERVICE.conf"
if [[ -z "${DOMAIN:-}" && -e /etc/nginx/sites-enabled/default ]]; then
    warn "Отключаю дефолтный сайт nginx, чтобы панель открывалась по IP"
    rm -f /etc/nginx/sites-enabled/default
fi
nginx -t -q
systemctl reload nginx || systemctl restart nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 'Nginx Full' >/dev/null
fi

# --------------------------------------------------------------------------- TLS

LE_EMAIL="${LE_EMAIL:-$(env_get LE_EMAIL)}"
if [[ -n "${DOMAIN:-}" && -n "$LE_EMAIL" ]]; then
    info "Сертификат Let's Encrypt для $DOMAIN"
    if certbot --nginx -d "$DOMAIN" -m "$LE_EMAIL" --agree-tos --non-interactive --redirect \
        --keep-until-expiring -q; then
        sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=true/' "$ENV_FILE"
        systemctl restart "$SERVICE"
        URL="https://$DOMAIN"
    else
        warn "Не удалось получить сертификат (проверьте DNS-запись домена и открытый 80 порт)."
        warn "Панель доступна по HTTP. Повторите позже: certbot --nginx -d $DOMAIN"
        URL="http://$DOMAIN"
    fi
else
    URL="http://${DOMAIN:-$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')}"
fi

# --------------------------------------------------------------------------- done

info "Ожидание запуска службы"
for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then break; fi
    sleep 1
done
if ! curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "Служба не отвечает, смотрите журнал выше"
fi

echo
echo "${c_bold}Панель установлена: $URL${c_off}"
if [[ -n "$ADMIN_PASSWORD_SHOWN" ]]; then
    echo "  Логин:  ${c_bold}$ADMIN_LOGIN${c_off}"
    echo "  Пароль: ${c_bold}$ADMIN_PASSWORD_SHOWN${c_off}   (сохраните, больше он не будет показан)"
fi
cat <<EOF

  Настройки:      $ENV_FILE
  Данные панели:  $DATA_DIR (бэкапы: $DATA_DIR/backups)
  Логи:           journalctl -u $SERVICE -f
  Обновление:     sudo bash $APP_DIR/deploy/install.sh
  Команды:        sudo refpanel list | create-admin LOGIN | set-password LOGIN | check-db
EOF
if [[ "$COOKIE_SECURE" != "true" && "$URL" == http://* ]]; then
    warn "Панель работает без HTTPS. Для продакшна укажите домен и e-mail: DOMAIN=... LE_EMAIL=... bash $APP_DIR/deploy/install.sh"
fi
