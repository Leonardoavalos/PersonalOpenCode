#!/usr/bin/env bash
# Install the bridge as a systemd user service that starts at boot and
# restarts itself after a crash.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="$HOME/.config/systemd/user/opencode-telegram.service"

if [ ! -f "$PROJECT/.env" ]; then
  echo "missing $PROJECT/.env; run: cp env.example .env" >&2
  exit 1
fi

if ! command -v systemctl > /dev/null; then
  echo "systemctl not found; this installer needs systemd" >&2
  exit 1
fi

mkdir -p "$HOME/.config/systemd/user"
sed "s|%PROJECT%|$PROJECT|g" "$PROJECT/deploy/opencode-telegram.service" > "$UNIT"

# Linger keeps user services alive with no login session open. Without it the
# bridge dies the moment the last terminal closes.
if ! loginctl show-user "$USER" -p Linger 2> /dev/null | grep -q "Linger=yes"; then
  loginctl enable-linger "$USER"
  echo "enabled linger for $USER"
fi

systemctl --user daemon-reload
systemctl --user enable --now opencode-telegram.service

sleep 3
systemctl --user is-active opencode-telegram.service
echo
echo "unit:    $UNIT"
echo "logs:    journalctl --user -u opencode-telegram -f"
echo "status:  systemctl --user status opencode-telegram"
echo "stop:    systemctl --user disable --now opencode-telegram"
