#!/usr/bin/env bash
# install.sh — idempotent setup for race-worker on Ubuntu 24.04
# Run as root. Does NOT touch other services or reload Caddy.
set -euo pipefail

APP_DIR="/opt/race-worker"
DATA_DIR="${APP_DIR}/data"
CONF_DIR="/etc/race-worker"
SVC_USER="raceapi"
PYTHON="python3.12"

echo "=== race-worker install ==="

# 1. System user
if ! id "$SVC_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$SVC_USER"
    echo "Created user $SVC_USER"
else
    echo "User $SVC_USER already exists"
fi

# 2. Directories
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"

# 3. Copy application code
cp -r app/ "$APP_DIR/"
cp requirements.txt "$APP_DIR/"

# 4. Virtual environment
if [ ! -d "${APP_DIR}/venv" ]; then
    $PYTHON -m venv "${APP_DIR}/venv"
    echo "Created venv"
else
    echo "venv already exists"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
echo "Dependencies installed"

# 5. Env file (only create if missing)
if [ ! -f "${CONF_DIR}/env" ]; then
    cp deploy/env.example "${CONF_DIR}/env"
    chmod 640 "${CONF_DIR}/env"
    chown root:${SVC_USER} "${CONF_DIR}/env"
    echo "Created ${CONF_DIR}/env from template — edit tokens before starting!"
else
    echo "${CONF_DIR}/env already exists, not overwriting"
fi

# 6. Systemd unit
cp deploy/race-worker.service /etc/systemd/system/race-worker.service
systemctl daemon-reload
echo "Systemd unit installed"

# 7. Permissions
chown -R ${SVC_USER}:${SVC_USER} "$APP_DIR"
chmod 750 "$DATA_DIR"
echo "Permissions set"

echo ""
echo "=== Done ==="
echo "Next steps:"
echo "  1. Edit /etc/race-worker/env — set real tokens"
echo "  2. Add Caddyfile snippet (see deploy/Caddyfile.snippet)"
echo "  3. sudo systemctl enable --now race-worker"
echo "  4. sudo systemctl status race-worker"
