from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pm_alpha_bot.db.models import (
    Fill,
    Market,
    MarketSnapshot,
    MarketToken,
    Order,
    PaperPosition,
    RuntimeEvent,
    RuntimeState,
    Signal,
    Wallet,
    WalletActivity,
    WalletDetailSnapshot,
    WalletLeaderboardSnapshot,
    WalletPosition,
    WalletScore,
    utcnow,
)
from pm_alpha_bot.domain import (
    LeaderboardEntry,
    LiveOpenOrder,
    NormalizedMarket,
    OrderBookSnapshot,
    PaperOrderRequest,
    PreparedLiveOrder,
    RiskState,
    SignalRecord,
    WalletActivityRecord,
    WalletPositionRecord,
    WalletScoreRecord,
)
from pm_alpha_bot.market_categories import normalize_market_category
from pm_alpha_bot.time import ensure_utc

_ACTIVE_LIVE_ORDER_STATUSES = (
    "created",
    "submitted",
    "resting",
    "live",
    "delayed",
    "unmatched",
    "matched",
    "partially_filled",
)


class Repository:
    """Small repository wrapper around SQLAlchemy operations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_markets(self, markets: Sequence[NormalizedMarket]) -> int:
        """Insert or update normalized markets and tokens."""
        merged_markets: dict[str, NormalizedMarket] = {}
        for item in markets:
            existing = merged_markets.get(item.condition_id)
            if existing is None:
                merged_markets[item.condition_id] = item
                continue
            merged_tokens = {token.token_id: token for token in existing.tokens}
            for token in item.tokens:
                merged_tokens[token.token_id] = token
            merged_markets[item.condition_id] = item.model_copy(
                update={"tokens": list(merged_tokens.values())}
            )

        if not merged_markets:
            return 0

        market_rows: list[dict[str, Any]] = []
        token_rows_by_id: dict[str, dict[str, Any]] = {}
        for item in merged_markets.values():
            timestamp = utcnow()
            market_rows.append(
                {
                    "condition_id": item.condition_id,
                    "market_id": item.market_id,
                    "event_id": item.event_id,
                    "question": item.question,
                    "category": item.category,
                    "slug": item.slug,
                    "end_date": item.end_date,
                    "active": item.active,
                    "closed": item.closed,
                    "neg_risk": item.neg_risk,
                    "min_tick_size": item.min_tick_size,
                    "min_order_size": item.min_order_size,
                    "raw_json": item.raw_json,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            for token in item.tokens:
                if not token.token_id:
                    continue
                token_rows_by_id[token.token_id] = {
                    "condition_id": token.condition_id,
                    "token_id": token.token_id,
                    "outcome": token.outcome,
                    "side_label": token.side_label,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }

        self._bulk_upsert_market_rows(market_rows)
        self._bulk_upsert_market_token_rows(list(token_rows_by_id.values()))
        self.session.flush()
        return len(market_rows)

    def _bulk_upsert_market_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        """Upsert market rows by `condition_id` using the active SQL dialect."""
        if not rows:
            return
        bind = self.session.get_bind()
        dialect = bind.dialect.name
        stmt: Any
        if dialect == "postgresql":
            stmt_pg = postgresql_insert(Market).values(list(rows))
            update_map = {
                "market_id": stmt_pg.excluded.market_id,
                "event_id": stmt_pg.excluded.event_id,
                "question": stmt_pg.excluded.question,
                "category": stmt_pg.excluded.category,
                "slug": stmt_pg.excluded.slug,
                "end_date": stmt_pg.excluded.end_date,
                "active": stmt_pg.excluded.active,
                "closed": stmt_pg.excluded.closed,
                "neg_risk": stmt_pg.excluded.neg_risk,
                "min_tick_size": stmt_pg.excluded.min_tick_size,
                "min_order_size": stmt_pg.excluded.min_order_size,
                "raw_json": stmt_pg.excluded.raw_json,
                "updated_at": stmt_pg.excluded.updated_at,
            }
            stmt = stmt_pg.on_conflict_do_update(
                index_elements=[Market.condition_id],
                set_=update_map,
            )
        elif dialect == "sqlite":
            stmt_sqlite = sqlite_insert(Market).values(list(rows))
            update_map = {
                "market_id": stmt_sqlite.excluded.market_id,
                "event_id": stmt_sqlite.excluded.event_id,
                "question": stmt_sqlite.excluded.question,
                "category": stmt_sqlite.excluded.category,
                "slug": stmt_sqlite.excluded.slug,
                "end_date": stmt_sqlite.excluded.end_date,
                "active": stmt_sqlite.excluded.active,
                "closed": stmt_sqlite.excluded.closed,
                "neg_risk": stmt_sqlite.excluded.neg_risk,
                "min_tick_size": stmt_sqlite.excluded.min_tick_size,
                "min_order_size": stmt_sqlite.excluded.min_order_size,
                "raw_json": stmt_sqlite.excluded.raw_json,
                "updated_at": stmt_sqlite.excluded.updated_at,
            }
            stmt = stmt_sqlite.on_conflict_do_update(
                index_elements=[Market.condition_id],
                set_=update_map,
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect for market upserts: {dialect}")
        self.session.execute(stmt)

    def _bulk_upsert_market_token_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        """Upsert market token rows by `token_id` using the active SQL dialect."""
        if not rows:
            return
        bind = self.session.get_bind()
        dialect = bind.dialect.name
        stmt: Any
        if dialect == "postgresql":
            stmt_pg = postgresql_insert(MarketToken).values(list(rows))
            update_map = {
                "condition_id": stmt_pg.excluded.condition_id,
                "outcome": stmt_pg.excluded.outcome,
                "side_label": stmt_pg.excluded.side_label,
                "updated_at": stmt_pg.excluded.updated_at,
            }
            stmt = stmt_pg.on_conflict_do_update(
                index_elements=[MarketToken.token_id],
                set_=update_map,
            )
        elif dialect == "sqlite":
            stmt_sqlite = sqlite_insert(MarketToken).values(list(rows))
            update_map = {
                "condition_id": stmt_sqlite.excluded.condition_id,
                "outcome": stmt_sqlite.excluded.outcome,
                "side_label": stmt_sqlite.excluded.side_label,
                "updated_at": stmt_sqlite.excluded.updated_at,
            }
            stmt = stmt_sqlite.on_conflict_do_update(
                index_elements=[MarketToken.token_id],
                set_=update_map,
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect for market token upserts: {dialect}")
        self.session.execute(stmt)

    def upsert_wallets_from_leaderboard(
        self,
        entries: Sequence[LeaderboardEntry],
        *,
        time_period: str,
        observed_at: datetime | None = None,
    ) -> int:
        """Insert or refresh wallet rows from leaderboard entries."""
        count = 0
        now = observed_at or utcnow()
        for entry in entries:
            wallet = self.session.scalar(
                select(Wallet).where(Wallet.proxy_wallet == entry.proxy_wallet)
            )
            if wallet is None:
                wallet = Wallet(
                    proxy_wallet=entry.proxy_wallet,
                    first_seen=now,
                )
                self.session.add(wallet)
            wallet.username = entry.username
            wallet.source = entry.source
            wallet.last_seen = now
            merged_raw = dict(wallet.raw_json or {})
            merged_raw["leaderboard"] = entry.raw_json or entry.model_dump(mode="json")
            merged_raw["leaderboard_category"] = entry.category
            merged_raw["leaderboard_time_period"] = time_period
            merged_raw["leaderboard_observed_at"] = now.isoformat()
            wallet.raw_json = merged_raw
            snapshot = self.session.scalar(
                select(WalletLeaderboardSnapshot).where(
                    WalletLeaderboardSnapshot.proxy_wallet == entry.proxy_wallet,
                    WalletLeaderboardSnapshot.category == (entry.category or ""),
                    WalletLeaderboardSnapshot.time_period == time_period,
                    WalletLeaderboardSnapshot.observed_at == now,
                )
            )
            if snapshot is None:
                snapshot = WalletLeaderboardSnapshot(
                    proxy_wallet=entry.proxy_wallet,
                    category=entry.category or "",
                    time_period=time_period,
                    observed_at=now,
                )
                self.session.add(snapshot)
            snapshot.username = entry.username
            snapshot.source = entry.source
            snapshot.pnl = entry.pnl
            snapshot.volume = entry.volume
            snapshot.rank = entry.rank
            snapshot.raw_json = entry.raw_json or entry.model_dump(mode="json")
            count += 1
        self.session.flush()
        return count

    def latest_leaderboard_snapshots(
        self,
        *,
        category: str,
        as_of: datetime | None = None,
        time_period: str = "MONTH",
        limit: int | None = None,
    ) -> list[WalletLeaderboardSnapshot]:
        """Return the latest leaderboard snapshot row per wallet for a category/time period."""
        subquery_stmt = (
            select(
                WalletLeaderboardSnapshot.proxy_wallet,
                func.max(WalletLeaderboardSnapshot.observed_at).label("max_observed_at"),
            )
            .where(
                WalletLeaderboardSnapshot.category == category,
                WalletLeaderboardSnapshot.time_period == time_period,
            )
        )
        if as_of is not None:
            subquery_stmt = subquery_stmt.where(WalletLeaderboardSnapshot.observed_at <= as_of)
        subquery = subquery_stmt.group_by(WalletLeaderboardSnapshot.proxy_wallet).subquery()
        stmt = (
            select(WalletLeaderboardSnapshot)
            .join(
                subquery,
                (WalletLeaderboardSnapshot.proxy_wallet == subquery.c.proxy_wallet)
                & (WalletLeaderboardSnapshot.observed_at == subquery.c.max_observed_at),
            )
            .where(
                WalletLeaderboardSnapshot.category == category,
                WalletLeaderboardSnapshot.time_period == time_period,
            )
            .order_by(
                WalletLeaderboardSnapshot.pnl.desc().nullslast(),
                WalletLeaderboardSnapshot.volume.desc().nullslast(),
                WalletLeaderboardSnapshot.proxy_wallet.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def list_wallets(self, limit: int | None = None, active_only: bool = True) -> list[Wallet]:
        """Return tracked wallets."""
        stmt: Select[tuple[Wallet]] = select(Wallet).order_by(Wallet.last_seen.desc())
        if active_only:
            stmt = stmt.where(Wallet.is_active.is_(True))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def upsert_discovered_wallets(
        self,
        wallets: Sequence[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> int:
        """Insert or refresh wallets discovered from liquid market activity."""
        now = observed_at or utcnow()
        count = 0
        for item in wallets:
            proxy_wallet = str(item.get("proxy_wallet") or "")
            if not proxy_wallet:
                continue
            wallet = self.session.scalar(
                select(Wallet).where(Wallet.proxy_wallet == proxy_wallet)
            )
            if wallet is None:
                wallet = Wallet(
                    proxy_wallet=proxy_wallet,
                    first_seen=now,
                    source="liquidity_discovery",
                )
                self.session.add(wallet)
            if wallet.source != "leaderboard":
                wallet.source = "liquidity_discovery"
            username = item.get("username")
            if username and not wallet.username:
                wallet.username = str(username)
            wallet.last_seen = now
            payload = dict(wallet.raw_json or {})
            discovery_payload = dict(item)
            discovery_payload["observed_at"] = now.isoformat()
            payload["liquidity_discovery"] = discovery_payload
            wallet.raw_json = payload
            count += 1
        self.session.flush()
        return count

    def get_wallet(self, proxy_wallet: str) -> Wallet | None:
        """Return a wallet by address."""
        return self.session.scalar(select(Wallet).where(Wallet.proxy_wallet == proxy_wallet))

    def update_wallet_details(
        self,
        proxy_wallet: str,
        *,
        closed_positions: Sequence[dict[str, Any]] | None = None,
        trades: Sequence[dict[str, Any]] | None = None,
        total_value: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Persist current wallet detail payloads and append a timestamped snapshot."""
        wallet = self.get_wallet(proxy_wallet)
        if wallet is None:
            return
        now = observed_at or utcnow()
        payload = dict(wallet.raw_json or {})
        if closed_positions is not None:
            payload["closed_positions"] = list(closed_positions)
        if trades is not None:
            payload["trades"] = list(trades)
        if total_value is not None:
            payload["total_value"] = dict(total_value)
        wallet.raw_json = payload
        wallet.last_seen = now

        snapshot = self.session.scalar(
            select(WalletDetailSnapshot).where(
                WalletDetailSnapshot.proxy_wallet == proxy_wallet,
                WalletDetailSnapshot.observed_at == now,
            )
        )
        if snapshot is None:
            snapshot = WalletDetailSnapshot(
                proxy_wallet=proxy_wallet,
                observed_at=now,
            )
            self.session.add(snapshot)
        if closed_positions is not None:
            snapshot.closed_positions_json = list(closed_positions)
        if trades is not None:
            snapshot.trades_json = list(trades)
        if total_value is not None:
            snapshot.total_value_json = dict(total_value)
        self.session.flush()

    def latest_wallet_detail_snapshot(
        self,
        proxy_wallet: str,
        *,
        as_of: datetime | None = None,
    ) -> WalletDetailSnapshot | None:
        """Return the latest wallet detail snapshot at or before `as_of`."""
        stmt = select(WalletDetailSnapshot).where(
            WalletDetailSnapshot.proxy_wallet == proxy_wallet
        )
        if as_of is not None:
            stmt = stmt.where(WalletDetailSnapshot.observed_at <= as_of)
        stmt = stmt.order_by(WalletDetailSnapshot.observed_at.desc()).limit(1)
        return self.session.scalar(stmt)

    def replace_wallet_positions(
        self,
        proxy_wallet: str,
        positions: Sequence[WalletPositionRecord],
    ) -> None:
        """Replace current wallet positions for a wallet."""
        self.session.execute(
            delete(WalletPosition).where(WalletPosition.proxy_wallet == proxy_wallet)
        )
        for item in positions:
            self.session.add(
                WalletPosition(
                    proxy_wallet=proxy_wallet,
                    condition_id=item.condition_id,
                    token_id=item.token_id,
                    outcome=item.outcome,
                    size=item.size,
                    avg_price=item.avg_price,
                    current_price=item.current_price,
                    current_value=item.current_value,
                    cash_pnl=item.cash_pnl,
                    realized_pnl=item.realized_pnl,
                    total_pnl=item.total_pnl,
                    updated_at=item.updated_at,
                    raw_json=item.raw_json,
                )
            )
        wallet = self.session.scalar(select(Wallet).where(Wallet.proxy_wallet == proxy_wallet))
        if wallet is not None:
            wallet.last_seen = utcnow()

    def add_wallet_activity(self, activities: Sequence[WalletActivityRecord]) -> int:
        """Insert wallet activity rows while skipping obvious duplicates."""
        count = 0
        for item in activities:
            stmt = select(WalletActivity).where(
                WalletActivity.proxy_wallet == item.proxy_wallet,
                WalletActivity.ts == item.ts,
                WalletActivity.condition_id == item.condition_id,
                WalletActivity.token_id == item.token_id,
            )
            existing = self.session.scalar(stmt)
            if existing is not None and existing.tx_hash == item.tx_hash:
                continue
            self.session.add(
                WalletActivity(
                    proxy_wallet=item.proxy_wallet,
                    ts=item.ts,
                    condition_id=item.condition_id,
                    token_id=item.token_id,
                    side=item.side,
                    price=item.price,
                    size=item.size,
                    tx_hash=item.tx_hash,
                    raw_json=item.raw_json,
                )
            )
            count += 1
        self.session.flush()
        return count

    def save_snapshots(self, snapshots: Sequence[OrderBookSnapshot]) -> int:
        """Insert or refresh orderbook snapshots by `(token_id, ts)`."""
        count = 0
        for item in snapshots:
            existing = self.session.scalar(
                select(MarketSnapshot).where(
                    MarketSnapshot.token_id == item.token_id,
                    MarketSnapshot.ts == item.ts,
                )
            )
            if existing is None:
                existing = MarketSnapshot(
                    token_id=item.token_id, ts=item.ts, condition_id=item.condition_id
                )
                self.session.add(existing)
            existing.condition_id = item.condition_id
            existing.best_bid = item.best_bid
            existing.best_ask = item.best_ask
            existing.midpoint = item.midpoint
            existing.spread = item.spread
            existing.last_trade_price = item.last_trade_price
            existing.liquidity_score = item.liquidity_score
            existing.raw_json = item.raw_json
            count += 1
        self.session.flush()
        return count

    def list_active_tokens(
        self,
        limit: int | None = None,
        market_category: str | None = None,
    ) -> list[MarketToken]:
        """Return tokens belonging to active, not-closed markets."""
        stmt: Select[tuple[MarketToken]] = (
            select(MarketToken)
            .join(Market, Market.condition_id == MarketToken.condition_id)
            .where(Market.active.is_(True), Market.closed.is_(False))
            .order_by(Market.end_date.asc().nulls_last(), MarketToken.id.asc())
        )
        normalized_market_category = normalize_market_category(market_category)
        if normalized_market_category is not None:
            stmt = stmt.where(
                func.lower(func.coalesce(Market.category, "")) == normalized_market_category
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def get_market_by_condition_id(self, condition_id: str) -> Market | None:
        """Return a market by condition ID."""
        return self.session.scalar(select(Market).where(Market.condition_id == condition_id))

    def get_market_for_token(self, token_id: str) -> Market | None:
        """Return the market for a token."""
        stmt = (
            select(Market)
            .join(MarketToken, Market.condition_id == MarketToken.condition_id)
            .where(MarketToken.token_id == token_id)
        )
        return self.session.scalar(stmt)

    def latest_snapshot_for_token(
        self,
        token_id: str,
        as_of: datetime | None = None,
    ) -> MarketSnapshot | None:
        """Return the latest snapshot for a token, optionally bounded by time."""
        stmt = select(MarketSnapshot).where(MarketSnapshot.token_id == token_id)
        if as_of is not None:
            stmt = stmt.where(MarketSnapshot.ts <= as_of)
        stmt = stmt.order_by(MarketSnapshot.ts.desc()).limit(1)
        return self.session.scalar(stmt)

    def latest_snapshot_map(self, token_ids: Sequence[str]) -> dict[str, MarketSnapshot]:
        """Return the latest snapshot per token."""
        result: dict[str, MarketSnapshot] = {}
        for token_id in token_ids:
            snapshot = self.latest_snapshot_for_token(token_id)
            if snapshot is not None:
                result[token_id] = snapshot
        return result

    def snapshots_between(
        self,
        token_id: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[MarketSnapshot]:
        """Return snapshots for a token between two timestamps."""
        stmt = select(MarketSnapshot).where(
            MarketSnapshot.token_id == token_id, MarketSnapshot.ts >= start
        )
        if end is not None:
            stmt = stmt.where(MarketSnapshot.ts <= end)
        stmt = stmt.order_by(MarketSnapshot.ts.asc())
        return list(self.session.scalars(stmt))

    def future_snapshot(self, token_id: str, after: datetime) -> MarketSnapshot | None:
        """Return the first snapshot at or after the given timestamp."""
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.token_id == token_id, MarketSnapshot.ts >= after)
            .order_by(MarketSnapshot.ts.asc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def recent_wallet_activity(
        self,
        proxy_wallets: Sequence[str] | None = None,
        since: datetime | None = None,
        token_ids: Sequence[str] | None = None,
        market_category: str | None = None,
        until: datetime | None = None,
    ) -> list[WalletActivity]:
        """Return wallet activity filtered by wallet, time, or token."""
        stmt: Select[tuple[WalletActivity]] = select(WalletActivity).order_by(
            WalletActivity.ts.desc()
        )
        normalized_market_category = normalize_market_category(market_category)
        if normalized_market_category is not None:
            stmt = stmt.join(Market, Market.condition_id == WalletActivity.condition_id).where(
                func.lower(func.coalesce(Market.category, "")) == normalized_market_category
            )
        if proxy_wallets:
            stmt = stmt.where(WalletActivity.proxy_wallet.in_(proxy_wallets))
        if since is not None:
            stmt = stmt.where(WalletActivity.ts >= since)
        if until is not None:
            stmt = stmt.where(WalletActivity.ts <= until)
        if token_ids:
            stmt = stmt.where(WalletActivity.token_id.in_(token_ids))
        return list(self.session.scalars(stmt))

    def recent_wallet_activity_condition_ids(
        self,
        *,
        proxy_wallets: Sequence[str] | None = None,
        since: datetime | None = None,
        market_category: str | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Return recent distinct condition IDs ordered by most recent activity."""
        stmt = select(
            WalletActivity.condition_id,
            func.max(WalletActivity.ts).label("last_ts"),
        ).where(WalletActivity.condition_id.is_not(None))
        normalized_market_category = normalize_market_category(market_category)
        if normalized_market_category is not None:
            stmt = stmt.join(Market, Market.condition_id == WalletActivity.condition_id).where(
                func.lower(func.coalesce(Market.category, "")) == normalized_market_category
            )
        if proxy_wallets:
            stmt = stmt.where(WalletActivity.proxy_wallet.in_(proxy_wallets))
        if since is not None:
            stmt = stmt.where(WalletActivity.ts >= since)
        if until is not None:
            stmt = stmt.where(WalletActivity.ts <= until)
        stmt = stmt.group_by(WalletActivity.condition_id).order_by(
            func.max(WalletActivity.ts).desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [str(condition_id) for condition_id, _ in self.session.execute(stmt).all()]

    def recent_wallet_activity_token_ids(
        self,
        *,
        proxy_wallets: Sequence[str] | None = None,
        since: datetime | None = None,
        market_category: str | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Return recent distinct token IDs ordered by most recent activity."""
        stmt = select(
            WalletActivity.token_id,
            func.max(WalletActivity.ts).label("last_ts"),
        ).where(WalletActivity.token_id.is_not(None))
        normalized_market_category = normalize_market_category(market_category)
        if normalized_market_category is not None:
            stmt = stmt.join(Market, Market.condition_id == WalletActivity.condition_id).where(
                func.lower(func.coalesce(Market.category, "")) == normalized_market_category
            )
        if proxy_wallets:
            stmt = stmt.where(WalletActivity.proxy_wallet.in_(proxy_wallets))
        if since is not None:
            stmt = stmt.where(WalletActivity.ts >= since)
        if until is not None:
            stmt = stmt.where(WalletActivity.ts <= until)
        stmt = stmt.group_by(WalletActivity.token_id).order_by(
            func.max(WalletActivity.ts).desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [str(token_id) for token_id, _ in self.session.execute(stmt).all()]

    def latest_wallet_scores(
        self,
        category: str,
        min_score: float | None = None,
        as_of: datetime | None = None,
    ) -> list[WalletScore]:
        """Return the latest score row per wallet for a category."""
        subquery_stmt = (
            select(
                WalletScore.proxy_wallet,
                func.max(WalletScore.as_of).label("max_as_of"),
            )
            .where(WalletScore.category == category)
        )
        if as_of is not None:
            subquery_stmt = subquery_stmt.where(WalletScore.as_of <= as_of)
        subquery = subquery_stmt.group_by(WalletScore.proxy_wallet).subquery()
        stmt = (
            select(WalletScore)
            .join(
                subquery,
                (WalletScore.proxy_wallet == subquery.c.proxy_wallet)
                & (WalletScore.as_of == subquery.c.max_as_of),
            )
            .where(WalletScore.category == category)
            .order_by(WalletScore.score.desc().nullslast())
        )
        if min_score is not None:
            stmt = stmt.where(WalletScore.score >= min_score)
        return list(self.session.scalars(stmt))

    def list_snapshots(
        self,
        start: datetime,
        end: datetime,
        token_ids: Sequence[str] | None = None,
    ) -> list[MarketSnapshot]:
        """Return snapshots in a time range ordered by time."""
        stmt: Select[tuple[MarketSnapshot]] = select(MarketSnapshot).where(
            MarketSnapshot.ts >= start,
            MarketSnapshot.ts <= end,
        )
        if token_ids:
            stmt = stmt.where(MarketSnapshot.token_id.in_(token_ids))
        stmt = stmt.order_by(MarketSnapshot.ts.asc(), MarketSnapshot.token_id.asc())
        return list(self.session.scalars(stmt))

    def save_wallet_scores(self, scores: Sequence[WalletScoreRecord]) -> int:
        """Insert wallet score rows."""
        count = 0
        for item in scores:
            existing = self.session.scalar(
                select(WalletScore).where(
                    WalletScore.proxy_wallet == item.proxy_wallet,
                    WalletScore.category == item.category,
                    WalletScore.as_of == item.as_of,
                )
            )
            if existing is None:
                existing = WalletScore(
                    proxy_wallet=item.proxy_wallet,
                    category=item.category,
                    as_of=item.as_of,
                )
                self.session.add(existing)
            existing.pnl = item.pnl
            existing.roi = item.roi
            existing.win_rate = item.win_rate
            existing.closed_market_count = item.closed_market_count
            existing.trade_count = item.trade_count
            existing.clv_1h = item.clv_1h
            existing.clv_6h = item.clv_6h
            existing.clv_24h = item.clv_24h
            existing.max_drawdown = item.max_drawdown
            existing.profit_concentration = item.profit_concentration
            existing.score = item.score
            existing.raw_metrics_json = item.raw_metrics_json
            count += 1
        self.session.flush()
        return count

    def save_signals(self, signals: Sequence[SignalRecord]) -> int:
        """Insert signal rows."""
        count = 0
        for item in signals:
            self.session.add(
                Signal(
                    ts=item.ts,
                    condition_id=item.condition_id,
                    token_id=item.token_id,
                    direction=item.direction,
                    market_midpoint=item.market_midpoint,
                    effective_entry_price=item.effective_entry_price,
                    fair_prob=item.fair_prob,
                    edge_bps=item.edge_bps,
                    confidence=item.confidence,
                    source_wallet_count=item.source_wallet_count,
                    source_wallets_json=item.source_wallets_json,
                    reason_json=item.reason_json,
                    status=item.status,
                )
            )
            count += 1
        self.session.flush()
        return count

    def list_recent_signals(self, status: str = "new", limit: int = 20) -> list[Signal]:
        """Return recent signals by status."""
        stmt = select(Signal).where(Signal.status == status).order_by(Signal.ts.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def claim_next_live_signal(
        self,
        *,
        as_of: datetime,
        limit: int = 20,
        pending_status: str = "live_pending",
    ) -> Signal | None:
        """Atomically claim the next new signal for live execution."""
        stmt = (
            select(Signal)
            .where(Signal.status == "new", Signal.ts <= as_of)
            .order_by(Signal.ts.desc(), Signal.id.desc())
            .limit(limit)
        )
        for signal in self.session.scalars(stmt):
            if signal.id is None:
                continue
            if self.live_order_exists_for_signal(signal.id):
                self.update_signal_status(
                    signal.id,
                    status="processed",
                    reason_json={
                        "live_skip_reason": "live_order_already_exists_for_signal",
                        "live_skip_at": utcnow().isoformat(),
                    },
                )
                continue
            if self.active_live_order_exists_for_token(signal.token_id):
                continue
            result = self.session.execute(
                update(Signal)
                .where(Signal.id == signal.id, Signal.status == "new")
                .values(status=pending_status)
                .execution_options(synchronize_session=False)
            )
            if cast(Any, result).rowcount != 1:
                continue
            self.session.flush()
            self.session.refresh(signal)
            return signal
        return None

    def update_signal_status(
        self,
        signal_id: int,
        *,
        status: str,
        reason_json: dict[str, Any] | None = None,
    ) -> Signal | None:
        """Update a signal status and merge optional metadata into reason_json."""
        signal = self.session.get(Signal, signal_id)
        if signal is None:
            return None
        signal.status = status
        if reason_json:
            payload = dict(signal.reason_json or {})
            payload.update(reason_json)
            signal.reason_json = payload
        self.session.flush()
        return signal

    def create_order(self, request: PaperOrderRequest, mode: str, status: str) -> Order:
        """Create an order row."""
        order = Order(
            mode=mode,
            condition_id=request.condition_id,
            token_id=request.token_id,
            side=request.side,
            price=request.price,
            size=request.size,
            order_type=request.order_type,
            status=status,
            signal_id=request.signal_id,
            reason_json=request.reason_json,
        )
        self.session.add(order)
        self.session.flush()
        return order

    def get_runtime_state(self, key: str) -> RuntimeState | None:
        """Return a runtime state row by key."""
        return self.session.scalar(select(RuntimeState).where(RuntimeState.key == key))

    def upsert_runtime_state(self, key: str, state_json: dict[str, Any]) -> RuntimeState:
        """Insert or update a runtime state JSON blob."""
        state = self.get_runtime_state(key)
        if state is None:
            state = RuntimeState(key=key)
            self.session.add(state)
        state.state_json = state_json
        state.updated_at = utcnow()
        self.session.flush()
        return state

    def insert_runtime_state_once(self, key: str, state_json: dict[str, Any]) -> bool:
        """Insert a runtime state row only if the key does not already exist."""
        timestamp = utcnow()
        row = {"key": key, "state_json": state_json, "updated_at": timestamp}
        bind = self.session.get_bind()
        dialect = bind.dialect.name
        stmt: Any
        if dialect == "postgresql":
            stmt = (
                postgresql_insert(RuntimeState)
                .values(row)
                .on_conflict_do_nothing(index_elements=[RuntimeState.key])
            )
        elif dialect == "sqlite":
            stmt = (
                sqlite_insert(RuntimeState)
                .values(row)
                .on_conflict_do_nothing(index_elements=[RuntimeState.key])
            )
        else:
            raise RuntimeError(f"Unsupported SQL dialect for runtime state insert: {dialect}")
        result = self.session.execute(stmt)
        self.session.flush()
        return cast(Any, result).rowcount == 1

    def delete_runtime_state(self, key: str) -> bool:
        """Delete a runtime state row by key."""
        state = self.get_runtime_state(key)
        if state is None:
            return False
        self.session.delete(state)
        self.session.flush()
        return True

    def live_operations_state(self) -> dict[str, Any]:
        """Return persisted live runtime state snapshots."""
        supervisor = self.get_runtime_state("live_supervisor")
        kill_switch = self.get_runtime_state("live_kill_switch")
        return {
            "supervisor": dict(supervisor.state_json or {}) if supervisor is not None else None,
            "kill_switch": dict(kill_switch.state_json or {}) if kill_switch is not None else None,
        }

    def add_runtime_event(
        self,
        *,
        category: str,
        event_type: str,
        level: str = "info",
        message: str | None = None,
        event_json: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> RuntimeEvent:
        """Append a runtime event row for operational auditability."""
        event = RuntimeEvent(
            ts=ts or utcnow(),
            category=category,
            level=level,
            event_type=event_type,
            message=message,
            event_json=event_json,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def list_runtime_events(
        self,
        *,
        category: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[RuntimeEvent]:
        """Return recent runtime events for the requested category/type."""
        stmt: Select[tuple[RuntimeEvent]] = select(RuntimeEvent).order_by(
            RuntimeEvent.ts.desc(),
            RuntimeEvent.id.desc(),
        )
        if category is not None:
            stmt = stmt.where(RuntimeEvent.category == category)
        if event_type is not None:
            stmt = stmt.where(RuntimeEvent.event_type == event_type)
        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def prune_runtime_events(
        self,
        *,
        older_than: datetime,
        category: str | None = None,
        dry_run: bool = True,
    ) -> int:
        """Count or delete runtime events older than the given cutoff."""
        count_stmt = (
            select(func.count()).select_from(RuntimeEvent).where(RuntimeEvent.ts < older_than)
        )
        delete_stmt = delete(RuntimeEvent).where(RuntimeEvent.ts < older_than)
        if category is not None:
            count_stmt = count_stmt.where(RuntimeEvent.category == category)
            delete_stmt = delete_stmt.where(RuntimeEvent.category == category)
        count = int(self.session.scalar(count_stmt) or 0)
        if dry_run or count == 0:
            return count
        self.session.execute(delete_stmt)
        self.session.flush()
        return count

    def create_live_order(
        self,
        prepared: PreparedLiveOrder,
        *,
        status: str,
        external_order_id: str | None = None,
        reason_json: dict[str, Any] | None = None,
    ) -> Order:
        """Create a live order row from a prepared order."""
        if prepared.signal_id > 0 and self.live_order_exists_for_signal(prepared.signal_id):
            raise ValueError(f"Live order already exists for signal_id={prepared.signal_id}.")
        if status in _ACTIVE_LIVE_ORDER_STATUSES and self.active_live_order_exists_for_token(
            prepared.token_id
        ):
            raise ValueError(f"Active live order already exists for token_id={prepared.token_id}.")
        payload = dict(prepared.reason_json)
        if reason_json:
            payload.update(reason_json)
        order = Order(
            mode="live",
            condition_id=prepared.condition_id,
            token_id=prepared.token_id,
            side=prepared.side,
            price=prepared.price,
            size=prepared.size,
            order_type=prepared.order_type,
            status=status,
            signal_id=prepared.signal_id,
            external_order_id=external_order_id,
            reason_json=payload,
        )
        self.session.add(order)
        self.session.flush()
        return order

    def live_order_exists_for_signal(self, signal_id: int) -> bool:
        """Return true when any live order already references the signal."""
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.mode == "live", Order.signal_id == signal_id)
        )
        return int(self.session.scalar(stmt) or 0) > 0

    def active_live_order_exists_for_token(self, token_id: str) -> bool:
        """Return true when a live order can still execute for the token."""
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(
                Order.mode == "live",
                Order.token_id == token_id,
                Order.status.in_(list(_ACTIVE_LIVE_ORDER_STATUSES)),
            )
        )
        return int(self.session.scalar(stmt) or 0) > 0

    def create_live_order_from_remote(self, remote_order: LiveOpenOrder) -> Order:
        """Backfill a local live order row from a remote open order."""
        order = Order(
            mode="live",
            condition_id=remote_order.condition_id or "",
            token_id=remote_order.token_id or "",
            side=remote_order.side or "BUY",
            price=remote_order.price,
            size=remote_order.original_size,
            order_type=remote_order.order_type or "GTC",
            status=remote_order.status or "live",
            signal_id=None,
            external_order_id=remote_order.external_order_id,
            reason_json={"sync_source": "remote_open_order", "remote_order": remote_order.raw_json},
        )
        self.session.add(order)
        self.session.flush()
        return order

    def get_order(self, order_id: int) -> Order | None:
        """Return an order by ID."""
        return self.session.scalar(select(Order).where(Order.id == order_id))

    def get_order_by_external_order_id(
        self,
        external_order_id: str,
        *,
        mode: str = "live",
    ) -> Order | None:
        """Return an order by its exchange order ID."""
        stmt = select(Order).where(
            Order.external_order_id == external_order_id,
            Order.mode == mode,
        )
        return self.session.scalar(stmt)

    def list_open_orders(self, mode: str = "paper") -> list[Order]:
        """Return open orders."""
        stmt = select(Order).where(
            Order.mode == mode, Order.status.in_(["created", "resting", "partially_filled"])
        )
        return list(self.session.scalars(stmt))

    def list_orders(
        self,
        *,
        mode: str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[Order]:
        """Return orders optionally filtered by mode and status."""
        stmt: Select[tuple[Order]] = select(Order).order_by(
            Order.created_at.desc(),
            Order.id.desc(),
        )
        if mode is not None:
            stmt = stmt.where(Order.mode == mode)
        if statuses:
            stmt = stmt.where(Order.status.in_(list(statuses)))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def update_order_status(
        self,
        order: Order,
        *,
        status: str,
        external_order_id: str | None = None,
        reason_json: dict[str, Any] | None = None,
    ) -> Order:
        """Update the stored status and optional metadata for an order."""
        order.status = status
        if external_order_id is not None:
            order.external_order_id = external_order_id
        if reason_json:
            payload = dict(order.reason_json or {})
            payload.update(reason_json)
            order.reason_json = payload
        self.session.flush()
        return order

    def upsert_fill(
        self,
        order: Order,
        ts: datetime,
        price: float,
        size: float,
        fee: float = 0.0,
        tx_hash: str | None = None,
        raw_json: dict[str, Any] | None = None,
    ) -> tuple[Fill, bool]:
        """Insert a fill unless the same trade has already been stored."""
        trade_id = str((raw_json or {}).get("trade_id") or "")
        for existing in self.fills_for_order(order.id):
            existing_trade_id = str((existing.raw_json or {}).get("trade_id") or "")
            if trade_id and existing_trade_id == trade_id:
                return existing, False
            if (
                tx_hash
                and existing.tx_hash == tx_hash
                and existing.price == price
                and existing.size == size
            ):
                return existing, False
            if existing.ts == ts and existing.price == price and existing.size == size:
                return existing, False
        fill = Fill(
            order_id=order.id,
            ts=ts,
            price=price,
            size=size,
            fee=fee,
            tx_hash=tx_hash,
            raw_json=raw_json,
        )
        self.session.add(fill)
        self.session.flush()
        return fill, True

    def add_fill(
        self,
        order: Order,
        ts: datetime,
        price: float,
        size: float,
        fee: float = 0.0,
        tx_hash: str | None = None,
        raw_json: dict[str, Any] | None = None,
    ) -> Fill:
        """Create a fill record."""
        fill, _ = self.upsert_fill(
            order=order,
            ts=ts,
            price=price,
            size=size,
            fee=fee,
            tx_hash=tx_hash,
            raw_json=raw_json,
        )
        return fill

    def fills_for_order(self, order_id: int) -> list[Fill]:
        """Return fills for an order ordered by time."""
        stmt = select(Fill).where(Fill.order_id == order_id).order_by(Fill.ts.asc())
        return list(self.session.scalars(stmt))

    def filled_size_for_order(self, order_id: int) -> float:
        """Return the cumulative filled size for an order."""
        stmt = select(func.coalesce(func.sum(Fill.size), 0.0)).where(Fill.order_id == order_id)
        value = self.session.scalar(stmt)
        return float(value or 0.0)

    def order_fill_rows(self, mode: str) -> list[tuple[Order, Fill]]:
        """Return `(order, fill)` tuples for a mode ordered by fill time."""
        stmt = (
            select(Order, Fill)
            .join(Fill, Fill.order_id == Order.id)
            .where(Order.mode == mode)
            .order_by(Fill.ts.asc(), Fill.id.asc(), Order.id.asc())
        )
        rows = self.session.execute(stmt).all()
        return [(row[0], row[1]) for row in rows]

    def get_paper_position(self, token_id: str) -> PaperPosition | None:
        """Return the current paper position for a token."""
        stmt = select(PaperPosition).where(PaperPosition.token_id == token_id).limit(1)
        return self.session.scalar(stmt)

    def upsert_paper_position(
        self,
        condition_id: str,
        token_id: str,
        side: str,
        size: float,
        avg_price: float,
        realized_pnl: float,
        unrealized_pnl: float,
    ) -> PaperPosition:
        """Insert or update a paper position row."""
        position = self.get_paper_position(token_id)
        if position is None:
            position = PaperPosition(
                condition_id=condition_id,
                token_id=token_id,
                side=side,
            )
            self.session.add(position)
        position.condition_id = condition_id
        position.token_id = token_id
        position.side = side
        position.size = size
        position.avg_price = avg_price
        position.realized_pnl = realized_pnl
        position.unrealized_pnl = unrealized_pnl
        position.updated_at = utcnow()
        self.session.flush()
        return position

    def list_paper_positions(self) -> list[PaperPosition]:
        """Return all paper positions."""
        return list(
            self.session.scalars(select(PaperPosition).order_by(PaperPosition.updated_at.desc()))
        )

    def paper_risk_state(self, initial_capital: float) -> RiskState:
        """Compute a conservative paper trading risk state from stored positions and fills."""
        positions = self.list_paper_positions()
        latest_snapshots = self.latest_snapshot_map([position.token_id for position in positions])
        capital = initial_capital
        realized = sum(position.realized_pnl for position in positions)
        unrealized = 0.0
        market_exposure: dict[str, float] = {}
        condition_exposure: dict[str, float] = defaultdict(float)
        event_exposure: dict[str, float] = defaultdict(float)
        category_exposure: dict[str, float] = defaultdict(float)
        for position in positions:
            snapshot = latest_snapshots.get(position.token_id)
            market_value = 0.0
            if snapshot is not None and snapshot.midpoint is not None:
                market_value = position.size * snapshot.midpoint
                unrealized += (snapshot.midpoint - position.avg_price) * position.size
            market = self.get_market_for_token(
                position.token_id
            ) or self.get_market_by_condition_id(position.condition_id)
            exposure_pct = market_value / capital if capital else 0.0
            market_exposure[position.token_id] = exposure_pct
            condition_id = market.condition_id if market is not None else position.condition_id
            if condition_id:
                condition_exposure[condition_id] += exposure_pct
            if market is not None:
                if market.event_id:
                    event_exposure[market.event_id] += exposure_pct
                if market.category:
                    category_exposure[market.category] += exposure_pct
        equity = capital + realized + unrealized
        fills_today = self.session.scalars(
            select(Fill)
            .join(Order, Fill.order_id == Order.id)
            .where(
                Order.mode == "paper",
                Fill.ts >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
            )
        )
        daily_realized_delta = 0.0
        for fill in fills_today:
            payload = fill.raw_json or {}
            daily_realized_delta += float(payload.get("realized_pnl_delta", 0.0))
        daily_mark_to_market = daily_realized_delta + unrealized
        daily_loss_pct = max(0.0, -daily_mark_to_market / capital) if capital else 0.0
        total_drawdown_pct = max(0.0, (capital - equity) / capital) if capital else 0.0
        cash = capital + realized
        return RiskState(
            capital=capital,
            cash=cash,
            equity=equity,
            daily_loss_pct=daily_loss_pct,
            total_drawdown_pct=total_drawdown_pct,
            market_exposure_pct=market_exposure,
            condition_exposure_pct=dict(condition_exposure),
            event_exposure_pct=dict(event_exposure),
            category_exposure_pct=dict(category_exposure),
        )

    def live_risk_state(self, initial_capital: float) -> RiskState:
        """Compute live risk state from synced live orders and fills."""
        report = self.live_report(initial_capital)
        capital = float(initial_capital)
        realized_net = float(report["realized_pnl"]) - float(report["fees_total"])
        unrealized = float(report["unrealized_pnl"])
        cash = capital + realized_net
        equity = cash + unrealized
        daily_realized_net = float(report["daily_realized_pnl"]) - float(report["daily_fees_total"])
        daily_mark_to_market = daily_realized_net + unrealized
        daily_loss_pct = max(0.0, -daily_mark_to_market / capital) if capital else 0.0
        total_drawdown_pct = max(0.0, (capital - equity) / capital) if capital else 0.0
        return RiskState(
            capital=capital,
            cash=cash,
            equity=equity,
            daily_loss_pct=daily_loss_pct,
            total_drawdown_pct=total_drawdown_pct,
            market_exposure_pct=dict(report["market_exposure_pct"]),
            condition_exposure_pct=dict(report["condition_exposure_pct"]),
            event_exposure_pct=dict(report["event_exposure_pct"]),
            category_exposure_pct=dict(report["category_exposure_pct"]),
        )

    def live_report(self, initial_capital: float) -> dict[str, Any]:
        """Build a live trading accounting report from synced orders and fills."""
        rows = self.order_fill_rows(mode="live")
        positions: dict[str, dict[str, Any]] = {}
        realized_pnl = 0.0
        daily_realized_pnl = 0.0
        fees_total = 0.0
        daily_fees_total = 0.0
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        for order, fill in rows:
            if fill.price is None or fill.size is None or fill.size <= 0:
                continue
            fill_ts = ensure_utc(fill.ts)
            position = positions.setdefault(
                order.token_id,
                {
                    "condition_id": order.condition_id,
                    "token_id": order.token_id,
                    "net_size": 0.0,
                    "avg_price": 0.0,
                    "realized_pnl": 0.0,
                    "fees_total": 0.0,
                    "updated_at": fill_ts,
                },
            )
            realized_delta = _apply_fill_to_position(
                position,
                side=order.side,
                size=float(fill.size),
                price=float(fill.price),
            )
            fee = float(fill.fee or 0.0)
            position["condition_id"] = order.condition_id
            position["updated_at"] = fill_ts
            position["realized_pnl"] += realized_delta
            position["fees_total"] += fee
            realized_pnl += realized_delta
            fees_total += fee
            if fill_ts >= start_of_day:
                daily_realized_pnl += realized_delta
                daily_fees_total += fee

        market_exposure: dict[str, float] = {}
        condition_exposure: dict[str, float] = defaultdict(float)
        event_exposure: dict[str, float] = defaultdict(float)
        category_exposure: dict[str, float] = defaultdict(float)
        position_rows: list[dict[str, Any]] = []
        unrealized_total = 0.0
        for token_id, position in positions.items():
            net_size = float(position["net_size"])
            avg_price = float(position["avg_price"])
            side = "FLAT"
            size = abs(net_size)
            current_price: float | None = None
            current_value = 0.0
            unrealized_pnl = 0.0
            if net_size > 0:
                side = "LONG"
            elif net_size < 0:
                side = "SHORT"

            snapshot = self.latest_snapshot_for_token(token_id)
            if snapshot is not None and snapshot.midpoint is not None:
                current_price = snapshot.midpoint
                current_value = size * snapshot.midpoint
                if side == "LONG":
                    unrealized_pnl = (snapshot.midpoint - avg_price) * size
                elif side == "SHORT":
                    unrealized_pnl = (avg_price - snapshot.midpoint) * size

            market = self.get_market_for_token(token_id) or self.get_market_by_condition_id(
                str(position["condition_id"])
            )
            exposure_pct = current_value / initial_capital if initial_capital else 0.0
            market_exposure[token_id] = exposure_pct
            condition_id = market.condition_id if market is not None else position["condition_id"]
            if condition_id:
                condition_exposure[str(condition_id)] += exposure_pct
            if market is not None:
                if market.event_id:
                    event_exposure[market.event_id] += exposure_pct
                if market.category:
                    category_exposure[market.category] += exposure_pct
            unrealized_total += unrealized_pnl
            position_rows.append(
                {
                    "condition_id": position["condition_id"],
                    "token_id": token_id,
                    "side": side,
                    "net_size": net_size,
                    "size": size,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "current_value": current_value,
                    "realized_pnl": float(position["realized_pnl"]),
                    "unrealized_pnl": unrealized_pnl,
                    "fees_total": float(position["fees_total"]),
                    "updated_at": position["updated_at"],
                    "category": market.category if market is not None else None,
                }
            )

        position_rows.sort(
            key=lambda item: (
                item["updated_at"],
                item["token_id"],
            ),
            reverse=True,
        )
        open_orders = self.list_orders(
            mode="live",
            statuses=[
                "created",
                "submitted",
                "resting",
                "live",
                "delayed",
                "unmatched",
                "matched",
                "partially_filled",
            ],
        )
        all_live_orders = self.list_orders(mode="live")
        return {
            "positions": position_rows,
            "fills_count": len(rows),
            "orders_count": len(all_live_orders),
            "open_orders_count": len(open_orders),
            "realized_pnl": realized_pnl,
            "daily_realized_pnl": daily_realized_pnl,
            "fees_total": fees_total,
            "daily_fees_total": daily_fees_total,
            "net_realized_pnl": realized_pnl - fees_total,
            "unrealized_pnl": unrealized_total,
            "equity": initial_capital + realized_pnl - fees_total + unrealized_total,
            "market_exposure_pct": market_exposure,
            "condition_exposure_pct": dict(condition_exposure),
            "event_exposure_pct": dict(event_exposure),
            "category_exposure_pct": dict(category_exposure),
        }

    def wallet_summary_rows(self, top: int) -> list[WalletScore]:
        """Return top wallet score rows across categories."""
        subquery = (
            select(
                WalletScore.proxy_wallet,
                WalletScore.category,
                func.max(WalletScore.as_of).label("max_as_of"),
            )
            .group_by(WalletScore.proxy_wallet, WalletScore.category)
            .subquery()
        )
        stmt = (
            select(WalletScore)
            .join(
                subquery,
                (WalletScore.proxy_wallet == subquery.c.proxy_wallet)
                & (WalletScore.category == subquery.c.category)
                & (WalletScore.as_of == subquery.c.max_as_of),
            )
            .order_by(WalletScore.score.desc().nullslast())
            .limit(top)
        )
        return list(self.session.scalars(stmt))

    def signal_summary_rows(self, top: int) -> list[Signal]:
        """Return top signals by edge."""
        stmt = select(Signal).order_by(Signal.edge_bps.desc().nullslast()).limit(top)
        return list(self.session.scalars(stmt))

    def paper_report(self) -> dict[str, Any]:
        """Build a lightweight paper trading report payload."""
        positions = self.list_paper_positions()
        fills = self.order_fill_rows(mode="paper")
        return {
            "positions": positions,
            "fills_count": len(fills),
            "realized_pnl": sum(position.realized_pnl for position in positions),
            "unrealized_pnl": sum(position.unrealized_pnl for position in positions),
        }


def _apply_fill_to_position(
    position: dict[str, Any],
    *,
    side: str,
    size: float,
    price: float,
) -> float:
    """Apply a fill to a signed position and return realized PnL delta."""
    if size <= 0:
        return 0.0
    net_size = float(position.get("net_size", 0.0))
    avg_price = float(position.get("avg_price", 0.0))
    resolved_side = side.upper()

    if resolved_side == "BUY":
        if net_size >= 0:
            new_size = net_size + size
            position["avg_price"] = (
                (((avg_price * net_size) + (price * size)) / new_size) if new_size > 0 else 0.0
            )
            position["net_size"] = new_size
            return 0.0

        short_size = abs(net_size)
        close_size = min(short_size, size)
        realized = (avg_price - price) * close_size
        remaining_buy = size - close_size
        if remaining_buy > 0:
            position["net_size"] = remaining_buy
            position["avg_price"] = price
        else:
            next_short = short_size - close_size
            position["net_size"] = -next_short
            position["avg_price"] = avg_price if next_short > 0 else 0.0
        return realized

    if net_size <= 0:
        new_short = abs(net_size) + size
        position["avg_price"] = (
            (((avg_price * abs(net_size)) + (price * size)) / new_short) if new_short > 0 else 0.0
        )
        position["net_size"] = -new_short
        return 0.0

    close_size = min(net_size, size)
    realized = (price - avg_price) * close_size
    remaining_sell = size - close_size
    if remaining_sell > 0:
        position["net_size"] = -remaining_sell
        position["avg_price"] = price
    else:
        next_long = net_size - close_size
        position["net_size"] = next_long
        position["avg_price"] = avg_price if next_long > 0 else 0.0
    return realized
