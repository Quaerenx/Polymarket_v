#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PM_ALPHA_BOT_DIR:-/root/PJ_AUTO_TRADE_BOT}"
HOST="${PM_ALPHA_CONSOLE_HOST:-$(tailscale ip -4 | head -n 1)}"
PORT="${PM_ALPHA_CONSOLE_PORT:-8787}"
ENABLE_ACTIONS="${PM_ALPHA_CONSOLE_ENABLE_ACTIONS:-false}"
ALLOW_REMOTE_ACTIONS="${PM_ALPHA_CONSOLE_ALLOW_REMOTE_ACTIONS:-false}"

if [[ -z "${HOST}" ]]; then
  echo "Unable to determine a console host. Set PM_ALPHA_CONSOLE_HOST explicitly." >&2
  exit 1
fi

cd "${APP_DIR}"

args=(
  ops
  serve-console
  --host "${HOST}"
  --port "${PORT}"
)

if [[ "${ENABLE_ACTIONS}" == "true" ]]; then
  args+=(--enable-actions)
else
  args+=(--read-only)
fi

if [[ "${ALLOW_REMOTE_ACTIONS}" == "true" ]]; then
  args+=(--allow-remote-actions)
fi

exec "${APP_DIR}/.venv/bin/pm-bot" "${args[@]}"
