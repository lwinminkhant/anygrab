#!/usr/bin/env bash
#
# AnyGrab VPS Setup Script
# Tested on: Ubuntu 22.04 / Debian 12
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/YOUR_USER/anygrab/main/deploy/setup.sh | bash
#   — or —
#   chmod +x deploy/setup.sh && sudo ./deploy/setup.sh
#
set -euo pipefail

APP_DIR="/opt/anygrab"
APP_USER="anygrab"
REPO_URL="${REPO_URL:-https://github.com/YOUR_USER/anygrab.git}"
BRANCH="${BRANCH:-main}"

echo "=============================="
echo "  AnyGrab VPS Setup"
echo "=============================="

# ── System dependencies ──────────────────────────────────────────────────────

echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    ffmpeg git curl

# ── App user ─────────────────────────────────────────────────────────────────

echo "[2/7] Creating app user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

# ── Clone / update repo ─────────────────────────────────────────────────────

echo "[3/7] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# ── Python venv + deps ──────────────────────────────────────────────────────

echo "[4/7] Setting up Python environment..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# ── Environment file ────────────────────────────────────────────────────────

echo "[5/7] Checking .env..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo ""
    echo "  ⚠️  IMPORTANT: Edit /opt/anygrab/.env and add your tokens!"
    echo "     nano /opt/anygrab/.env"
    echo ""
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Systemd services ────────────────────────────────────────────────────────

echo "[6/7] Installing systemd services..."
cp "$APP_DIR/deploy/anygrab-api.service" /etc/systemd/system/
cp "$APP_DIR/deploy/anygrab-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable anygrab-api anygrab-bot

# ── Start ────────────────────────────────────────────────────────────────────

echo "[7/7] Starting services..."
systemctl restart anygrab-api
sleep 2
systemctl restart anygrab-bot

echo ""
echo "=============================="
echo "  ✅ AnyGrab deployed!"
echo "=============================="
echo ""
echo "  API:  http://$(hostname -I | awk '{print $1}'):8000"
echo "  Bot:  Running as systemd service"
echo ""
echo "  Commands:"
echo "    sudo systemctl status anygrab-api anygrab-bot"
echo "    sudo journalctl -u anygrab-api -f"
echo "    sudo journalctl -u anygrab-bot -f"
echo ""
echo "  Config: /opt/anygrab/.env"
echo "  Update: cd /opt/anygrab && git pull && sudo systemctl restart anygrab-api anygrab-bot"
echo ""
