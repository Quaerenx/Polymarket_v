#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PM_ALPHA_BOT_DIR:-/root/PJ_AUTO_TRADE_BOT}"
LOCK_FILE="${PM_ALPHA_REFRESH_LOCK_FILE:-/tmp/pm-alpha-bot-refresh.lock}"
LEADERBOARD_CATEGORY="${PM_ALPHA_REFRESH_LEADERBOARD_CATEGORY:-OVERALL}"
LEADERBOARD_TIME_PERIOD="${PM_ALPHA_REFRESH_LEADERBOARD_TIME_PERIOD:-MONTH}"
MARKET_LIMIT="${PM_ALPHA_REFRESH_MARKET_LIMIT:-100}"
ACTIVE_ONLY="${PM_ALPHA_REFRESH_ACTIVE_ONLY:-true}"
MARKET_CATEGORY="${PM_ALPHA_REFRESH_MARKET_CATEGORY:-}"

cd "${APP_DIR}"

args=(
  ops
  refresh-data
  --leaderboard-category "${LEADERBOARD_CATEGORY}"
  --leaderboard-time-period "${LEADERBOARD_TIME_PERIOD}"
  --market-limit "${MARKET_LIMIT}"
)

if [[ -n "${PM_ALPHA_REFRESH_WALLET_LIMIT:-}" ]]; then
  args+=(--wallet-limit "${PM_ALPHA_REFRESH_WALLET_LIMIT}")
fi

if [[ -n "${PM_ALPHA_REFRESH_ORDERBOOK_LIMIT:-}" ]]; then
  args+=(--orderbook-limit "${PM_ALPHA_REFRESH_ORDERBOOK_LIMIT}")
fi

if [[ -n "${PM_ALPHA_REFRESH_WALLET_CONTEXT_LIMIT:-}" ]]; then
  args+=(--wallet-context-limit "${PM_ALPHA_REFRESH_WALLET_CONTEXT_LIMIT}")
fi

if [[ -n "${MARKET_CATEGORY}" ]]; then
  args+=(--market-category "${MARKET_CATEGORY}")
fi

if [[ "${ACTIVE_ONLY}" == "true" ]]; then
  args+=(--active-only)
else
  args+=(--all-markets)
fi

exec /usr/bin/flock -n "${LOCK_FILE}" "${APP_DIR}/.venv/bin/pm-bot" "${args[@]}"
