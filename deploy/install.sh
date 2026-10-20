#!/usr/bin/env bash
# Full deployment of Kago Analytics on Ubuntu 22.04 / 24.04.
#
#   sudo bash deploy/install.sh
#
# Installs PostgreSQL, Python environment, systemd service, nginx with HTTPS
# (Let's Encrypt for a domain, otherwise a self-signed certificate for the server IP),
# daily database backups, and issues the ingest token for the bot.
# Re-running the script updates the code and keeps all data and settings.
#
# Non-interactive use: set any of these variables beforehand.
#   DOMAIN=stats.example.com    domain pointed at this server (empty = HTTPS by IP, self-signed)
#   LE_EMAIL=me@example.com     e-mail for Let's Encrypt (needed together with DOMAIN)
#   PUBLIC_IP=1.2.3.4           server address for the self-signed certificate (auto-detected)
#   ADMIN_LOGIN=admin           first admin login (password is generated)
#   APP_NAME="Kago Analytics"   title shown in the panel
#   TIMEZONE=Europe/Moscow      timezone for daily statistics
#   REPO_URL=https://github.com/...  BRANCH=main   clone from git instead of the script's directory
#   ASSUME_YES=1                do not ask questions

set -Eeuo pipefail

APP_USER="kago"
APP_DIR="/opt/kago-analytics"
DATA_DIR="/var/lib/kago"
CONF_DIR="/etc/kago"
ENV_FILE="$CONF_DIR/kago.env"
TLS_DIR="$CONF_DIR/tls"
ACME_DIR="/var/www/kago-acme"
SERVICE="kago-analytics"
APP_PORT="${APP_PORT:-8090}"
DB_NAME="kago"
DB_USER="kago"

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

env_get() {  # env_get KEY -> value from the existing env file, quotes stripped
    [[ -f "$ENV_FILE" ]] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -E "s/^['\"](.*)['\"]$/\1/" || true
}

rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
randhex() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
pg() { (cd / && runuser -u postgres -- psql -v ON_ERROR_STOP=1 -qtA "$@"); }

# --------------------------------------------------------------------------- checks

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash $0"
[[ -r /etc/os-release ]] && . /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    warn "Скрипт рассчитан на Ubuntu (обнаружено: ${PRETTY_NAME:-unknown}). Продолжаю на свой риск."
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRST_INSTALL=1
[[ -f "$ENV_FILE" ]] && FIRST_INSTALL=0

# --------------------------------------------------------------------------- settings

if (( FIRST_INSTALL )); then
    info "Первичная настройка"
    ask DOMAIN "Домен панели (A-запись на этот сервер; пусто — HTTPS по IP с самоподписанным сертификатом)" ""
    if [[ -n "$DOMAIN" ]]; then
        ask LE_EMAIL "E-mail для сертификата Let's Encrypt" ""
    fi
    ask APP_NAME "Название панели" "Kago Analytics"
    ask TIMEZONE "Часовой пояс для статистики" "Europe/Moscow"
    ask ADMIN_LOGIN "Логин администратора" "admin"
else
    info "Найдена существующая установка — обновление (данные и настройки сохраняются)"
    DOMAIN="${DOMAIN:-$(env_get PANEL_DOMAIN)}"
    LE_EMAIL="${LE_EMAIL:-$(env_get LE_EMAIL)}"
fi

# --------------------------------------------------------------------------- packages

info "Установка системных пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx postgresql git curl openssl \
    ca-certificates certbot tzdata >/dev/null

PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
(( PYV >= 310 )) || die "Нужен Python 3.10+, установлен $(python3 --version)"

if [[ -z "${PUBLIC_IP:-}" ]]; then
    PUBLIC_IP=$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || true)
    [[ "$PUBLIC_IP" =~ ^[0-9.]+$ ]] || PUBLIC_IP=$(hostname -I | awk '{print $1}')
fi
HOST="${DOMAIN:-$PUBLIC_IP}"
PUBLIC_URL="https://$HOST"

# --------------------------------------------------------------------------- PostgreSQL

info "PostgreSQL"
systemctl enable --now postgresql -q 2>/dev/null || systemctl start postgresql
for _ in $(seq 1 20); do runuser -u postgres -- pg_isready -q && break; sleep 1; done

DATABASE_URL="$(env_get DATABASE_URL)"
if [[ -z "$DATABASE_URL" ]]; then
    DB_PASS=$(randhex 24)
    if [[ "$(pg -c "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'")" == "1" ]]; then
        pg -c "ALTER ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'"
    else
        pg -c "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'"
    fi
    if [[ "$(pg -c "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]]; then
        pg -c "CREATE DATABASE $DB_NAME OWNER $DB_USER"
    fi
    DATABASE_URL="postgresql+psycopg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME"
fi

# --------------------------------------------------------------------------- user & code

info "Системный пользователь и каталоги"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$DATA_DIR"
install -d -o postgres -g postgres -m 750 "$DATA_DIR/backups"
install -d -o root -g "$APP_USER" -m 750 "$CONF_DIR"
install -d -m 700 "$TLS_DIR"
install -d -m 755 "$APP_DIR" "$ACME_DIR"

info "Копирование кода в $APP_DIR"
if [[ -n "${REPO_URL:-}" ]]; then
    tmp=$(mktemp -d)
    git clone -q --depth 1 ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$tmp/src"
    SRC_DIR="$tmp/src"
fi
[[ -d "$SRC_DIR/app" && -f "$SRC_DIR/requirements.txt" ]] || die "Не найден код панели в $SRC_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    rm -rf "$APP_DIR/app" "$APP_DIR/integration" "$APP_DIR/deploy"
    cp -a "$SRC_DIR/app" "$SRC_DIR/integration" "$SRC_DIR/deploy" "$SRC_DIR/requirements.txt" "$APP_DIR/"
fi
CODE=("$APP_DIR/app" "$APP_DIR/integration" "$APP_DIR/deploy" "$APP_DIR/requirements.txt")
chown -R root:root "${CODE[@]}"
find "${CODE[@]}" -type d -exec chmod 755 {} +
find "${CODE[@]}" -type f -exec chmod 644 {} +

info "Python-окружение"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -m compileall -q "$APP_DIR/app"  # the service can't write to /opt

# --------------------------------------------------------------------------- env file

write_env() {
    # Read everything from the old file first: writing replaces it.
    local secret app_name tz cache tmp
    secret=$(env_get SECRET_KEY); secret=${secret:-$(rand 48)}
    app_name=${APP_NAME:-$(env_get APP_NAME)}
    tz=${TIMEZONE:-$(env_get TIMEZONE)}
    cache=${STATS_CACHE_SECONDS:-$(env_get STATS_CACHE_SECONDS)}
    tmp=$(mktemp "$CONF_DIR/.kago.env.XXXXXX")
    cat > "$tmp" <<EOF
# Kago Analytics settings. After editing: systemctl restart $SERVICE
APP_NAME="${app_name//\"/}"
SECRET_KEY=$secret
DATABASE_URL='$DATABASE_URL'
PUBLIC_URL=$PUBLIC_URL
TIMEZONE=$tz
COOKIE_SECURE=true
STATS_CACHE_SECONDS=$cache
PANEL_DOMAIN=${DOMAIN:-}
LE_EMAIL=${LE_EMAIL:-}
EOF
    sed -i -e '/^[A-Z_]*=$/d' -e '/^[A-Z_]*=""$/d' "$tmp"  # empty values -> defaults apply
    chown root:"$APP_USER" "$tmp"; chmod 640 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
}
write_env

# Management commands: kago list | create-admin LOGIN | set-password LOGIN | issue-token
cat > /usr/local/bin/kago <<EOF
#!/bin/sh
[ "\$(id -u)" -eq 0 ] || { echo "Run as root: sudo kago \$*" >&2; exit 1; }
set -a; . $ENV_FILE; set +a
cd $APP_DIR && exec runuser -u $APP_USER -- $APP_DIR/venv/bin/python -m app.cli "\$@"
EOF
chmod 755 /usr/local/bin/kago

ADMIN_PASSWORD_SHOWN=""
if ! kago has-admin; then
    ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"
    ADMIN_PASSWORD_SHOWN=$(rand 12)
    kago create-admin "$ADMIN_LOGIN" --password "$ADMIN_PASSWORD_SHOWN" >/dev/null
fi
INGEST_TOKEN=""
if (( FIRST_INSTALL )); then
    INGEST_TOKEN=$(kago issue-token)
fi

# --------------------------------------------------------------------------- systemd

info "Служба systemd"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Kago Analytics
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

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

# Daily database backup, 14 copies kept.
cat > "/etc/cron.daily/$SERVICE-backup" <<EOF
#!/bin/sh
runuser -u postgres -- pg_dump -Fc $DB_NAME > $DATA_DIR/backups/kago-\$(date +%F).dump
find $DATA_DIR/backups -name 'kago-*.dump' -mtime +14 -delete
EOF
chmod 755 "/etc/cron.daily/$SERVICE-backup"

# --------------------------------------------------------------------------- nginx + TLS

LISTEN_V6_80=""; LISTEN_V6_443=""
if [[ -s /proc/net/if_inet6 ]]; then  # some VPS have IPv6 disabled
    LISTEN_V6_80="listen [::]:80;"; LISTEN_V6_443="listen [::]:443 ssl;"
fi
NGINX_SITE="/etc/nginx/sites-available/$SERVICE.conf"
SERVER_NAME="${DOMAIN:-_}"

cat > /etc/nginx/snippets/kago-proxy.conf <<EOF
proxy_pass http://127.0.0.1:$APP_PORT;
proxy_http_version 1.1;
proxy_set_header Host \$host;
proxy_set_header X-Real-IP \$remote_addr;
proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto \$scheme;
proxy_read_timeout 120s;
EOF

write_http_only() {  # temporary config: serves the ACME challenge before a certificate exists
    cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    $LISTEN_V6_80
    server_name $SERVER_NAME;
    location /.well-known/acme-challenge/ { root $ACME_DIR; }
    location / { include /etc/nginx/snippets/kago-proxy.conf; }
}
EOF
}

write_https() {  # write_https CERT KEY HSTS(0/1)
    local hsts=""
    [[ "$3" == "1" ]] && hsts='add_header Strict-Transport-Security "max-age=31536000" always;'
    cat > "$NGINX_SITE" <<EOF
limit_req_zone \$binary_remote_addr zone=kago_login:10m rate=10r/m;

server {
    listen 80;
    $LISTEN_V6_80
    server_name $SERVER_NAME;
    location /.well-known/acme-challenge/ { root $ACME_DIR; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl;
    $LISTEN_V6_443
    http2 on;
    server_name $SERVER_NAME;

    ssl_certificate     $1;
    ssl_certificate_key $2;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:kago_ssl:10m;
    server_tokens off;
    $hsts

    client_max_body_size 1m;

    location /static/ {
        alias $APP_DIR/app/static/;
        expires 7d;
        access_log off;
    }
    location = /api/ingest {
        client_max_body_size 2m;  # the app itself answers 413 above 1 MB
        include /etc/nginx/snippets/kago-proxy.conf;
    }
    location = /login {
        limit_req zone=kago_login burst=10 nodelay;
        include /etc/nginx/snippets/kago-proxy.conf;
    }
    location / {
        include /etc/nginx/snippets/kago-proxy.conf;
    }
}
EOF
}

reload_nginx() {
    nginx -t -q
    systemctl reload nginx 2>/dev/null || systemctl restart nginx
}

info "Nginx и HTTPS"
ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/$SERVICE.conf"
rm -f /etc/nginx/sites-enabled/default  # the panel takes ports 80/443 of this server
# nginx before 1.25 does not know "http2 on;"
NGX=$(nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
OLD_NGINX=0; [[ "$(printf '%s\n1.25\n' "$NGX" | sort -V | head -1)" != "1.25" ]] && OLD_NGINX=1

CERT=""; KEY=""; HSTS=0
if [[ -n "${DOMAIN:-}" && -n "${LE_EMAIL:-}" ]]; then
    LE_LIVE="/etc/letsencrypt/live/$DOMAIN"
    if [[ ! -f "$LE_LIVE/fullchain.pem" ]]; then
        [[ -f "$NGINX_SITE" ]] || write_http_only
        reload_nginx
        info "Получение сертификата Let's Encrypt для $DOMAIN"
        certbot certonly --webroot -w "$ACME_DIR" -d "$DOMAIN" -m "$LE_EMAIL" \
            --agree-tos --non-interactive -q \
            || warn "Не удалось получить сертификат: проверьте A-запись $DOMAIN и открытый 80 порт."
    fi
    if [[ -f "$LE_LIVE/fullchain.pem" ]]; then
        CERT="$LE_LIVE/fullchain.pem"; KEY="$LE_LIVE/privkey.pem"; HSTS=1
        install -d /etc/letsencrypt/renewal-hooks/deploy
        printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/kago-nginx
        chmod 755 /etc/letsencrypt/renewal-hooks/deploy/kago-nginx
    fi
fi
if [[ -z "$CERT" ]]; then
    CERT="$TLS_DIR/selfsigned.crt"; KEY="$TLS_DIR/selfsigned.key"
    SAN="IP:$PUBLIC_IP"; [[ -n "${DOMAIN:-}" ]] && SAN="DNS:$DOMAIN,$SAN"
    if [[ ! -f "$CERT" ]] || ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -q "${DOMAIN:-$PUBLIC_IP}"; then
        info "Самоподписанный сертификат для $HOST"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$HOST" \
            -addext "subjectAltName=$SAN" -keyout "$KEY" -out "$CERT" 2>/dev/null
        chmod 600 "$KEY"
    fi
fi
write_https "$CERT" "$KEY" "$HSTS"
if (( OLD_NGINX )); then
    sed -i -e '/^    http2 on;$/d' -e 's/listen 443 ssl;/listen 443 ssl http2;/' \
        -e 's/listen \[::\]:443 ssl;/listen [::]:443 ssl http2;/' "$NGINX_SITE"
fi
reload_nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 'Nginx Full' >/dev/null
fi

# --------------------------------------------------------------------------- done

info "Ожидание запуска службы"
for _ in $(seq 1 20); do
    curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1 && break
    sleep 1
done
if ! curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "Служба не отвечает, смотрите журнал выше"
fi

CURL_K=""; (( HSTS )) || CURL_K="-k "
echo
echo "${c_bold}Панель установлена: $PUBLIC_URL${c_off}"
if [[ -n "$ADMIN_PASSWORD_SHOWN" ]]; then
    echo "  Логин:  ${c_bold}$ADMIN_LOGIN${c_off}"
    echo "  Пароль: ${c_bold}$ADMIN_PASSWORD_SHOWN${c_off}   (сохраните, больше он не будет показан)"
fi
echo
echo "  Приём событий:  POST $PUBLIC_URL/api/ingest"
if [[ -n "$INGEST_TOKEN" ]]; then
    echo "  Токен бота:     ${c_bold}$INGEST_TOKEN${c_off}   (показан один раз; перевыпуск — в «Интеграции»)"
    echo
    echo "  Проверка:"
    echo "  curl ${CURL_K}-X POST $PUBLIC_URL/api/ingest -H \"Authorization: Bearer $INGEST_TOKEN\" \\"
    echo "    -H 'Content-Type: application/json' -d '{\"id\":\"test-1\",\"type\":\"user.registered\",\"ts\":\"$(date -u +%FT%T)+00:00\",\"data\":{\"user_id\":1}}'"
fi
cat <<EOF

  Настройки:   $ENV_FILE
  Бэкапы БД:   $DATA_DIR/backups (ежедневно, 14 копий)
  Логи:        journalctl -u $SERVICE -f
  Обновление:  sudo bash $APP_DIR/deploy/install.sh
  Команды:     sudo kago list | create-admin LOGIN | set-password LOGIN | issue-token
EOF
if (( !HSTS )); then
    warn "Сертификат самоподписанный: браузер покажет предупреждение. Для бота закрепите сертификат:"
    warn "  скопируйте $CERT на сервер бота и укажите KAGO_CA_FILE=<путь> (или verify=<путь> в httpx)."
    warn "Когда появится домен: sudo DOMAIN=... LE_EMAIL=... bash $APP_DIR/deploy/install.sh"
fi
