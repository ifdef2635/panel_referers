#!/usr/bin/env bash
# Run on the VPN-bot (Remnashop) server: installs the kago-sync service, which reads the bot
# database and sends registrations and purchases to the KAGO panel every minute. With Remnawave
# access it also delivers referrer payouts: adds the days to the referrer's subscription.
#
#   sudo bash deploy/install_remnashop_sync.sh
#
# Non-interactive use: set any of these variables beforehand.
#   KAGO_URL=https://stats.example.com   panel address (or https://<panel IP>)
#   KAGO_TOKEN=kago_...                  ingest token (panel → «Интеграции», or `sudo kago issue-token`)
#   DB_CONTAINER=remnashop-db            Remnashop PostgreSQL container (found automatically)
#   REMNASHOP_DSN=postgresql://...       instead of DB_CONTAINER, when the database is not in Docker
#   REMNAWAVE_URL=https://panel.example.com  Remnawave panel, for payouts (empty = no payouts)
#   REMNAWAVE_TOKEN=...                  Remnawave API token (Remnawave → Settings → API tokens)
#   ASSUME_YES=1                         do not ask questions (also trusts the panel certificate)
# Re-running updates the script and keeps the settings.

set -Eeuo pipefail

SERVICE="kago-sync"
APP_DIR="/opt/kago-sync"
CONF_DIR="/etc/kago-sync"
ENV_FILE="$CONF_DIR/kago-sync.env"
CA_FILE="$APP_DIR/panel.crt"  # readable by the service's dynamic user
SYNC_SECONDS="${SYNC_SECONDS:-60}"

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

env_get() {
    [[ -f "$ENV_FILE" ]] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -E "s/^['\"](.*)['\"]$/\1/" || true
}

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo bash $0"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/integration/remnashop_sync.py"
[[ -f "$SRC" ]] || die "Не найден $SRC — запускайте скрипт из клона репозитория"

# --------------------------------------------------------------------------- settings

KAGO_URL="${KAGO_URL:-$(env_get KAGO_URL)}"
KAGO_TOKEN="${KAGO_TOKEN:-$(env_get KAGO_TOKEN)}"
DB_CONTAINER="${DB_CONTAINER:-$(env_get DB_CONTAINER)}"
REMNASHOP_DSN="${REMNASHOP_DSN:-$(env_get REMNASHOP_DSN)}"
REMNAWAVE_URL="${REMNAWAVE_URL:-$(env_get REMNAWAVE_URL)}"
REMNAWAVE_TOKEN="${REMNAWAVE_TOKEN:-$(env_get REMNAWAVE_TOKEN)}"

ask KAGO_URL "Адрес панели KAGO (https://домен или https://IP)" ""
[[ "$KAGO_URL" =~ ^https?://[^/[:space:]]+ ]] || die "KAGO_URL: нужен адрес вида https://stats.example.com"
KAGO_URL="${KAGO_URL%/}"
[[ "$KAGO_URL" == */api/ingest ]] || KAGO_URL="$KAGO_URL/api/ingest"

ask KAGO_TOKEN "Токен приёма событий (kago_…; панель → «Интеграции» или sudo kago issue-token)" ""
[[ "$KAGO_TOKEN" =~ ^kago_[A-Za-z0-9_-]+$ ]] || die "KAGO_TOKEN должен начинаться с kago_"

if [[ -z "$REMNASHOP_DSN" && -z "$DB_CONTAINER" ]] && command -v docker >/dev/null; then
    found=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /postgres/ {print $1}')
    guess=$(grep -i remna <<<"$found" | head -1 || true)
    guess=${guess:-$(head -1 <<<"$found")}
    if [[ -n "$found" ]]; then
        echo "Контейнеры PostgreSQL: $(tr '\n' ' ' <<<"$found")"
        ask DB_CONTAINER "Контейнер базы Remnashop (пусто — указать строку подключения)" "$guess"
    fi
fi
if [[ -z "$DB_CONTAINER" ]]; then
    ask REMNASHOP_DSN "Строка подключения к БД Remnashop (postgresql://user:pass@host:5432/db)" ""
    [[ -n "$REMNASHOP_DSN" ]] || die "Нужен контейнер базы или REMNASHOP_DSN"
    [[ "$REMNASHOP_DSN" != *"'"* ]] || die "REMNASHOP_DSN не должен содержать одинарных кавычек"
else
    docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || die "Контейнер $DB_CONTAINER не найден (docker ps)"
fi

# Remnawave access for payouts: defaults from the Remnashop containers' environment.
if [[ -z "$REMNAWAVE_TOKEN" ]] && command -v docker >/dev/null; then
    for c in $(docker ps --format '{{.Names}}'); do
        envs=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null || true)
        t=$(sed -n 's/^REMNAWAVE_\(API_\)\{0,1\}TOKEN=//p' <<<"$envs" | head -1)
        [[ -n "$t" ]] || continue
        RW_TOKEN_GUESS="$t"
        u=$(sed -n 's/^REMNAWAVE_\(HOST\|URL\|PANEL_URL\)=//p' <<<"$envs" | head -1)
        [[ "$u" == http*://* && "$u" != *localhost* ]] && RW_URL_GUESS="$u"
        break
    done
fi
echo "Автовыдача дней: kago-sync сам продлевает подписку рефереру через API Remnawave."
ask REMNAWAVE_URL "Адрес панели Remnawave (https://…; пусто — без автовыдачи)" "${RW_URL_GUESS:-}"
REMNAWAVE_URL="${REMNAWAVE_URL%/}"
if [[ -n "$REMNAWAVE_URL" ]]; then
    [[ "$REMNAWAVE_URL" =~ ^https?://[^/[:space:]]+ ]] || die "REMNAWAVE_URL: нужен адрес вида https://panel.example.com"
    if [[ -n "${RW_TOKEN_GUESS:-}" && -z "$REMNAWAVE_TOKEN" ]]; then
        echo "Найден токен Remnawave в окружении Remnashop (${RW_TOKEN_GUESS:0:12}…)"
    fi
    ask REMNAWAVE_TOKEN "API-токен Remnawave (Remnawave → Настройки → API-токены)" "${RW_TOKEN_GUESS:-}"
    [[ "$REMNAWAVE_TOKEN" =~ ^[A-Za-z0-9._-]+$ ]] || die "REMNAWAVE_TOKEN пуст или содержит недопустимые символы"
    rw_code=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $REMNAWAVE_TOKEN" \
        -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 127.0.0.1' \
        "$REMNAWAVE_URL/api/users?size=1&start=0" || true)
    case "$rw_code" in
        200) info "Remnawave: токен принят" ;;
        401|403) die "Remnawave не принял токен (HTTP $rw_code)" ;;
        *) warn "Remnawave ответил HTTP $rw_code на проверку — проверьте адрес; выдачи с ошибкой будут видны в боте" ;;
    esac
fi

# --------------------------------------------------------------------------- packages & code

info "Пакеты и Python-окружение"
if ! python3 -c 'import venv, ensurepip' 2>/dev/null || ! command -v openssl >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv openssl ca-certificates curl >/dev/null
fi
install -d -m 755 "$APP_DIR"
install -d -m 700 "$CONF_DIR"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q "psycopg[binary]==3.2.3" "httpx==0.28.1"
install -m 644 "$SRC" "$APP_DIR/remnashop_sync.py"

# Resolves the database container's address on every start: its IP changes when it is recreated.
cat > "$APP_DIR/resolve-db.sh" <<'EOF'
#!/bin/sh
# Writes REMNASHOP_DSN for the Remnashop PostgreSQL container into /run/kago-sync/db.env.
set -eu
c="$DB_CONTAINER"
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$c" | awk '{print $1}')
envs=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c")
get() { printf '%s\n' "$envs" | sed -n "s/^$1=//p" | head -1; }
user=$(get POSTGRES_USER); pass=$(get POSTGRES_PASSWORD); db=$(get POSTGRES_DB)
[ -n "$ip" ] || { echo "no IP for container $c" >&2; exit 1; }
# Percent-encoded URL: no spaces or quotes, safe in an environment file whatever the password is.
DSN=$(U="${user:-postgres}" P="$pass" D="${db:-${user:-postgres}}" H="$ip" /opt/kago-sync/venv/bin/python -c '
import os, urllib.parse
e = os.environ
q = lambda k: urllib.parse.quote(e[k], safe="")
print("postgresql://" + q("U") + ":" + q("P") + "@" + e["H"] + ":5432/" + q("D"))')
umask 077
printf 'REMNASHOP_DSN=%s\n' "$DSN" > /run/kago-sync/db.env
EOF
chmod 755 "$APP_DIR/resolve-db.sh"

# --------------------------------------------------------------------------- panel certificate

info "Проверка связи с панелью"
host_port=$(sed -E 's#^https?://([^/]+).*#\1#' <<<"$KAGO_URL")
[[ "$host_port" == *:* ]] || host_port="$host_port:443"
CA_LINE=""
if [[ "$KAGO_URL" == https://* ]] && ! curl -sS --max-time 10 -o /dev/null "$KAGO_URL" 2>/dev/null; then
    if curl -sSk --max-time 10 -o /dev/null "$KAGO_URL" 2>/dev/null; then
        sni=(); [[ "${host_port%:*}" =~ ^[0-9.]+$ ]] || sni=(-servername "${host_port%:*}")
        cert=$(openssl s_client -connect "$host_port" "${sni[@]}" </dev/null 2>/dev/null \
            | openssl x509 2>/dev/null || true)
        [[ -n "$cert" ]] || die "Не удалось получить сертификат панели с $host_port"
        fp=$(openssl x509 -noout -fingerprint -sha256 <<<"$cert" | cut -d= -f2)
        warn "У панели самоподписанный сертификат. Отпечаток SHA-256:"
        echo "      $fp"
        echo "  Сверьте его на сервере панели:"
        echo "      sudo openssl x509 -noout -fingerprint -sha256 -in /etc/kago/tls/selfsigned.crt"
        ok="${TRUST_CERT:-}"
        ask ok "Отпечатки совпадают, доверять этому сертификату? (y/n)" "y"
        [[ "$ok" =~ ^[YyДд] ]] || die "Отменено"
        printf '%s\n' "$cert" > "$CA_FILE"
        chmod 644 "$CA_FILE"
        CA_LINE="KAGO_CA_FILE=$CA_FILE"
    else
        die "Панель $KAGO_URL недоступна с этого сервера (проверьте адрес и файрвол)"
    fi
elif [[ -f "$CA_FILE" && -n "$(env_get KAGO_CA_FILE)" ]]; then
    CA_LINE="KAGO_CA_FILE=$CA_FILE"
fi

code=$(curl -sS --max-time 10 ${CA_LINE:+--cacert "$CA_FILE"} -o /dev/null -w '%{http_code}' \
    -X POST "$KAGO_URL" -H "Authorization: Bearer $KAGO_TOKEN" -H 'Content-Type: application/json' \
    -d '[{"id":"kago-sync-check","type":"sync.check","ts":"'"$(date -u +%FT%T)"'+00:00","data":{}}]' || true)
[[ "$code" == "200" ]] || die "Панель не приняла токен (HTTP $code). Проверьте KAGO_TOKEN и KAGO_URL."

# --------------------------------------------------------------------------- config & service

umask 077
{
    echo "# kago-sync settings. After editing: systemctl restart $SERVICE"
    echo "KAGO_URL=$KAGO_URL"
    echo "KAGO_TOKEN=$KAGO_TOKEN"
    [[ -n "$CA_LINE" ]] && echo "$CA_LINE"
    if [[ -n "$DB_CONTAINER" ]]; then
        echo "DB_CONTAINER=$DB_CONTAINER"
    else
        echo "REMNASHOP_DSN='$REMNASHOP_DSN'"
    fi
    if [[ -n "$REMNAWAVE_URL" ]]; then
        echo "REMNAWAVE_URL=$REMNAWAVE_URL"
        echo "REMNAWAVE_TOKEN=$REMNAWAVE_TOKEN"
    fi
} > "$ENV_FILE"
umask 022

PRE=""
if [[ -n "$DB_CONTAINER" ]]; then
    PRE="ExecStartPre=+$APP_DIR/resolve-db.sh
EnvironmentFile=-/run/$SERVICE/db.env"
fi

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=KAGO: send Remnashop registrations and purchases to the panel
After=network-online.target docker.service
Wants=network-online.target

[Service]
DynamicUser=yes
StateDirectory=$SERVICE
RuntimeDirectory=$SERVICE
EnvironmentFile=$ENV_FILE
$PRE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/remnashop_sync.py --watch $SYNC_SECONDS --state /var/lib/$SERVICE/state.json
Restart=always
RestartSec=30

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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" -q
systemctl restart "$SERVICE"

info "Первая синхронизация (вся история, может занять минуту)"
for _ in $(seq 1 60); do
    journalctl -u "$SERVICE" --since "-2min" --no-pager -q | grep -q "sent .* events" && break
    systemctl is-active -q "$SERVICE" || break
    sleep 2
done
if journalctl -u "$SERVICE" --since "-2min" --no-pager -q | grep -q "sent .* events"; then
    journalctl -u "$SERVICE" --since "-2min" --no-pager -q | grep "sent .* events" | tail -1
    echo
    echo "${c_bold}Готово.${c_off} Регистрации и покупки уходят в панель каждые $SYNC_SECONDS с."
    if [[ -n "$REMNAWAVE_URL" ]]; then
        echo "Автовыдача подключена: включите её в боте → «Правила начисления» → «Выдача: через kago-sync»."
    else
        echo "Автовыдача не настроена (нет REMNAWAVE_URL) — дни выдаются вручную."
    fi
else
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "Синхронизация не запустилась, смотрите журнал выше"
fi
cat <<EOF

  Настройки:  $ENV_FILE
  Логи:       journalctl -u $SERVICE -f
  Обновление: sudo bash $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install_remnashop_sync.sh
EOF
