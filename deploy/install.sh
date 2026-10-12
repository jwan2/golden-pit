#!/usr/bin/env bash
# Golden Pit — one-shot server install (Debian / Ubuntu, run as root). Safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/jwan2/golden-pit/main/deploy/install.sh | bash
#   # or, with a domain served by Caddy (automatic HTTPS):
#   DOMAIN=stock.example.com bash install.sh
#
# What it does:
#   - creates a system user `goldenpit`, installs to /opt/golden-pit, data in /var/lib/golden-pit
#   - generates /etc/golden-pit.env with a random admin token (only on first run)
#   - runs gunicorn under systemd, by default on 127.0.0.1:5000 (not exposed)
#   - optional: Caddy reverse proxy for $DOMAIN
#   - BIND=0.0.0.0:5000 exposes it directly, e.g. when Cloudflare already proxies to the origin port
set -euo pipefail

REPO="${REPO:-https://github.com/jwan2/golden-pit.git}"
REF="${REF:-main}"
APP=/opt/golden-pit
DATA=/var/lib/golden-pit
ENV_FILE=/etc/golden-pit.env
DOMAIN="${DOMAIN:-}"
BIND="${BIND:-127.0.0.1:5000}"

[ "$(id -u)" = 0 ] || { echo "please run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo ">>> [1/5] system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates openssl >/dev/null

echo ">>> [2/5] user, code, venv"
id goldenpit >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin goldenpit
mkdir -p "$DATA" && chown goldenpit:goldenpit "$DATA" && chmod 750 "$DATA"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$REF" && git -C "$APP" checkout -q -f "origin/$REF"
else
  git clone -q --branch "$REF" "$REPO" "$APP"
fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$APP[server]"

echo ">>> [3/5] config"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
GOLDEN_PIT_DATA=$DATA
GOLDEN_PIT_ADMIN_TOKEN=$(openssl rand -hex 24)
GOLDEN_PIT_SCHEDULER=1
GOLDEN_PIT_SLOTS=08:30,16:15
GOLDEN_PIT_WORKERS=4
GOLDEN_PIT_AI_PROVIDER=none
EOF
  echo "    created $ENV_FILE (admin token inside — keep it private)"
fi
chown root:goldenpit "$ENV_FILE" && chmod 640 "$ENV_FILE"

echo ">>> [4/5] systemd"
cat > /etc/systemd/system/golden-pit.service <<EOF
[Unit]
Description=Golden Pit stock assistant
After=network-online.target
Wants=network-online.target

[Service]
User=goldenpit
Group=goldenpit
WorkingDirectory=$DATA
EnvironmentFile=$ENV_FILE
# scan state and scheduler live in-process: exactly ONE worker
ExecStart=$APP/venv/bin/gunicorn -w 1 --threads 8 --timeout 120 -b $BIND "golden_pit.server:create_app_from_env()"
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q golden-pit
systemctl restart golden-pit

if [ -n "$DOMAIN" ]; then
  echo ">>> [5/5] caddy for $DOMAIN"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:5000
}
EOF
  systemctl enable -q caddy && systemctl reload caddy 2>/dev/null || systemctl restart caddy
else
  echo ">>> [5/5] no DOMAIN given — service listens on $BIND; put HTTPS (reverse proxy / Cloudflare) in front"
fi

sleep 3
curl -fsS http://127.0.0.1:5000/api/health && echo "  <- golden-pit is up"
