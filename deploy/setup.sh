#!/usr/bin/env bash
# One-time server setup for smuHBLogs bot.
# Run as root on a fresh Ubuntu droplet, from inside the uploaded project dir:
#   sudo bash deploy/setup.sh
set -euo pipefail

APP_DIR=/opt/smuhblogs
APP_USER=smuhb
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

echo "==> Creating service user '${APP_USER}' (if missing)"
id -u "${APP_USER}" &>/dev/null || useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"

echo "==> Copying project to ${APP_DIR}"
mkdir -p "${APP_DIR}/data"
cp "${SRC_DIR}"/bot.py "${SRC_DIR}"/database.py "${SRC_DIR}"/sheets.py "${SRC_DIR}"/requirements.txt "${APP_DIR}/"
# Secrets are uploaded separately; copy them if present alongside the code.
[ -f "${SRC_DIR}/.env" ] && cp "${SRC_DIR}/.env" "${APP_DIR}/.env"
[ -f "${SRC_DIR}/service_account.json" ] && cp "${SRC_DIR}/service_account.json" "${APP_DIR}/service_account.json"
# Preserve existing data if a DB was uploaded with the code.
[ -f "${SRC_DIR}/hblogs.db" ] && [ ! -f "${APP_DIR}/data/hblogs.db" ] && cp "${SRC_DIR}/hblogs.db" "${APP_DIR}/data/hblogs.db"

echo "==> Creating virtualenv and installing dependencies"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo "==> Setting ownership and permissions"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
chmod 600 "${APP_DIR}/service_account.json" 2>/dev/null || true

echo "==> Installing systemd service"
cp "${SRC_DIR}/deploy/smuhblogs.service" /etc/systemd/system/smuhblogs.service
systemctl daemon-reload
systemctl enable --now smuhblogs

echo "==> Done. Recent logs:"
sleep 3
systemctl status smuhblogs --no-pager -l | head -20
