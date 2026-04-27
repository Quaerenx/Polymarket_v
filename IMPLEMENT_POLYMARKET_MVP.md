아래 프롬프트는 Polymarket 공식 문서 기준으로 Gamma API / Data API / CLOB API 분리를 반영했고, CLOB V2의 py-clob-client-v2, pUSD, 수수료 모델, V2 테스트 호스트, WebSocket market channel, 주문 타입 구조를 기준으로 작성했습니다.

# Polymarket Wallet-Weighted EV Auto-Trading Bot MVP 구현 지시서

## 0. 역할

당신은 시니어 Python 백엔드/데이터 엔지니어이자 퀀트 트레이딩 시스템 엔지니어다.

이 저장소에 Polymarket용 MVP 자동매매 봇을 구현한다.

목표는 “당장 실거래 수익 극대화”가 아니라, 다음 4가지를 안전하고 검증 가능하게 만드는 것이다.

1. Polymarket 시장/호가/지갑 데이터를 안정적으로 수집한다.
2. 리더보드 기반 우수 지갑을 추적하고 스코어링한다.
3. 지갑 컨센서스 + 퀀트 필터 + 리스크 엔진으로 매매 신호를 만든다.
4. 기본값은 반드시 paper trading이며, live trading은 명시적으로 켜야만 동작하게 한다.

이 프로젝트는 Polymarket CLOB V2 기준으로 설계한다.

---

## 1. 핵심 전략 요약

구현할 전략명:

`Wallet-Weighted Liquidity-Aware EV Bot`

핵심 원칙:

- 리더보드의 단순 승률 70% 이상 지갑을 무조건 추종하지 않는다.
- 지갑 추적은 독립적인 매수/매도 근거가 아니라 알파 입력값으로만 사용한다.
- 최종 진입은 항상 기대값, 유동성, 스프레드, 슬리피지, 리스크 제한을 통과해야 한다.
- maker-first limit order 또는 paper maker simulation을 기본으로 한다.
- market order/FOK/FAK는 MVP에서는 기본 비활성화한다.
- MVP 기본 실행 모드는 `paper`다.
- live trading은 `ENABLE_LIVE_TRADING=true`와 CLI의 `--live` 옵션이 동시에 있어야만 가능하다.
- private key, API key, wallet secret을 절대 코드에 하드코딩하지 않는다.

---

## 2. 공식 문서 기반 전제

현재 기준으로 다음 전제를 코드에 반영하라. 단, 모든 호스트와 엔드포인트는 환경변수로 override 가능해야 한다.

### Polymarket API 분리

- Gamma API:
  - 시장, 이벤트, 태그, 시리즈, 검색, public profile 탐색
  - 기본 호스트: `https://gamma-api.polymarket.com`

- Data API:
  - 리더보드, 지갑 포지션, closed positions, activity, trades, value, holders 등
  - 기본 호스트: `https://data-api.polymarket.com`

- CLOB API:
  - orderbook, price, midpoint, spread, price history, order placement/cancel 등
  - V2 테스트 호스트: `https://clob-v2.polymarket.com`
  - 프로덕션 호스트: `https://clob.polymarket.com`
  - 호스트는 반드시 `POLY_CLOB_HOST` 환경변수로 설정 가능하게 한다.

### CLOB V2 전제

- SDK는 V2 패키지를 기준으로 한다.
- Python 우선 구현:
  - `py-clob-client-v2`
- 기존 `py-clob-client`에 직접 의존하지 않는다.
- V2에서는 collateral이 `pUSD` 기준이다.
- V2에서는 `nonce`, `feeRateBps`, `taker`를 주문 생성 코드에서 직접 세팅하지 않는다.
- V2에서는 `timestamp`, `metadata`, `builder`/`builderCode` 개념을 고려한다.
- maker는 수수료가 없고, taker는 프로토콜의 동적 수수료 모델을 따른다.
- MVP는 수수료를 보수적으로 추정하되, CLOB market info 또는 SDK가 제공하는 fee 정보를 우선 사용한다.
- 시장 데이터 실시간 수집은 polling보다 WebSocket market channel을 우선 지원하되, MVP에서는 REST polling fallback도 반드시 구현한다.

### 지리 제한 체크

live trading 진입 전에는 반드시 geoblock 체크를 수행한다.

- `GET https://polymarket.com/api/geoblock`
- 응답의 `blocked`가 true이면 신규 주문을 금지한다.
- paper trading에서는 geoblock 실패가 전체 실행을 막지 않게 하되 warning을 출력한다.

---

## 3. 기술 스택

Python 3.12 기준으로 구현한다.

권장 패키지:

- 패키지/실행:
  - `uv` 또는 `poetry`
- CLI:
  - `typer`
  - `rich`
- HTTP:
  - `httpx`
  - `tenacity`
- 설정:
  - `pydantic`
  - `pydantic-settings`
  - `python-dotenv`
- DB:
  - `sqlalchemy`
  - `alembic`
  - `psycopg[binary]`
  - SQLite fallback도 가능하게 설계
- 데이터/퀀트:
  - `pandas`
  - `numpy`
- WebSocket:
  - `websockets`
- 테스트:
  - `pytest`
  - `pytest-asyncio`
  - `respx`
  - `freezegun`
- 품질:
  - `ruff`
  - `mypy`

프로젝트 루트에 다음을 만든다.

```text
.
├── README.md
├── .env.example
├── pyproject.toml
├── docker-compose.yml
├── alembic.ini
├── migrations/
├── src/
│   └── pm_alpha_bot/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── logging.py
│       ├── db/
│       │   ├── session.py
│       │   ├── models.py
│       │   └── repository.py
│       ├── clients/
│       │   ├── gamma.py
│       │   ├── data_api.py
│       │   ├── clob_public.py
│       │   ├── clob_v2.py
│       │   ├── websocket_market.py
│       │   └── geoblock.py
│       ├── ingest/
│       │   ├── markets.py
│       │   ├── leaderboard.py
│       │   ├── wallets.py
│       │   └── orderbook.py
│       ├── scoring/
│       │   ├── wallet_score.py
│       │   ├── clv.py
│       │   └── metrics.py
│       ├── strategy/
│       │   ├── signal.py
│       │   ├── fair_value.py
│       │   └── filters.py
│       ├── risk/
│       │   ├── sizing.py
│       │   └── limits.py
│       ├── execution/
│       │   ├── paper.py
│       │   ├── live.py
│       │   └── orders.py
│       ├── backtest/
│       │   └── replay.py
│       └── reporting/
│           └── report.py
└── tests/
4. 환경변수

.env.example을 작성한다.

필수:

APP_ENV=local
LOG_LEVEL=INFO

DATABASE_URL=postgresql+psycopg://pm:pm@localhost:5432/pm_alpha_bot

POLY_GAMMA_HOST=https://gamma-api.polymarket.com
POLY_DATA_HOST=https://data-api.polymarket.com
POLY_CLOB_HOST=https://clob-v2.polymarket.com
POLY_WS_MARKET_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market

# Trading mode
ENABLE_LIVE_TRADING=false
PAPER_INITIAL_CAPITAL=1000
PAPER_FILL_MIN_DWELL_SEC=30
PAPER_QUEUE_MISS_BPS=25
REPLAY_FILL_MIN_DWELL_SEC=30
REPLAY_QUEUE_MISS_BPS=25

# Optional live trading secrets
POLY_PRIVATE_KEY=
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
POLY_ADDRESS=
POLY_FUNDER_ADDRESS=
POLY_SIGNATURE_TYPE=0
POLY_BUILDER_CODE=

# Strategy parameters
MIN_WALLET_CONSENSUS=3
MIN_EDGE_BPS=500
MAX_SPREAD_BPS=500
MAX_MARKET_EXPOSURE_PCT=0.03
MAX_CONDITION_EXPOSURE_PCT=0.03
MAX_EVENT_EXPOSURE_PCT=0.06
MAX_CATEGORY_EXPOSURE_PCT=0.15
MAX_DAILY_LOSS_PCT=0.02
MAX_TOTAL_DRAWDOWN_PCT=0.10
KELLY_FRACTION=0.25
LIVE_CANCEL_ALL_ON_KILL_SWITCH=true

# Data collection
LEADERBOARD_LIMIT=50
TRACKED_WALLET_LIMIT=100
ORDERBOOK_POLL_INTERVAL_SEC=15

중요:

.env는 .gitignore에 포함한다.
secret 값은 절대 테스트 fixture나 README 예시에 넣지 않는다.
live trading 관련 값이 비어 있으면 live command는 명확한 에러를 내고 종료한다.
5. DB 스키마

SQLAlchemy 모델과 Alembic migration을 작성한다.

markets

필드:

id
condition_id
market_id
event_id
question
category
slug
end_date
active
closed
neg_risk
min_tick_size
min_order_size
raw_json
created_at
updated_at
market_tokens

필드:

id
condition_id
token_id
outcome
side_label
created_at
updated_at
market_snapshots

필드:

id
ts
condition_id
token_id
best_bid
best_ask
midpoint
spread
last_trade_price
liquidity_score
raw_json

인덱스:

(token_id, ts)
(condition_id, ts)
wallets

필드:

id
proxy_wallet
username
source
first_seen
last_seen
is_active
raw_json
wallet_positions

필드:

id
proxy_wallet
condition_id
token_id
outcome
size
avg_price
current_price
current_value
cash_pnl
realized_pnl
total_pnl
updated_at
raw_json
wallet_activity

필드:

id
proxy_wallet
ts
condition_id
token_id
side
price
size
tx_hash
raw_json

인덱스:

(proxy_wallet, ts)
(condition_id, ts)
(token_id, ts)
wallet_scores

필드:

id
proxy_wallet
as_of
category
pnl
roi
win_rate
closed_market_count
trade_count
clv_1h
clv_6h
clv_24h
max_drawdown
profit_concentration
score
raw_metrics_json
signals

필드:

id
ts
condition_id
token_id
direction
market_midpoint
effective_entry_price
fair_prob
edge_bps
confidence
source_wallet_count
source_wallets_json
reason_json
status
orders

필드:

id
created_at
mode
condition_id
token_id
side
price
size
order_type
status
signal_id
external_order_id
reason_json
fills

필드:

id
order_id
ts
price
size
fee
tx_hash
raw_json
paper_positions

필드:

id
condition_id
token_id
side
size
avg_price
realized_pnl
unrealized_pnl
updated_at
6. CLI 요구사항

Typer 기반 CLI를 만든다.

엔트리포인트:

pm-bot --help

명령어:

6.1 DB
pm-bot db init
pm-bot db migrate
6.2 시장 수집
pm-bot ingest markets --limit 100 --active-only

기능:

Gamma API 또는 CLOB simplified markets를 통해 active market 수집
token id, condition id, question, category, end date 저장
중복 upsert
6.3 리더보드 수집
pm-bot ingest leaderboard --category OVERALL --time-period MONTH --limit 50
pm-bot ingest leaderboard --category POLITICS --time-period MONTH --limit 50
pm-bot ingest leaderboard --category CRYPTO --time-period MONTH --limit 50

기능:

Data API leaderboard 수집
proxyWallet, userName, pnl, vol, rank 등 가능한 필드 저장
category별 반복 실행 가능
6.4 지갑 데이터 업데이트
pm-bot ingest wallets --limit 100

기능:

저장된 wallet 목록에 대해 current positions, closed positions, activity, trades, total value를 수집
실패한 지갑은 retry/backoff
rate limit 고려
partial failure가 전체 실행을 죽이지 않게 처리
6.5 오더북 스냅샷 수집
pm-bot ingest orderbook --limit 100

기능:

저장된 active token에 대해 best bid/ask/midpoint/spread 저장
REST polling fallback 구현
WebSocket 수집기는 별도 command로 구현
pm-bot stream market --tokens-from-db --limit 50
6.6 지갑 스코어 계산
pm-bot score wallets --category OVERALL

기능:

지갑별 score 계산
단순 승률보다 ROI, PnL, CLV, 최근 성과, 거래 수, 드로다운, 수익 집중도 반영
결과를 wallet_scores에 저장
6.7 신호 스캔
pm-bot scan signals --category OVERALL

기능:

현재 active market과 wallet positions/activity를 비교
지갑 컨센서스 기반 신호 생성
liquidity/spread/risk filter 통과한 것만 signals 저장
console에 top signals 표시
6.8 Paper trading
pm-bot trade paper --once
pm-bot trade paper --loop --interval 30

기능:

signal을 읽고 paper order 생성
실제 주문 금지
maker-first simulation:
BUY는 best_bid와 best_ask 사이의 유리한 지정가를 제안
지정가가 곧바로 taker가 될 상황이면 주문하지 않거나 paper 상태를 would_take로 기록
체결 시뮬레이션:
이후 snapshot에서 해당 가격이 체결 가능해졌다고 판단될 때 fill 처리
단순화를 위해 MVP에서는 conservative fill model 사용
6.9 Live trading
pm-bot trade live --once

live trading 조건:

ENABLE_LIVE_TRADING=true
CLI에 --live 또는 trade live 명령 사용
private key/API credentials 존재
geoblock check passed
risk engine passed
DB connection healthy
latest orderbook snapshot exists
signal age가 너무 오래되지 않음

MVP에서 live execution은 최소 구현 또는 안전한 stub이어도 된다. 단, paper trading은 완성한다.

6.10 리포트
pm-bot report wallets --top 20
pm-bot report signals --top 20
pm-bot report paper

기능:

top wallet score
top signal
paper PnL
exposure
drawdown
signal hit/miss summary 출력
7. 클라이언트 구현
7.1 공통 HTTP 클라이언트

httpx.AsyncClient 기반으로 구현한다.

요구사항:

timeout 설정
retry/backoff
JSON parse error 처리
HTTP status error 처리
structured logging
response raw 저장 옵션
rate limit friendly delay 옵션
7.2 Gamma client

파일:

src/pm_alpha_bot/clients/gamma.py

기능:

list markets/events
active market discovery
token id 추출
market metadata normalization
7.3 Data API client

파일:

src/pm_alpha_bot/clients/data_api.py

기능:

leaderboard
current positions by user
closed positions by user
activity by user
trades by user
total value by user
positions for market
holders if available

반환값은 Pydantic model 또는 typed dict로 normalize한다.

7.4 CLOB public client

파일:

src/pm_alpha_bot/clients/clob_public.py

기능:

get orderbook
get orderbooks batch
get midpoint
get spread
get last trade price
get prices history
get CLOB market info
get server time
7.5 CLOB V2 trading client

파일:

src/pm_alpha_bot/clients/clob_v2.py

기능:

lazy import py-clob-client-v2
private key 없으면 import/initialization을 시도하지 않는다.
live trading이 비활성화된 상태에서는 주문 메서드 호출 시 명확한 에러를 낸다.
create limit order
cancel order
get open orders
get trades
SDK 필드명은 실제 설치된 패키지 기준으로 맞춘다.
V2에서 제거된 nonce, feeRateBps, taker를 사용하지 않는다.
7.6 WebSocket market client

파일:

src/pm_alpha_bot/clients/websocket_market.py

기능:

market channel subscribe
book
price_change
last_trade_price
best_bid_ask
reconnect
heartbeat/ping 관리
message를 snapshot 형태로 normalize하여 DB 저장
8. 지갑 스코어링 로직

파일:

src/pm_alpha_bot/scoring/wallet_score.py

단순 승률 70% 이상 필터를 메인 기준으로 쓰지 말고, 다음 스코어를 구현한다.

wallet_score =
  0.20 * normalized_pnl
+ 0.20 * normalized_roi
+ 0.15 * recent_performance_score
+ 0.15 * clv_score
+ 0.10 * category_specialization_score
+ 0.10 * drawdown_score
+ 0.10 * replicability_score
필수 계산 지표
pnl
roi
win_rate
closed_market_count
trade_count
recent_7d_pnl
recent_30d_pnl
clv_1h
clv_6h
clv_24h
max_drawdown
profit_concentration
category_specialization
replicability_score
필터

초기 watchlist에 남길 조건:

closed_market_count >= 30
trade_count >= 50
recent_30d_pnl >= 0
roi >= 0.03
profit_concentration <= 0.40
score >= 0.60

MVP에서는 데이터 부족으로 일부 지표가 계산 불가능할 수 있다. 그런 경우:

metric을 None으로 두지 말고 neutral score를 부여한다.
raw_metrics_json에 어떤 지표가 unavailable인지 기록한다.
README에 MVP limitation으로 명시한다.
CLV 계산

Closing Line Value는 다음 방식으로 근사한다.

지갑 activity에서 매수/매도 시점과 가격을 얻는다.
해당 token의 이후 1h/6h/24h midpoint snapshot을 찾는다.
BUY YES 기준:
clv = future_midpoint - entry_price
SELL 또는 NO는 방향성을 반전하여 계산한다.
snapshot이 없으면 unavailable 처리한다.
평균 CLV가 양수인 지갑에 가중치를 높인다.
9. 신호 생성 로직

파일:

src/pm_alpha_bot/strategy/signal.py

입력
active markets
latest orderbook snapshot
tracked wallet positions/activity
wallet scores
risk state
지갑 컨센서스

특정 token/condition에 대해 다음을 계산한다.

wallet_direction_score =
  sum(wallet_score_i * signed_position_delta_i * recency_weight_i)

조건:

최소 2개 이상의 우수 지갑이 같은 방향이어야 한다.
단일 지갑 신호만으로는 진입하지 않는다.
동일 지갑의 반복 주문은 중복 신호로 과대평가하지 않는다.
최근성 가중치:
1시간 이내: 1.0
6시간 이내: 0.7
24시간 이내: 0.4
그 이상: 0.1
Fair probability

다음 구조로 계산한다.

market_midpoint = (best_bid + best_ask) / 2

fair_prob =
  market_midpoint
+ wallet_alpha
+ momentum_alpha
- liquidity_penalty
- spread_penalty
- stale_data_penalty

MVP 기본값:

wallet_alpha = clamp(wallet_direction_score * 0.03, -0.08, 0.08)
momentum_alpha = clamp(recent_price_change_1h * 0.20, -0.03, 0.03)
liquidity_penalty = 0.00 ~ 0.03
spread_penalty = spread / 2
stale_data_penalty = 0.00 ~ 0.05

최종 fair_prob는 0.01~0.99로 clamp한다.

Edge

BUY YES 기준:

effective_entry_price = best_ask + estimated_taker_fee + slippage
edge = fair_prob - effective_entry_price

maker-first paper mode에서는:

maker_limit_price = min(best_ask - tick_size, market_midpoint)
edge = fair_prob - maker_limit_price

진입 조건:

edge_bps >= MIN_EDGE_BPS
spread_bps <= MAX_SPREAD_BPS
source_wallet_count >= MIN_WALLET_CONSENSUS
market is active
market is not closed
orderbook is fresh
risk engine passed

기본값:

MIN_EDGE_BPS=500
MAX_SPREAD_BPS=500
MIN_WALLET_CONSENSUS=3
MAX_CONDITION_EXPOSURE_PCT=0.03
MAX_EVENT_EXPOSURE_PCT=0.06
10. 리스크 엔진

파일:

src/pm_alpha_bot/risk/limits.py
src/pm_alpha_bot/risk/sizing.py

Position sizing

바이너리 계약 Kelly 근사:

kelly_fraction = (q - p) / (1 - p)

여기서:

q: fair probability
p: effective entry price

실제 주문 비중:

size_pct = max(0, kelly_fraction) * KELLY_FRACTION

기본:

KELLY_FRACTION=0.25

제한:

single_market_exposure <= 3% of capital
single_category_exposure <= 15% of capital
daily_loss_limit <= 2% of capital
total_drawdown_limit <= 10% of capital
single_order_min_size obeys market minimum order size
Kill switch

다음 상황에서는 모든 신규 주문을 중단한다.

daily loss limit 초과
total drawdown limit 초과
DB 오류 지속
orderbook stale
geoblock blocked
live trading credential invalid
signal generation exception 반복
paper/live broker에서 비정상 상태 발생
11. Paper execution

파일:

src/pm_alpha_bot/execution/paper.py

구현 목표:

signal을 받아 paper order 생성
maker-first conservative simulation
주문 상태:
created
resting
partially_filled
filled
cancelled
expired
rejected
would_take
fill rule:
BUY limit price >= future best_ask이면 체결로 간주하지 말고, 보수적으로 would_take 또는 crossed로 기록
maker fill은 future best_bid/ask 변화가 limit price를 지나간 경우에만 partial fill로 처리
MVP에서는 fill size를 작게 보수적으로 추정
수수료:
maker fee 0으로 처리
taker-like simulation이 발생하면 conservative fee를 반영
모든 order/fill은 DB에 저장
12. Live execution

파일:

src/pm_alpha_bot/execution/live.py

MVP에서는 다음 중 하나를 선택한다.

안전한 stub 구현
실제 SDK order creation까지 구현하되 기본 비활성화

필수 안전장치:

ENABLE_LIVE_TRADING=true 없으면 실행 금지
trade live 명령 외 경로에서 주문 금지
geoblock check 통과 필요
private key/API credential 존재 필요
order size가 risk engine 산출값 이하인지 검증
주문 직전 최신 orderbook 재조회
주문 직전 edge 재계산
주문 reason_json 저장
모든 live order는 GTC 또는 GTD limit order만 허용
MVP에서는 FOK/FAK/market order 금지
13. 백테스트/리플레이

파일:

src/pm_alpha_bot/backtest/replay.py

MVP 백테스트는 완전한 과거 orderbook 재구성이 아니라, 저장된 snapshot 기반 forward replay로 구현한다.

CLI:

pm-bot backtest replay --from 2026-04-01 --to 2026-04-22

기능:

지정 기간의 market_snapshots, wallet_activity, wallet_scores를 시간순으로 replay
lookahead bias 금지
각 timestamp에서 그 시점 이전 데이터만 사용
signal 생성
paper execution
PnL/edge/drawdown 출력

리포트:

total trades
win rate
average edge
realized PnL
unrealized PnL
max drawdown
exposure by category
top winning markets
top losing markets
source wallet attribution
14. 테스트 요구사항

최소 테스트를 작성한다.

Unit tests
config load
Data API response normalization
CLOB orderbook normalization
wallet score calculation
CLV calculation
fair probability calculation
edge calculation
Kelly sizing
risk limit rejection
paper fill simulation
Integration-like tests

respx로 HTTP mock을 사용한다.

leaderboard ingest
wallet positions ingest
orderbook ingest
signal scan
paper order creation
Safety tests
live trading disabled이면 live order 불가
missing private key이면 live order 불가
geoblock blocked이면 live order 불가
daily loss exceeded이면 신규 주문 불가
stale orderbook이면 신규 주문 불가
15. README 작성

README에는 다음을 포함한다.

프로젝트 목적
MVP 범위
전략 설명
왜 단순 승률 70% 지갑 추종이 위험한지
설치 방법
.env 설정 방법
Docker Postgres 실행 방법
DB migration 방법
데이터 수집 명령어
지갑 스코어링 명령어
신호 스캔 명령어
paper trading 명령어
backtest 명령어
live trading 안전장치
known limitations
다음 단계

README에서 명확히 적어라.

이 MVP는 기본적으로 paper trading 시스템이다.
live trading은 명시적으로 켜야 한다.
수익을 보장하지 않는다.
백테스트와 페이퍼 트레이딩에서 검증되지 않은 전략은 실거래하지 않는다.
Polymarket API/CLOB V2 변경 가능성을 고려하여 env override와 adapter 구조를 사용한다.
16. 구현 순서

작업은 다음 순서로 진행한다.

Phase 1: 프로젝트 골격
pyproject 작성
패키지 구조 생성
config 작성
logging 작성
DB session/models 작성
Alembic migration 작성
Docker Compose Postgres 작성
README 초안 작성
Phase 2: Public data clients
Gamma client
Data API client
CLOB public client
geoblock client
response normalization
ingest commands
Phase 3: Wallet scoring
leaderboard ingest
wallet ingest
score calculation
CLV approximation
report wallets
Phase 4: Signal engine
latest snapshot 조회
wallet consensus 계산
fair probability 계산
edge 계산
filters 적용
signals 저장
report signals
Phase 5: Paper trading
paper broker
risk engine
order/fill 기록
paper report
safety tests
Phase 6: WebSocket and backtest
market websocket stream
snapshot persistence
replay backtest
Phase 7: Live trading stub or guarded implementation
live command
geoblock check
credential validation
CLOB V2 client adapter
GTC/GTD limit order only
tests for disabled/default safety
17. 코딩 스타일
타입 힌트를 적극 사용한다.
public function에는 docstring을 작성한다.
복잡한 계산에는 작은 순수 함수와 테스트를 붙인다.
API response raw JSON은 필요한 경우 저장하되, core logic은 normalized model을 사용한다.
예외는 삼키지 말고 structured log로 남긴다.
retry는 idempotent GET 요청 중심으로 적용한다.
DB upsert를 사용해 반복 실행 가능하게 만든다.
CLI command는 실패 시 non-zero exit code를 반환한다.
secrets는 절대 로그에 출력하지 않는다.
18. 산출물 Definition of Done

아래가 모두 만족되면 MVP 완료로 본다.

pm-bot --help 동작
pm-bot db init 또는 migration 동작
pm-bot ingest markets --limit 20 --active-only 동작
pm-bot ingest leaderboard --category OVERALL --time-period MONTH --limit 20 동작
pm-bot ingest wallets --limit 20 동작
pm-bot ingest orderbook --limit 20 동작
pm-bot score wallets 동작
pm-bot scan signals 동작
pm-bot trade paper --once 동작
pm-bot report paper 동작
테스트 통과
ruff 통과
mypy에서 심각한 오류 없음
README 완성
.env.example 완성
live trading은 기본적으로 꺼져 있음
live trading disabled safety test 통과
19. 추가 구현 디테일
Upsert

DB 저장은 재실행 가능해야 한다.

market은 condition_id 기준 upsert
token은 token_id 기준 upsert
wallet은 proxy_wallet 기준 upsert
snapshot은 (token_id, ts) 기준 중복 방지
wallet score는 (proxy_wallet, as_of, category) 기준 저장
Snapshot freshness

기본 stale 기준:

snapshot age > 60 seconds => stale

stale이면 signal은 만들 수 있어도 order는 생성하지 않는다.

Spread bps
spread_bps = (best_ask - best_bid) * 10000

Polymarket 가격은 0~1 범위로 취급한다.

Edge bps
edge_bps = (fair_prob - effective_entry_price) * 10000
Conservative defaults

데이터가 부족하면 trading 하지 않는다.

예:

best_bid 없음
best_ask 없음
midpoint 없음
wallet score 없음
token id 없음
market closed
end date 지남
orderbook stale
spread 너무 큼

이 경우 skip reason을 reason_json에 남긴다.

20. 현재 작업 요청

지금 이 저장소에 위 MVP를 구현하라.

우선 Phase 1부터 Phase 5까지 완성하는 것을 목표로 한다.

Phase 6과 Phase 7은 시간이 부족하면 skeleton과 TODO, 안전한 stub까지만 구현해도 된다.

작업 중 다음을 반드시 수행하라.

먼저 현재 저장소 구조를 확인한다.
기존 코드가 있으면 보존하고 통합한다.
구현 계획을 간단히 세운다.
작은 단위로 파일을 만든다.
테스트를 작성한다.
가능한 테스트를 실행한다.
마지막에 변경 파일, 실행 방법, 남은 TODO를 요약한다.

중요:

실제 주문이 발생하지 않게 기본값을 paper trading으로 유지한다.
live trading은 안전장치 없이는 절대 구현하지 않는다.
실거래 주문 command가 실수로 실행되지 않도록 여러 조건을 둔다.
private key나 API secret이 없어도 paper mode는 정상 동작해야 한다.
