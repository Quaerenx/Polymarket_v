# Polymarket Auto-Trading Bot 교차검증 리뷰

검토일: 2026-04-24
대상: `/root/PJ_AUTO_TRADE_BOT`
기준: `/root/CC_referi/CC.md` (시니어 퀀트 / 실행 엔지니어 / 백테스트 감사관 / 프로덕션 리스크 4역할 동시 수행)

---

## A. 전체 판단

**`paper trading만 권장`**

리포는 운영/리스크 장치(페이퍼 기본, 킬스위치, 지오블록, 잔고검증, post-only)가 잘 짜여 있고 코드 품질은 MVP 수준에서 준수합니다. 그러나 **전략 본체 (wallet alpha → fair probability → edge)** 는 구조적으로 자기참조(circular)이고, 백테스트·paper 시뮬레이터가 체결을 지나치게 낙관적으로 가정하며, leaderboard 기반 universe 구성이 **survivorship/selection bias**로 심하게 오염되어 있어 현 상태의 백테스트 결과를 "+EV 근거"로 받아들이면 안 됩니다. 소액 라이브는 최소 D/E/F 섹션 수정 후 재평가 대상입니다.

---

## B. 가장 치명적인 문제 Top 10

| # | 항목 | 심각도 | 왜 문제인지 | 실제 손실 메커니즘 | 수정 난이도 | 추천 수정안 |
|---|---|---|---|---|---|---|
| 1 | `edge_bps`가 구조적으로 `wallet_alpha`와 동어반복 ([fair_value.py:45-69](src/pm_alpha_bot/strategy/fair_value.py#L45-L69), [signal.py:111-126](src/pm_alpha_bot/strategy/signal.py#L111-L126)) | **Critical** | `fair_prob = midpoint + wallet_alpha + ...`, `entry_price = min(best_ask-tick, midpoint)`. 따라서 `edge = fair_prob - entry_price ≈ wallet_alpha + (midpoint - entry_price)`. "엣지"가 실제 EV가 아니라 wallet-direction 점수의 다른 표현. | 엣지 필터가 EV 필터 역할을 못 하고, 신호 강도를 두 번 곱해 과도한 진입을 유발. 비용(수수료·슬리피지·queue miss) 반영이 없어 장기 -EV. | Medium | fair_prob를 midpoint와 **독립**으로 산출(예: 유사 과거 구간의 실현확률, 또는 베이지안 사후분포). edge는 midpoint-independent 신호 강도만으로 정의. 수수료/슬리피지/queue-miss 기대비용을 **명시적 차감**. |
| 2 | Leaderboard 추적 wallet universe = survivorship+selection bias ([ingest/leaderboard.py](src/pm_alpha_bot/ingest/leaderboard.py), [scoring/wallet_score.py:32-80](src/pm_alpha_bot/scoring/wallet_score.py#L32-L80)) | **Critical** | 현재 상위 PnL wallet만 저장·추적. 과거에 상위였다가 사라진 지갑, 잔존 포지션만 좋아 보이는 지갑, 소수 이벤트 대박 지갑이 필터링되지 않음. `raw_json.closed_positions/trades`는 ingest 시점 스냅샷이므로 replay 백테스트에서도 **현재 시점 데이터로 과거를 판단**. | 백테스트가 인위적으로 좋게 보임. 실거래는 "어제의 위너"를 copy하는 유명한 -EV 전략. | Hard | (a) leaderboard 스냅샷을 **timestamped로** 보관, replay 시 해당 시점 leaderboard만 사용. (b) 지갑 universe를 PnL 외에 Sharpe, max trade contribution cap, 시계열 안정성으로 필터. (c) out-of-sample 재검증. |
| 3 | 백테스트/페이퍼 fill이 비현실적으로 낙관적 ([replay.py:176-210](src/pm_alpha_bot/backtest/replay.py#L176-L210), [execution/paper.py:34-52](src/pm_alpha_bot/execution/paper.py#L34-L52)) | **Critical** | maker 주문을 `best_ask <= order.price`면 **즉시 전량/반량 체결**로 간주. queue position, 체결 확률, 반대편 취소/갱신을 전혀 모델링 안 함. fee=0. | 백테스트 수익이 거의 전부 이 가정에서 나옴. 실제로는 maker 주문이 체결되지 않거나 adverse selection 상태에서만 체결 → 실거래 PnL이 백테스트와 크게 괴리. | Medium | (a) 최소 fee 모델(Polymarket fee + gas + 환산 slippage) 추가. (b) maker 체결 확률을 best_ask가 price를 **지속 유지한 시간/스프레드/거래량**으로 확률적으로 샘플. (c) pessimistic / optimistic fill 두 시나리오 동시 리포팅. |
| 4 | Wallet 감지 지연이 알파를 전부 소모 ([signal.py:47-73](src/pm_alpha_bot/strategy/signal.py#L47-L73), `signal_activity_lookback_hours=72`, refresh는 `pm-bot ops refresh-data` cron) | **High** | activity 데이터는 data-api polling(15s~분 단위) + refresh cycle 주기 의존. 기본 lookback이 72시간. 즉 이미 2~3일 지난 trade 두 건으로 consensus=2가 성립되어 진입 가능. | 가격이 이미 해당 정보 반영 이후 진입 → 후행 추종 → 계통적 -EV. copy-crowding과 결합 시 악화. | Medium | (a) lookback를 수 시간 이내로 줄이고 consensus 기준을 더 높이기. (b) recency_weight를 지수감쇠로 교체하고 임계치를 부과. (c) websocket 기반 실시간 wallet-activity 트리거로 전환. |
| 5 | `MIN_EDGE_BPS=500`·`wallet_alpha` 최대 8% → 임계값이 사실상 wallet consensus + midpoint gap 필터 | **High** | wallet_direction_score가 8%에 close되어 edge≈5~8%면 자동 통과. 손익분기 edge는 (수수료+슬리피지+queue miss 확률 × 평균 반사거리)여야 하는데 이 계산이 없음. | 명목 임계 통과해도 순 EV 음수인 진입을 반복. | Easy | Break-even edge를 시뮬레이션 기반으로 산출하고 `MIN_EDGE_BPS`를 그 위에 **마진**으로 덮어씌움. |
| 6 | Replay가 현재-시점 wallet 데이터를 과거에 사용 ([scoring/wallet_score.py:55-128](src/pm_alpha_bot/scoring/wallet_score.py#L55-L128)) | **High** | 점수 계산이 `wallet.raw_json.closed_positions/trades/total_value`에 의존. ingest 때 overwrite 되는 최신 blob을 사용 → 과거 점수 재계산 시 미래 정보가 섞임. | 백테스트 성과가 lookahead로 인해 실제보다 좋게 나옴 → 실거래 배포 시 실망. | Hard | wallet activity/position의 **time-series 이력 테이블**을 만들어 점수 계산을 해당 시점 snapshot에 한정. |
| 7 | 동일 token·동일 방향 신호 재진입/중복 주문 방지 부족 ([execution/live.py:636-657](src/pm_alpha_bot/execution/live.py#L636-L657), [execution/paper.py:54-118](src/pm_alpha_bot/execution/paper.py#L54-L118)) | **High** | `_next_live_signal`은 status='new' 중 첫 signal만 가져오며 포지션/열린주문과 cross-check 없음. paper도 token_id 기준 포지션/주문 중복 체크 없음(backtest에는 있음 [replay.py:140-143](src/pm_alpha_bot/backtest/replay.py#L140-L143)). 재시도·refresh 중복에서 duplicate order 가능. | 예상보다 큰 포지션, 카테고리 노출, 자본 소진. client_order_id/idempotency key 부재가 결정적. | Medium | (a) signal 생성 시 `client_order_id = hash(token_id|direction|ts|signal_id)` 부여, CLOB 제출 시 idempotency로 사용. (b) `prepare_live_order`·paper 양쪽에서 "이미 열린 주문/포지션 있으면 skip" 검증. |
| 8 | `confidence = wallet_direction_score / len(source_wallets)`가 사이즈 계산에 미반영 ([signal.py:135](src/pm_alpha_bot/strategy/signal.py#L135), sizing.py) | **Medium** | Kelly는 `(fair_prob-entry)/(1-entry)`만 사용. confidence가 0.1이든 0.9든 동일 사이즈. | 약신호·강신호를 똑같이 크게 잡아 분산을 키움. | Easy | `size_pct *= confidence` 또는 fair_prob 자체를 신뢰도로 shrinkage. |
| 9 | 드로우다운/데일리 손실 기준이 **realized 기반**, unrealized 무시 ([replay.py:283-286](src/pm_alpha_bot/backtest/replay.py#L283-L286), [risk/limits.py:33-36](src/pm_alpha_bot/risk/limits.py#L33-L36)) | **Medium** | Polymarket 포지션은 장기 hold가 많아 unrealized loss가 실제 자본의 대부분. realized만 보는 daily_loss_pct/drawdown은 늦게 트립됨. | 실거래 자본이 half 되어도 리스크엔진이 건재함. | Medium | equity 기반 drawdown / unrealized 포함 daily PnL. mark-to-market은 이미 구현되어 있으므로 지표만 교체. |
| 10 | wallet score 정규화가 universe 전체 min-max → 소수 지갑 샘플에서 비안정 ([scoring/wallet_score.py:201-220](src/pm_alpha_bot/scoring/wallet_score.py#L201-L220)) | **Medium** | min_max_normalize는 universe가 30~50개면 한 개 outlier가 전체 분포를 지배. 또한 신규 wallet 등장 시 기존 점수 모두 리스케일 → threshold 기반 eligibility가 요동. | signal_min_wallet_score 통과/탈락이 지표가 아닌 universe 구성에 흔들려 비일관적 진입. | Easy | (a) rolling-window percentile 기반. (b) outlier winsorize(상/하위 5%). (c) eligibility를 절대 기준과 상대 기준 이중 적용. |

---

## C. 숨은 가정과 착시

- leaderboard 상위 지갑은 미래에도 +EV이다 (`[가정 필요]`, 검증 없음)
- wallet activity polling 지연 < 가격 반응 속도 (`[가정 필요]`)
- midpoint가 fair value의 비편향 추정이다 (심한 toxic flow가 없는 시장에서도)
- midpoint 기반 fair_prob + wallet_alpha가 **독립 정보**이다 (실제로는 둘 다 같은 wallet 행동에 영향받음)
- maker 주문이 post-only로 거부되지 않는 한 예상대로 체결된다 (queue position, cancel cascade 무시)
- 저장된 snapshot ≈ 실시간 orderbook (polling 간격 15초, burst에 대응 못 함)
- 미체결/부분체결/재시도가 거의 일어나지 않는다 (paper는 50% 체결 규칙, replay는 100%)
- Polymarket fee/gas ≈ 0 (paper/backtest 모두 fee=0)
- 지갑 행동이 카테고리·상품에 무관하게 일반화된다 (`category_specialization` weight 10%로 약함)
- recent_30d_pnl이 leaderboard.pnl과 동일하다 ([wallet_score.py:104-108](src/pm_alpha_bot/scoring/wallet_score.py#L104-L108)) — 실제로는 leaderboard.pnl이 전체 기간일 수 있어 recent signal로 쓸 수 없을 수 있음 `[가정 필요]`
- Polymarket CLOB v2 `post_only` 보장이 실제로 enforce 된다 (API 측 에러 처리가 아닌 silent cross 시 손실)

---

## D. 백테스트 신뢰성 감사

| 항목 | 판정 | 근거 |
|---|---|---|
| Lookahead bias | **존재** | wallet_score의 raw_json은 ingest 시점 최신 blob. 과거 `as_of`로 scoring 재계산해도 **과거가 아닌 현재의 closed_positions / trades**를 사용. 또한 CLV는 의도적 future lookahead지만, 그 future-CLV가 wallet 점수 → wallet eligibility → 과거 신호 생성으로 역류 가능. |
| Survivorship bias | **심각** | leaderboard 호출이 "현재 시점 상위"만 반환. 폭망한 과거 상위 지갑은 tracked_wallets에 없음. |
| Fill optimism | **심각** | replay는 `best_ask <= price`이면 즉시 전량 체결; paper도 50% 또는 전량 체결 가정. queue, cancel, partial, fee 모두 0. |
| Fee/slippage 누락 | **누락** | `fee=0.0` ([paper.py:166](src/pm_alpha_bot/execution/paper.py#L166)), replay에도 fee 필드 없음. Polymarket 실거래 수수료·gas 환산 미반영. |
| Leakage 가능성 | **있음** | (a) wallet.raw_json 최신 데이터, (b) 최신 snapshot이 `as_of`로 필터되긴 하지만 snapshot이 retrospective backfill된 경우 ts 신뢰 필요 `[가정 필요]`. |
| Point-in-time correctness | **부분적** | snapshot/signal/wallet_score는 as_of 필터가 있으나, wallet universe 자체와 raw_json이 PIT 아님. |
| 결과를 믿어도 되는가 | **불가** | 위 5가지가 모두 상승방향으로 편향되어 있어 리포트 상 성과는 체계적 과대평가 가능. |

---

## E. 전략 개선안 (기대값 개선 가능성 순)

1. **Fee/slippage/queue-miss를 명시적 비용 모델로** — 각 진입마다 `expected_cost = fee + bid_ask_impact + P(no-fill)×opportunity_cost` 차감.
   - 왜 도움: 손익분기 edge가 드러나 -EV 진입 차단.
   - 난이도: 쉬움 / 기대 개선: 큼 / 선행: Polymarket 실제 수수료·gas 측정값.
2. **fair_prob를 midpoint와 분리** — wallet_alpha만 독립 알파로 두고 entry_price 대비 얼마나 유리한지로 edge 재정의.
   - 왜 도움: 현재 edge의 자기참조 제거.
   - 난이도: 중간 / 기대 개선: 큼 / 선행: historical wallet-alpha → 실현확률 mapping 자료.
3. **Leaderboard PIT 스냅샷 테이블** — leaderboard ingest마다 timestamped 저장, replay가 이를 사용.
   - 왜 도움: survivorship 제거, 백테스트 신뢰도 회복.
   - 난이도: 어려움 / 기대 개선: 큼.
4. **Wallet universe 안정성 필터** — trade count ≥ 50, Sharpe ≥ X, top-trade contribution ≤ 25%, 30일 연속 eligible.
   - 왜 도움: 잭팟형 지갑 제거, 재현성 개선.
   - 난이도: 쉬움 / 기대 개선: 중간.
5. **진입 신호 재현 실험 (OOS)** — 현재 전략으로 과거 구간 out-of-sample replay(PIT 수정 후).
   - 왜 도움: "백테스트가 현실적으로 +EV인지" 최초로 검증.
   - 난이도: 중간 / 기대 개선: 결정력 큼.
6. **Maker fill 확률 모델** — snapshot 사이 시간 동안 best_ask가 price 아래 머문 지속시간 비율로 체결 확률 샘플.
   - 왜 도움: paper/replay 성과를 실거래와 정렬.
   - 난이도: 중간 / 기대 개선: 큼.
7. **SELL/NO 토큰 side 처리** — 현재 BUY only. Polymarket YES/NO 두 토큰 구조에서 반대 signal을 반대 토큰 BUY로 변환.
   - 왜 도움: 알파의 절반을 회수.
   - 난이도: 중간 / 기대 개선: 중간.
8. **Confidence·size 연결** — size_pct에 confidence를 곱해 약신호 축소.
   - 왜 도움: 잡신호에 덩치 큰 포지션 금지.
   - 난이도: 쉬움 / 기대 개선: 중간.
9. **Regime-aware threshold** — 스프레드·유동성별로 `min_edge_bps`를 다르게.
   - 왜 도움: 저유동성에서 큰 edge 요구, 고유동성에선 더 촘촘.
   - 난이도: 중간 / 기대 개선: 중간.
10. **Equity 기반 drawdown/kill-switch** — unrealized 포함 daily PnL / drawdown 트리거.
    - 왜 도움: 실제 자본 훼손에 대응.
    - 난이도: 쉬움 / 기대 개선: 안전성 큼.

---

## F. 당장 해야 할 수정 순서 (Day-1 ~ Week-2)

1. **Paper/Replay에 Polymarket fee + 기본 slippage 반영** ([paper.py:166](src/pm_alpha_bot/execution/paper.py#L166), [replay.py:213-252](src/pm_alpha_bot/backtest/replay.py#L213-L252)).
2. **Replay fill을 확률적으로** — `best_ask <= price`만으로 전량체결 금지. 최소한 pessimistic(전혀 안 찬다) vs optimistic 두 리포트 출력.
3. **Wallet universe를 PIT로** — leaderboard ingest 스냅샷 테이블 추가, replay가 해당 시점 universe만 사용.
4. **edge 재정의** — wallet_alpha가 midpoint에 직접 더해지는 구조 분리, `edge = wallet_alpha - expected_cost`.
5. **Live idempotency / 중복 포지션 방지** — `client_order_id` 전파, `prepare_live_order`에서 기존 포지션/주문 체크.
6. **Drawdown/daily-loss을 unrealized 포함 equity 기준으로** 리라이트.
7. **`signal_activity_lookback_hours` 기본값 축소 + recency를 지수감쇠로**, `MIN_WALLET_CONSENSUS` 상향.

---

## G. 추가 실험 제안

| 실험 | 가설 | 방법 | 성공 기준 |
|---|---|---|---|
| Consensus sweep | consensus↑ → 신호수↓·품질↑ | consensus ∈ {1,2,3,4,5}로 replay, Sharpe/hit-rate/turnover 비교 | Sharpe 단조증가 + hit-rate 개선이 같은 방향 |
| Category 분해 | wallet alpha 효과가 카테고리 의존 | 카테고리별 Sharpe, avg realized edge | 카테고리 간 유의미한 차이 존재 / 일부 negative면 제외 규칙 |
| Signal-delay sensitivity | 감지 지연 >N분이면 alpha 소멸 | activity를 일부러 1/5/15/60분 지연시킨 replay | 지연 ≥X분에서 EV 0 이하 확인, 운영 SLA 산출 |
| Maker-fill pessimistic vs optimistic | 현재 백테스트 성과의 상당 부분이 fill 가정에서 옴 | 체결 확률 0%, 10%, 50%, 100% 네 시나리오 | pessimistic에서도 +EV 유지 여부가 go/no-go 기준 |
| Edge threshold sweep | 손익분기 edge가 임계치보다 높을 수 있음 | `min_edge_bps ∈ {200,500,800,1200,1600}` | fee·slippage 반영 후 최대 Sharpe 임계 찾기 |
| Kelly sweep | 현재 0.25는 근거 없음 | `kelly_fraction ∈ {0.05,0.10,0.15,0.25,0.5}` + bootstrap drawdown | drawdown 허용 상한 안에서 기대 log-growth 최대 |
| OOS 검증 | in-sample과 out-of-sample 성과 차이 | 시간을 split, train에서 임계 고정, test에서 성과 측정 | OOS Sharpe ≥ 0.5 × IS Sharpe 여야 신뢰 가능 |
| Copy-crowding stress | 동일 signal을 다수 봇이 추종하여 slippage 확대 | 진입가를 best_ask 쪽으로 1~3 tick 악화시켰을 때 EV | 1tick 악화로 EV 반감 시 maker-only 고수, 아니면 take 허용 고려 |

---

## H. 마지막 결론

현재 이 봇은 **운영/안전 프레임워크 측면에서는 페이퍼 단계로는 합격**이지만, **전략·데이터·백테스트 계층에서는 "+EV"를 주장할 근거가 없습니다.** 구체적으로:

- `edge`는 wallet 신호의 리패키징에 가깝고, 수수료·미체결 비용이 빠져 있어 실전 break-even을 넘는다는 증거가 없습니다.
- Leaderboard 추적 자체가 survivorship·copy-crowding·signal-lag 3중 악재에 노출되어 있고, 이걸 **replay가 보정하지 않음** → 백테스트 결과는 과대평가.
- Paper/replay의 체결 가정이 비현실적이어서, paper 수익이 "전략이 작동한다"의 증거가 될 수 없습니다.

**실거래 검토 가능한 조건**(동시에 충족해야 함):

1. F 섹션 1~4번(fee/fill 모델, PIT universe, edge 재정의, idempotency)이 머지되고
2. G 섹션 "Maker-fill pessimistic"과 "OOS 검증"에서 pessimistic fill 가정에서도 fee 반영 후 Sharpe > 0.5, OOS hit-rate 저하 ≤30% 를 입증하며
3. Live supervisor의 kill-switch를 realized가 아닌 unrealized-포함 equity drawdown에 물리고
4. 최소 4주 이상 paper에서 신호 생성/체결/손익이 백테스트 기대범위 내에 있는지 드리프트 없이 확인한 뒤

그때 비로소 `LIVE_INITIAL_CAPITAL`을 총자본의 1% 이하로 제한한 **소액 실거래 테스트**를 고려할 수 있습니다. 그 이전 단계에서의 실거래는 구조적으로 손실을 태우는 실험이 될 가능성이 높습니다.
