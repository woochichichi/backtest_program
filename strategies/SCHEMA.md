# 전략 DSL v1 (`krx-backtest-strategy/v1`)

백테스트 엔진과 AI 생성기가 공유하는 전략 표현 포맷. 사람이 손으로 쓸 수도 있고,
LLM이 자연어를 이 JSON으로 변환해 그대로 실행할 수도 있다.

> AI에게 전략을 만들게 할 때는 이 문서 전체를 시스템 프롬프트에 넣고
> "출력은 `krx-backtest-strategy/v1` JSON 하나만" 이라고 지시한다.

---

## 최상위 구조

| 키 | 필수 | 설명 |
|---|---|---|
| `schema` | O | 고정값 `"krx-backtest-strategy/v1"` |
| `id` | O | 파일명과 일치하는 슬러그 (`strategy1`) |
| `name` | O | 화면에 표시될 이름 |
| `description` | | 자연어 설명. AI 변환 시 원문 프롬프트를 그대로 넣어도 된다 |
| `market` | O | 대상 시장·봉 주기 |
| `universe` | O | 종목 선정 규칙 |
| `entries` | O | 진입 규칙 배열 (분할 매수는 원소를 여러 개) |
| `exits` | O | 청산 규칙 배열 |
| `exit_priority` | | 같은 봉에서 여러 청산이 동시 성립할 때 우선순위. 기본 `["SL","TP"]` (보수적) |
| `indicators` | | 차트에 그릴 지표 목록 |
| `portfolio` | O | 자본·분산 |
| `execution` | O | 체결 모델·비용 |
| `period` | O | 백테스트 구간. `end: "auto"`면 데이터 최신 거래일 |

---

## `market`

```json
{ "country": "KR", "asset": "stock", "bar": "1d", "trade_resolution": "1m" }
```

- `bar` — 신호 판정 봉. 현재 `1d`만 지원
- `trade_resolution` — 체결 판정 해상도. `1m`(장중 터치 기준) 또는 `1d`(일봉 근사)

---

## `universe` — 종목 선정

```json
{
  "markets": ["KOSPI", "KOSDAQ"],
  "exclude": ["ETF", "ETN", "SPAC", "PREFERRED", "ADMIN_ISSUE", "TRADE_HALT"],
  "reference_day": {
    "rule": "amount_spike",
    "lookback_days": 20,
    "spike_amount_krw_eok": 1000,
    "prev_day_amount_max_eok": 200
  },
  "condition": "close_below_reference_open",
  "valid_days_after_reference": 60
}
```

### `reference_day.rule`

| 값 | 의미 |
|---|---|
| `amount_spike` | 거래대금이 `spike_amount_krw_eok` 이상인 날 = 기준일 |
| `volume_spike` | 거래량 배수 기준 (`volume_mult`, `volume_ma_period`) |
| `range_breakout` | N일 신고가 돌파일 |
| `none` | 기준일 개념 없이 전 종목 대상 |

`prev_day_amount_max_eok`는 "기준일 직전 영업일의 거래대금이 이 값 이하" 필터.
조건을 만족하는 날이 여러 개면 **가장 최근 날**을 기준일로 채택한다.

### `condition` — 기준일 이후 감시 조건

| 값 | 의미 |
|---|---|
| `close_below_reference_open` | 기준일 시가까지 하락한 종목만 진입 대상 |
| `close_below_reference_low` | 기준일 저가까지 하락 |
| `pullback_pct` | 기준일 종가 대비 N% 하락 (`pullback_pct` 필드 필요) |
| `none` | 조건 없음 |

`valid_days_after_reference` — 기준일 이후 이 영업일 안에 진입이 안 되면 후보 폐기.

---

## `entries` / `exits` — 규칙 객체

```json
{
  "id": "B2",
  "label": "2차 매수",
  "requires": ["B1"],
  "trigger_pct": -10,
  "when": { "op": "<=", "left": "low", "right": { "expr": "fill.B1.price * (1 + trigger_pct/100)" } },
  "price": { "expr": "fill.B1.price * (1 + trigger_pct/100)" },
  "size_pct": 50,
  "size_of": "planned_position"
}
```

| 키 | 설명 |
|---|---|
| `id` | 규칙 식별자. `fill.<id>.price` 로 다른 규칙에서 참조 |
| `requires` | 선행 체결이 필요한 규칙 id 배열 |
| `when` | 조건식 (아래 참조) |
| `price` | 체결가. 지정 안 하면 조건 성립 봉의 종가 |
| `size_pct` | 비중 % |
| `size_of` | `planned_position`(계획 포지션 대비) / `equity`(총자산 대비) / `position`(현재 보유 대비, 청산용) |
| `type` | 청산 전용: `take_profit` / `stop_loss` / `trailing_stop` / `time_exit` |

### `when` 조건식

```json
{ "op": "<=", "left": <피연산자>, "right": <피연산자> }
```

`op`: `>` `>=` `<` `<=` `==` `cross_above` `cross_below`

복합 조건은 중첩:

```json
{ "op": "and", "conditions": [ {...}, {...} ] }
{ "op": "or",  "conditions": [ {...}, {...} ] }
{ "op": "not", "condition": {...} }
```

### 피연산자 종류

| 형태 | 예 | 의미 |
|---|---|---|
| 봉 필드 | `"open" "high" "low" "close" "volume" "amount"` | 당일 값 |
| 기준일 참조 | `"ref.open" "ref.close" "ref.amount"` | 기준일 봉 값 |
| 체결 참조 | `"fill.B1.price"` | 해당 규칙 체결가 |
| 포지션 참조 | `"position.avg_price" "position.pnl_pct" "position.hold_days"` | 현재 포지션 상태 |
| 지표 | `{"indicator":"SMA","period":45,"source":"close"}` | 지표 값 |
| 수식 | `{"expr":"position.avg_price * 1.1"}` | 산술식 (`+ - * / ( )` 와 위 참조들) |
| 상수 | `10` `"2026-01-01"` | 리터럴 |

---

## `indicators` — LAG 지표

`type`으로 지정. 모두 후행(lagging) 계열이며 `plot: true`면 차트에 그린다.

| type | 파라미터 | 설명 |
|---|---|---|
| `SMA` `EMA` `WMA` | `period`, `source` | 이동평균 |
| `BBANDS` | `period`, `stddev` | 볼린저 밴드 (`.upper` `.middle` `.lower`) |
| `RSI` | `period` | 상대강도지수 |
| `MACD` | `fast`, `slow`, `signal` | (`.macd` `.signal` `.hist`) |
| `STOCH` | `k`, `d`, `smooth` | 스토캐스틱 (`.k` `.d`) |
| `ATR` | `period` | 평균 실체범위 |
| `ADX` | `period` | 추세 강도 (`.adx` `.pdi` `.mdi`) |
| `CCI` | `period` | 상품채널지수 |
| `OBV` | — | 누적 거래량 |
| `VWAP` | `anchor` | 거래량가중평균 |
| `ENVELOPE` | `period`, `pct` | 이격도 밴드 |
| `DONCHIAN` | `period` | 채널 (`.upper` `.lower`) |

조건식에서 서브 필드는 `field`로 지정한다:

```json
{ "indicator": "BBANDS", "period": 20, "stddev": 2, "field": "lower" }
```

새 지표를 추가하려면 `engine/indicators.py`에 함수 하나를 등록하고 이 표에 한 줄 추가한다.

---

## `portfolio` / `execution`

```json
"portfolio": {
  "initial_capital_manwon": 10000,
  "max_positions": 10,
  "position_sizing": "equal_weight",
  "allow_duplicate_symbol": false
},
"execution": {
  "resolution": "1m",
  "fill_model": "touch",
  "same_day_exit": "loss_only",
  "slippage_pct": 0.1,
  "fee_pct": 0.23
}
```

- `position_sizing` — `equal_weight` / `fixed_amount`(`amount_manwon`) / `fixed_qty`(`qty`)
- `fill_model` — `touch`(장중 가격이 닿으면 그 가격에 체결) / `next_open`(다음 봉 시가) / `close`(당일 종가)
- `fee_pct` — 왕복 수수료 + 증권거래세 근사값. 매도 시점에 일괄 차감

### `same_day_exit` — 진입 당일 청산을 인정할 것인가

지금 백테스트는 **일봉만** 쓴다. 일봉에는 시가·고가·저가·종가 네 값만 있고
**하루 안에서 저가와 고가 중 무엇이 먼저였는지는 기록되어 있지 않다.**

예를 들어 어떤 날 봉이 `시가 265,000 / 고가 293,500 / 저가 259,000` 이라고 하자.
매수 목표가가 259,000, 익절가가 285,000 이면 둘 다 그날 안에 닿긴 했다. 하지만

- 저가를 먼저 찍고 반등해 고가를 찍었다면 → 매수 후 익절 성립 (수익)
- 고가를 먼저 찍고 흘러내려 저가를 찍었다면 → 매수만 되고 익절은 성립하지 않음

이 둘을 일봉으로는 구분할 수 없다. 앞쪽이라고 가정하면 성과가 실제보다 좋게 나온다.
`same_day_exit` 은 이 애매한 봉을 어떻게 처리할지 정한다.

| 값 | 동작 | 언제 쓰나 |
|---|---|---|
| `loss_only` **(기본)** | 진입 체결이 있었던 날에는 **이익이 나는 청산을 전부 차단**한다. 손실·본전 청산만 당일에 허용하고, 이익 청산은 다음 거래일부터 평가한다 | 일봉 백테스트의 권장값. 유리한 쪽만 미루므로 결과가 보수적으로 나온다 |
| `never` | 진입 당일에는 어떤 청산도 평가하지 않는다 | 가장 보수적. 하루 최소 보유를 강제하고 싶을 때 |
| `always` | 진입 당일에도 익절·손절을 모두 평가한다 | 1분봉이 연결된 뒤에 쓸 값. 지금 쓰면 **낙관 편향**이 생긴다 |

판정 기준은 **그 종목의 마지막 진입 체결일**이다.
B1만 체결된 상태에서 다음 날 B2가 체결되면, B2 체결일에도 같은 규칙이 다시 적용된다.

#### `loss_only` 는 규칙 이름이 아니라 **체결가**로 판정한다

`loss_only` 는 `type` 을 보지 않는다. 그 청산을 실제로 실행했을 때
**슬리피지·수수료까지 뺀 순손익**을 그날까지의 평단가와 비교해서

- 순손익이 **이익** → 차단하고 다음 거래일로 이월
- 순손익이 **손실 또는 본전** → 당일 허용

이렇게 갈린다. `type: "take_profit"` 이냐 `type: "stop_loss"` 냐는 판정에 쓰지 않으며,
`type` 이 없는 커스텀 규칙도 같은 기준으로 자동 처리된다.

**왜 이름으로 판정하면 안 되나 — 전략1의 45일선 손절이 그 사례다.**
전략1의 `SL` 은 "저가가 45일 이동평균선에 닿으면 청산"이다. 그런데 기준일 시가까지 눌린 종목은
**진입가가 45일선보다 아래에 있는 경우가 흔하다.** 이때 손절선은 진입가 *위*에 놓이고,
매수와 동시에 손절 조건이 성립하면서 **평단보다 비싼 값에 팔린다.**
라벨은 손절인데 동작은 익절인 것이다.

실측(2025-01-02 ~ 2026-07-23)에서 진입 당일 청산 333건 중 **159건이 이 경우**였다.
예: 링크제니시스 평단 6,356 → SL 체결 6,446 (+1.18%).
`type` 기반으로 판정했다면 이 159건이 전부 "손절이니까 당일 허용"으로 통과해서,
막으려던 낙관 편향이 그대로 남았을 것이다.

> **분봉 매매는 추후 지원 예정이다.** marcap 은 일봉만 제공하므로 `resolution: "1m"` 을 지정해도
> 현재는 일봉 근사로 동작한다. 1분봉 데이터가 연결되면 순서를 실제로 알 수 있으므로
> `same_day_exit` 을 `always` 로 되돌리면 된다.

백테스트 결과의 `assumptions` 블록에 실제로 적용된 값과 사람이 읽을 수 있는 설명(`notes`),
그리고 아래 통계가 함께 담겨 나온다.

| `assumptions.stats` | 의미 |
|---|---|
| `ambiguous_bars` | 진입과 청산이 같은 봉 안에서 모두 성립해 **순서를 알 수 없었던** 봉의 수. 많을수록 결과가 가정에 크게 의존한다 |
| `same_day_profit_exits_blocked` | 그중 `loss_only`/`never` 규칙 때문에 **당일 이익 청산이 차단되어** 다음 거래일로 넘어간 건수. 이 옵션이 실제로 얼마나 작동했는지를 보여준다 |
| `same_day_entry_exit` / `_pct` | 진입일과 청산일이 같은 거래 수와 비중 |

---

## AI 생성 시 규칙

1. 출력은 JSON 객체 **하나**. 코드펜스·설명 문장 금지
2. 모르는 값은 만들지 말고 이 문서의 기본값을 쓴다
3. 분할 매수/매도는 `entries`/`exits` 배열 원소를 늘려서 표현한다 (별도 필드 만들지 않음)
4. 원문 자연어는 `description`에 그대로 보존한다 (나중에 재변환·검증용)
5. 생성 후 `engine/validate.py`로 스키마 검증을 통과해야 등록된다

### 프롬프트 템플릿

```
아래는 전략 DSL v1 명세다.
<SCHEMA.md 전문>

사용자 전략 설명:
"""
{사용자 입력}
"""

위 설명을 DSL v1 JSON 하나로 변환해라. 설명 없이 JSON만 출력한다.
```
