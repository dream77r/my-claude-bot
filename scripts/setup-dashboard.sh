#!/usr/bin/env bash
# scripts/setup-dashboard.sh — privileged helper для one-click настройки
# дэшборда через Telegram (вызывается из /setup_dashboard).
#
# Запускается под sudo (через whitelist в /etc/sudoers.d/my-claude-bot).
# Строго валидирует вход: domain, port, email. Пишет nginx config,
# симлинкует, reload, выпускает HTTPS-cert через certbot --nginx.
#
# Использование:
#   sudo scripts/setup-dashboard.sh <domain> <port> [email]
#
# Exit codes:
#   0  — успех
#   2  — невалидный вход
#   3  — отсутствуют nginx/certbot
#   4  — DNS/cert failure
#   5  — ошибка nginx config
#   6  — ошибка certbot
set -euo pipefail

log()  { printf '[setup-dashboard] %s\n' "$*" >&2; }
fail() { log "ERROR: $*"; exit "${2:-1}"; }

# ── Аргументы ─────────────────────────────────────────────────────────────
if [[ $# -lt 2 ]]; then
    fail "usage: $0 <domain> <port> [email]" 2
fi

DOMAIN="$1"
PORT="$2"
EMAIL="${3:-noreply@${DOMAIN}}"

# ── Валидация входа ───────────────────────────────────────────────────────
# Hostname: только [a-z0-9.-], 1..253 символа, не начинается/кончается дефисом.
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$ ]]; then
    fail "invalid domain: '$DOMAIN'" 2
fi
if (( ${#DOMAIN} > 253 )); then
    fail "domain too long" 2
fi

# Port: число 1024..65535.
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
    fail "invalid port: '$PORT'" 2
fi

# Email: простая проверка.
if ! [[ "$EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    fail "invalid email: '$EMAIL'" 2
fi

# ── Инструменты ───────────────────────────────────────────────────────────
command -v nginx   >/dev/null || fail "nginx not installed" 3
command -v certbot >/dev/null || fail "certbot not installed" 3

# ── Nginx config ──────────────────────────────────────────────────────────
SITES_AVAIL="/etc/nginx/sites-available"
SITES_EN="/etc/nginx/sites-enabled"
CONF_FILE="${SITES_AVAIL}/${DOMAIN}"

log "writing nginx config: $CONF_FILE"
cat > "$CONF_FILE" <<NGINX_CONF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    proxy_set_header Connection '';
    chunked_transfer_encoding on;
    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
    }
}
NGINX_CONF

ln -sf "$CONF_FILE" "${SITES_EN}/${DOMAIN}"

log "nginx -t"
nginx -t 2>&1 | tail -4 || fail "nginx config invalid" 5

log "reloading nginx"
systemctl reload nginx || fail "nginx reload failed" 5

# ── Certbot (HTTPS) ───────────────────────────────────────────────────────
CERT_LIVE="/etc/letsencrypt/live/${DOMAIN}"
if [[ -d "$CERT_LIVE" ]]; then
    log "cert already exists for $DOMAIN, skipping certbot"
else
    log "requesting Let's Encrypt cert for $DOMAIN"
    certbot --nginx \
        -d "$DOMAIN" \
        --non-interactive \
        --agree-tos \
        --email "$EMAIL" \
        --redirect \
        --no-eff-email 2>&1 | tail -20 || fail "certbot failed" 6
fi

log "done"
echo "{\"status\":\"ok\",\"domain\":\"$DOMAIN\",\"port\":$PORT}"
