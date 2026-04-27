# Polymarket Wallet-Weighted EV Bot MVP

Paper-first Polymarket MVP that ingests public market and wallet data, scores wallets, generates signals, and simulates maker-first execution without placing live orders by default.

## Scope

- Phase 1-5 implemented as the primary MVP path:
  - project skeleton and CLI
  - Postgres/SQLite persistence
  - public data ingestion
  - wallet scoring
  - signal generation
  - paper trading
- Phase 6-7 are implemented with safety gates:
  - WebSocket streaming and replay backtests are available
  - live trading remains disabled by default and requires explicit acknowledgement

## Safety

- Default mode is paper trading.
- Live trading requires both `ENABLE_LIVE_TRADING=true` and an explicit live command.
- Missing credentials, stale orderbook data, geoblock failure, insufficient balance/allowance, and risk-limit violations block live orders.
- Read-only sync of existing live orders does not require `ENABLE_LIVE_TRADING=true`, but it does require valid CLOB credentials.
- Resting live orders require heartbeats; use `pm-bot trade live-loop --live` to keep heartbeats and reconciliation running.
- The live supervisor defaults to reconciliation-only mode. New live orders are submitted only when `--submit-new-orders` is explicitly enabled.
- The live supervisor now has a kill switch for consecutive live-loop failures. Remote `cancel-all` on kill switch is enabled by default with `LIVE_CANCEL_ALL_ON_KILL_SWITCH=true`.
- Live supervisor and kill-switch state are persisted in the database so operators can inspect or reset state after a restart.
- Live operational events are stored as an append-only audit trail in the database for recent submit/cancel/sync/supervisor/kill-switch actions.
- Optional webhook alerts can be sent on kill-switch trigger and reset by setting `ALERT_WEBHOOK_URL`.
- A local harness-style monitoring console is available via `pm-bot ops serve-console`.
- The web console is read-only by default, binds to `127.0.0.1` by default, and only enables management actions when `--enable-actions` is explicitly passed.
- The console supports optional HTTP Basic Auth via `PM_ALPHA_CONSOLE_AUTH_USERNAME` and `PM_ALPHA_CONSOLE_AUTH_PASSWORD`.
- This project does not guarantee profitability.

## Why not blindly copy 70% win-rate wallets?

High win rate alone is not sufficient. Wallets can overfit thin markets, concentrate profits in a few trades, or exhibit poor replicability due to slippage and timing. This MVP scores wallets on broader quality signals such as ROI, PnL, recent performance, CLV, drawdown, and profit concentration.

## Point-In-Time Data

Leaderboard ingest stores timestamped rows in `wallet_leaderboard_snapshots`, and wallet detail refresh stores timestamped `closed_positions`, `trades`, and `total_value` payloads in `wallet_detail_snapshots`. Wallet scoring uses the latest snapshot at or before its `as_of` timestamp when available, then falls back to the latest wallet JSON only for pre-history data.

## Requirements

- Python 3.12
- `uv`
- Docker (for local Postgres)

## Setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
```

Optional live SDK:

```bash
uv pip install -e ".[live]"
```

## Local Postgres

```bash
docker compose up -d postgres
```

## Database

```bash
pm-bot db init
pm-bot db migrate
pm-bot db prune-runtime-events --older-than-days 30
pm-bot db prune-runtime-events --older-than-days 30 --apply
pm-bot ops healthcheck
pm-bot ops healthcheck --require-live-supervisor --fail-on-kill-switch --fail-on-supervisor-error
pm-bot ops refresh-data
pm-bot ops serve-console
pm-bot ops serve-console --enable-actions
```

## Core Commands

```bash
pm-bot ingest markets --limit 20 --active-only
pm-bot ingest leaderboard --category OVERALL --time-period MONTH --limit 20
pm-bot ingest wallets --limit 20
pm-bot ingest orderbook --limit 20
pm-bot stream market --tokens-from-db --limit 20 --max-messages 100
pm-bot score wallets --category OVERALL
pm-bot scan signals --category OVERALL
pm-bot trade paper --once
pm-bot backtest replay --from 2026-04-01 --to 2026-04-22 --category OVERALL
pm-bot trade sync-live --limit 50
pm-bot trade live-loop --live --iterations 3
pm-bot trade reset-live-kill-switch --acknowledge --reason reviewed_cause
pm-bot report wallets --top 20
pm-bot report signals --top 20
pm-bot report paper
pm-bot report live
pm-bot report live-health
pm-bot report live-events --top 50
pm-bot report signal-diagnostics --category OVERALL --top-candidates 10
pm-bot report signal-diagnostics --category OVERALL --market-category Sports --top-candidates 10
pm-bot ops refresh-data --market-limit 100 --leaderboard-category OVERALL --leaderboard-time-period MONTH
pm-bot ops refresh-data --market-limit 100 --wallet-context-limit 150
pm-bot ops refresh-data --market-category Sports
pm-bot ops serve-console --host 127.0.0.1 --port 8787
```

Explicit live acknowledgement is required even on the live command:

```bash
ENABLE_LIVE_TRADING=true pm-bot trade live --live
ENABLE_LIVE_TRADING=true pm-bot trade live-loop --live --submit-new-orders
pm-bot trade cancel-live --order-id <exchange-order-id> --live
```

## Environment

See `.env.example` for the full variable set. The host URLs are overrideable because Polymarket may change APIs or hosts over time.

`LIVE_INITIAL_CAPITAL` controls live risk sizing and exposure limits. It should reflect the capital you want the bot to treat as available for live trading.

`LIVE_MAX_CONSECUTIVE_ERRORS` controls how many consecutive supervisor failures are tolerated before the live kill switch trips.

`PAPER_FILL_MIN_DWELL_SEC`, `PAPER_QUEUE_MISS_BPS`, `REPLAY_FILL_MIN_DWELL_SEC`, and `REPLAY_QUEUE_MISS_BPS` make maker-fill simulation more conservative by requiring orders to rest before fill and requiring the book to move through the limit price.

`LIVE_CANCEL_ALL_ON_KILL_SWITCH` defaults to `true`, so a supervisor kill-switch trip attempts to cancel all remote live orders and mirrors cancelled orders locally. Resetting the kill switch requires explicit operator acknowledgement and is blocked while local active live orders remain unless `--allow-active-orders` is passed.

`MAX_MARKET_EXPOSURE_PCT`, `MAX_CONDITION_EXPOSURE_PCT`, `MAX_EVENT_EXPOSURE_PCT`, and `MAX_CATEGORY_EXPOSURE_PCT` cap exposure by token, condition, event, and category respectively. Condition defaults to the token cap (`3%`) and event defaults to `6%`.

`MIN_WALLET_CONSENSUS` defaults to `3`, so tradeable signals require at least three eligible wallets agreeing on the same token direction. This is the conservative fallback because the public wallet data does not reliably prove wallet ownership independence.

`SIGNAL_MIN_WALLET_SCORE` and `SIGNAL_ACTIVITY_LOOKBACK_HOURS` control how aggressively the signal engine converts recent wallet activity into candidate signals. The default activity lookback is `8` hours; older activity receives no recency weight even if a longer lookback is configured.

`PM_ALPHA_SIGNAL_LIQUIDITY_GATE_ENABLED` defaults to `true`. When a market category has enough recent orderbook samples but none have usable bid/ask liquidity, signal enrichment skips that category until fresh tradeable orderbooks reappear. `PM_ALPHA_SIGNAL_LIQUIDITY_GATE_WINDOW_MINUTES` and `PM_ALPHA_SIGNAL_LIQUIDITY_GATE_MIN_CATEGORY_SAMPLES` tune the lookback window and minimum sample size.

`PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_ENABLED` defaults to `true`. During refresh, the bot samples recent public trades and also fetches trades from markets with a recently usable orderbook, then adds wallets from those liquid trades to the local tracking universe. `PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_TRADE_LIMIT`, `PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_MARKET_LIMIT`, `PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_MARKET_TRADE_LIMIT`, and `PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_WALLET_LIMIT` cap the amount of public trade data, liquid markets, per-market trades, and new wallets processed per cycle.

`OBSERVATION_PROMOTION_MIN_STREAK` controls how many consecutive refresh cycles a promotion-ready empty-book candidate must survive before it enters the explicit promotion queue.

`OBSERVATION_QUEUE_REMINDER_EVERY_STREAK` controls how often a queued observation candidate triggers a reminder alert after entering the promotion queue. A value of `3` means reminders at streaks `6`, `9`, `12`, and so on when the queue threshold is `3`.

`WALLET_MARKET_LOOKBACK_HOURS` and `WALLET_MARKET_ENRICHMENT_LIMIT` control the wallet-context enrichment pass that backfills missing market metadata and recent wallet token orderbooks before signal scanning.

`MISSING_ORDERBOOK_CACHE_TTL_HOURS` controls how long 404 CLOB orderbooks are skipped before the bot retries them during refresh and ingest.

`PM_ALPHA_REFRESH_WALLET_CONTEXT_LIMIT` can override the wallet-context enrichment limit for the bundled refresh script without changing the default app setting.

`RUNTIME_EVENT_RETENTION_DAYS` sets the default retention window used by `pm-bot db prune-runtime-events` when `--older-than-days` is omitted.

`ALERT_WEBHOOK_URL` enables best-effort JSON webhook alerts for live kill-switch trigger/reset events and promotion-queue observation transitions such as `signal_observation_queued`, `signal_observation_queue_reminder`, and `signal_observation_cleared`. Observation queue alerts include `review_commands` in their JSON payload so operators can immediately run the relevant CLI checks.

## Web Console

The monitoring console follows the internal dark-console harness and is intended for operators, not public users.

```bash
pm-bot ops serve-console
```

Open `http://127.0.0.1:8787/` in a browser to view:

- current mode and health summary
- live supervisor and kill-switch status
- latest signals and top wallets
- promotion-ready observation candidates with score and last-trade context
- observation streaks that help separate one-off empty-book candidates from persistent ones
- a separate promotion queue for observation candidates that survive the configured streak threshold
- signal observation runtime events in the web console for ready / queued / cleared transitions
- open live orders
- recent runtime events
- live and paper position details

Read-only mode is the default. To enable restricted mutations from the browser:

```bash
pm-bot ops serve-console --enable-actions
```

When actions are enabled, the console currently supports:

- resetting the persisted live kill switch
- syncing remote live orders/fills
- previewing or applying runtime event pruning

For safety, non-loopback action binds are rejected unless `--allow-remote-actions` is explicitly set as well.
Remote action binds also require console HTTP Basic Auth to be configured.

To require browser authentication for all console requests, set:

```bash
export PM_ALPHA_CONSOLE_AUTH_USERNAME=operator
export PM_ALPHA_CONSOLE_AUTH_PASSWORD='choose-a-strong-password'
pm-bot ops serve-console --host 127.0.0.1 --port 8787
```

For Tailscale or any non-loopback bind, enabling HTTP Basic Auth is recommended even in read-only mode.

## Refresh Automation

The manual refresh command is:

```bash
pm-bot ops refresh-data
pm-bot ops refresh-data --wallet-context-limit 150
```

It runs this sequence with separate DB transactions per stage:

- ingest active markets
- ingest leaderboard wallets
- refresh tracked wallet positions and activity
- enrich missing markets and orderbooks from recent tracked-wallet activity
- ingest orderbook snapshots
- score wallets
- backfill missing signal-input markets and orderbooks for currently eligible wallets
- scan and persist signals
- update the observation watchlist for promotion-ready empty-book candidates and append runtime events on ready/persistent/cleared transitions
- move observation candidates into an explicit promotion queue once they survive the configured streak threshold

For periodic automation, use:

- `deploy/systemd/pm-alpha-bot-refresh.service`
- `deploy/systemd/pm-alpha-bot-refresh.timer`
- `deploy/systemd/refresh.env.example`

The bundled `scripts/run_refresh_cycle.sh` uses `flock` to prevent overlapping refresh runs.
It also benefits from a runtime-state cache that temporarily skips token IDs whose public `/book` endpoint recently returned `404`.
When no `--market-category` filter is set, orderbook refresh samples active tokens across categories in round-robin order so discovery is not dominated by one stale market group. Signal-context enrichment also refetches candidate token orderbooks when the latest stored snapshot is stale or empty.

## Signal Diagnostics

Use the diagnostics report to understand why the current dataset is not generating tradeable signals:

```bash
pm-bot report signal-diagnostics --category OVERALL --top-candidates 10
pm-bot report signal-diagnostics --category OVERALL --market-category Sports --top-candidates 10
pm-bot report signal-observations --category OVERALL --score-threshold 0.30 --consensus 1
pm-bot report signal-observations --category OVERALL --token-id <token-id> --score-threshold 0.30 --consensus 1
pm-bot report signal-observations --category OVERALL --market-category Sports --score-threshold 0.30 --consensus 1
```

The report summarizes:

- latest wallet-score coverage and score distribution
- current eligible-wallet count at the configured threshold
- top blocking reasons such as missing markets, missing snapshots, insufficient consensus, or edge below threshold
- `market_category_no_tradeable_orderbook` when recent category-level liquidity is too poor to spend signal slots there
- recent `404`-cached missing snapshots, `empty_orderbook`, and `empty_orderbook_with_last_trade` observation-only candidates are reported separately
- top candidate tokens with spread, edge, and snapshot-age context
- observation-only candidates are shown in a separate table with wallet-direction strength and last-trade context
- observation reports now include an `observation_streak` so operators can distinguish transient candidates from persistent ones across refresh cycles
- a dedicated promotion-queue table shows only observation candidates that passed `OBSERVATION_PROMOTION_MIN_STREAK`
- sensitivity across alternate score thresholds and consensus settings

Use `pm-bot report signal-observations` when you want to inspect only observation-only empty-book candidates under a custom wallet-score threshold and consensus setting. The report includes an `observation score` and a `promotion ready` flag that indicates whether the candidate would satisfy the observation gate if an orderbook reappears.

Both `pm-bot report signal-diagnostics` and `pm-bot report signal-observations` support `--token-id` to narrow the analysis to a single token when reviewing alert payloads. They also support `--market-category` to focus the pipeline on one market group such as `Sports`.

Use `pm-bot report live-events --category signal` to inspect persisted signal-observation runtime events such as `signal_observation_ready`, `signal_observation_persistent`, `signal_observation_queued`, and `signal_observation_cleared`.

## Deployment Templates

- `deploy/systemd/pm-alpha-bot-live.service` runs the live supervisor under a local virtualenv.
- `deploy/systemd/pm-alpha-bot-console.service` runs the monitoring console as a persistent `systemd` service.
- `deploy/systemd/pm-alpha-bot-refresh.service` and `deploy/systemd/pm-alpha-bot-refresh.timer` run the ingest/score/scan refresh cycle on a schedule.
- `deploy/systemd/pm-alpha-bot-healthcheck.service` and `deploy/systemd/pm-alpha-bot-healthcheck.timer` run `pm-bot ops healthcheck` every 30 seconds.
- `deploy/docker-compose.live.yml` provides a bind-mounted Docker Compose example with the same healthcheck command wired into container health status.
- `pm-bot ops serve-console` can be run alongside those templates for local browser monitoring; keep it on loopback unless you add an authenticated reverse proxy yourself.

For a persistent Tailscale-facing console, copy `deploy/systemd/pm-alpha-bot-console.service` to `/etc/systemd/system/`, copy `deploy/systemd/console.env.example` to `/etc/pm-alpha-bot/console.env`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pm-alpha-bot-console.service
```

The bundled `scripts/start_console.sh` resolves the current Tailscale IPv4 address automatically when `PM_ALPHA_CONSOLE_HOST` is not set.

For periodic refresh automation, copy `deploy/systemd/pm-alpha-bot-refresh.service` and `deploy/systemd/pm-alpha-bot-refresh.timer` into `/etc/systemd/system/`, copy `deploy/systemd/refresh.env.example` to `/etc/pm-alpha-bot/refresh.env`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pm-alpha-bot-refresh.timer
```

`pm-bot ops healthcheck` supports DB-only mode by default. For live deployments, use `--require-live-supervisor --fail-on-kill-switch --fail-on-supervisor-error` so stale supervisor state, kill switch activation, and persisted error status fail health checks with a non-zero exit code.

## Known limitations

- CLV is an approximation based on stored snapshots rather than full historical orderbook replay.
- Paper fills are conservative and may understate fills.
- Live accounting is reconstructed from synced orders and fills; if the local database misses historical fills, `report live` and live risk limits will be incomplete until a full sync is performed.
- Balance/allowance checks use the authenticated CLOB account view. They can catch obvious insufficiency before submission, but they still depend on Polymarket’s remote balance cache and the account’s current allowance state.
- The persisted kill switch blocks new live submissions until it is manually reset with `pm-bot trade reset-live-kill-switch --acknowledge --reason reviewed_cause`. Reconciliation-only `live-loop` runs can still be used to inspect and heartbeat existing orders.
- The runtime event log is append-only and currently intended for operational inspection, not long-term archival or analytics.
- Runtime event pruning is manual and defaults to dry-run so operators can review the impact before deletion.
- The provided systemd and Docker templates are examples. They do not install the app, provision secrets, or wire automatic remediation beyond process restart/health probing.
- Replay backtests only use data already stored in the database. If historical `wallet_scores` do not exist before the replay window, no signals will be generated for those timestamps.
- Public APIs can evolve; normalization adapters are intentionally explicit to reduce breakage.
- Wallet metrics with unavailable inputs are neutral-scored and recorded as unavailable in raw metrics.

## Next steps

- Expand live fill/position reconciliation beyond initial order sync.
- Expand market microstructure features and richer liquidity penalties.
