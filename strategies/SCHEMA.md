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
  "slippage_pct": 0.1,
  "fee_pct": 0.23
}
```

- `position_sizing` — `equal_weight` / `fixed_amount`(`amount_manwon`) / `fixed_qty`(`qty`)
- `fill_model` — `touch`(장중 가격이 닿으면 그 가격에 체결) / `next_open`(다음 봉 시가) / `close`(당일 종가)
- `fee_pct` — 왕복 수수료 + 증권거래세 근사값. 매도 시점에 일괄 차감

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
