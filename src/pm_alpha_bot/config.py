from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://pm:pm@localhost:5432/pm_alpha_bot"

    poly_gamma_host: str = "https://gamma-api.polymarket.com"
    poly_data_host: str = "https://data-api.polymarket.com"
    poly_clob_host: str = "https://clob-v2.polymarket.com"
    poly_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    enable_live_trading: bool = False
    paper_initial_capital: float = 1000.0
    live_initial_capital: float = 1000.0
    paper_fee_bps: float = 10.0
    paper_slippage_bps: float = 5.0
    paper_fill_min_dwell_sec: float = 30.0
    paper_queue_miss_bps: float = 25.0
    replay_fee_bps: float = 10.0
    replay_slippage_bps: float = 5.0
    replay_fill_min_dwell_sec: float = 30.0
    replay_queue_miss_bps: float = 25.0

    poly_private_key: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""
    poly_address: str = ""
    poly_funder_address: str = ""
    poly_signature_type: int = 0
    poly_builder_code: str = ""

    min_wallet_consensus: int = 3
    min_edge_bps: int = 500
    max_spread_bps: int = 500
    max_market_exposure_pct: float = 0.03
    max_condition_exposure_pct: float = 0.03
    max_event_exposure_pct: float = 0.06
    max_category_exposure_pct: float = 0.15
    max_daily_loss_pct: float = 0.02
    max_total_drawdown_pct: float = 0.10
    kelly_fraction: float = 0.25
    signal_min_wallet_score: float = 0.35
    signal_activity_lookback_hours: int = 8
    signal_liquidity_gate_enabled: bool = Field(
        default=True,
        validation_alias="PM_ALPHA_SIGNAL_LIQUIDITY_GATE_ENABLED",
    )
    signal_liquidity_gate_window_minutes: int = Field(
        default=360,
        validation_alias="PM_ALPHA_SIGNAL_LIQUIDITY_GATE_WINDOW_MINUTES",
    )
    signal_liquidity_gate_min_category_samples: int = Field(
        default=20,
        validation_alias="PM_ALPHA_SIGNAL_LIQUIDITY_GATE_MIN_CATEGORY_SAMPLES",
    )
    liquidity_wallet_discovery_enabled: bool = Field(
        default=True,
        validation_alias="PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_ENABLED",
    )
    liquidity_wallet_discovery_trade_limit: int = Field(
        default=50,
        validation_alias="PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_TRADE_LIMIT",
    )
    liquidity_wallet_discovery_market_limit: int = Field(
        default=20,
        validation_alias="PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_MARKET_LIMIT",
    )
    liquidity_wallet_discovery_market_trade_limit: int = Field(
        default=25,
        validation_alias="PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_MARKET_TRADE_LIMIT",
    )
    liquidity_wallet_discovery_wallet_limit: int = Field(
        default=20,
        validation_alias="PM_ALPHA_LIQUIDITY_WALLET_DISCOVERY_WALLET_LIMIT",
    )
    market_category_filter: str = Field(
        default="",
        validation_alias="PM_ALPHA_MARKET_CATEGORY_FILTER",
    )
    observation_promotion_min_streak: int = 3
    observation_queue_reminder_every_streak: int = 3

    leaderboard_limit: int = 50
    tracked_wallet_limit: int = 100
    wallet_market_lookback_hours: int = 24
    wallet_market_enrichment_limit: int = 100
    tradeable_orderbook_focus: bool = Field(
        default=False,
        validation_alias="PM_ALPHA_TRADEABLE_ORDERBOOK_FOCUS",
    )
    tradeable_orderbook_focus_window_minutes: int = Field(
        default=360,
        validation_alias="PM_ALPHA_TRADEABLE_ORDERBOOK_FOCUS_WINDOW_MINUTES",
    )
    missing_orderbook_cache_ttl_hours: int = 6
    orderbook_poll_interval_sec: int = 15
    snapshot_stale_after_sec: int = 60
    live_heartbeat_interval_sec: float = 5.0
    live_order_sync_limit: int = 50
    live_max_consecutive_errors: int = 3
    live_cancel_all_on_kill_switch: bool = True
    runtime_event_retention_days: int = 30
    alert_webhook_url: str = ""
    alert_webhook_timeout_sec: float = 10.0
    http_timeout_sec: float = 20.0
    http_rate_limit_delay_sec: float = 0.0
    console_auth_username: str = Field(
        default="",
        validation_alias="PM_ALPHA_CONSOLE_AUTH_USERNAME",
    )
    console_auth_password: str = Field(
        default="",
        validation_alias="PM_ALPHA_CONSOLE_AUTH_PASSWORD",
    )
    console_auth_realm: str = Field(
        default="PM Alpha Bot Console",
        validation_alias="PM_ALPHA_CONSOLE_AUTH_REALM",
    )
    data_dir: Path = Field(default_factory=lambda: Path("."))

    @property
    def live_credentials_present(self) -> bool:
        """Return true when the required live credentials exist."""
        required = [
            self.poly_private_key,
            self.poly_api_key,
            self.poly_api_secret,
            self.poly_api_passphrase,
        ]
        return all(bool(value.strip()) for value in required)

    @property
    def is_sqlite(self) -> bool:
        """Return true when the database URL targets SQLite."""
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
