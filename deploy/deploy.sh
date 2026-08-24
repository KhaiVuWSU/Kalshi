#!/usr/bin/env bash
# Deploy / update the Kalshi Edge Scanner on a Debian/Ubuntu VPS.
# Run ON the VPS with sudo:  sudo bash deploy/deploy.sh
# Idempotent: safe to re-run for updates (git pull + pip sync + restart).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/KhaiVuWSU/Kalshi.git}"
BRANCH="${BRANCH:-claude/kalshi-edge-scanner-spec-ll5h6y}"
APP_DIR="${APP_DIR:-/opt/kalshi-scanner}"
RUN_USER="${RUN_USER:-kalshi}"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/deploy.sh" >&2
    exit 1
fi

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
python3 - <<'EOF'
import sys
assert sys.version_info >= (3, 11), f"Python 3.11+ required, found {sys.version}"
EOF
echo "    python3 ${PYVER} OK"

echo "==> Service user"
id -u "$RUN_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"

echo "==> Code -> ${APP_DIR} (branch ${BRANCH})"
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch origin "$BRANCH"
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Virtualenv + dependencies"
[[ -d "$APP_DIR/.venv" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet httpx websockets pydantic PyYAML cryptography pytest pytest-asyncio

echo "==> Directories, .env, key placement"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/reports" "$APP_DIR/keys"
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "    Created ${APP_DIR}/.env — YOU MUST EDIT IT before starting."
fi
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
chmod 700 "$APP_DIR/keys"
chmod 600 "$APP_DIR/.env"
find "$APP_DIR/keys" -name '*.pem' -exec chmod 600 {} \; 2>/dev/null || true

echo "==> Tests (offline; should all pass)"
sudo -u "$RUN_USER" bash -c "cd '$APP_DIR' && .venv/bin/python -m pytest -q" \
    || { echo "Tests failed — not installing the service."; exit 1; }

echo "==> systemd unit"
cp "$APP_DIR/deploy/kalshi-scanner.service" /etc/systemd/system/kalshi-scanner.service
systemctl daemon-reload
systemctl enable kalshi-scanner >/dev/null

cat <<EOF

Deployed to ${APP_DIR} (service installed but NOT started).

Next — the first-run checklist:
  1. Put your demo RSA key at ${APP_DIR}/keys/kalshi-demo.pem (chmod 600)
     and edit ${APP_DIR}/.env
  2. sudo -u ${RUN_USER} bash -c 'cd ${APP_DIR} && .venv/bin/python -m src.main verify-auth'
  3. sudo -u ${RUN_USER} bash -c 'cd ${APP_DIR} && .venv/bin/python -m src.main sync'   (run twice)
  4. sudo -u ${RUN_USER} bash -c 'cd ${APP_DIR} && .venv/bin/python -m src.main scan-once'
  5. sudo systemctl start kalshi-scanner && journalctl -u kalshi-scanner -f
See README.md and NOTES.md for details.
EOF
