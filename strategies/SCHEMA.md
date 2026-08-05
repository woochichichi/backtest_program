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
| `params` | | **UI 전용** 조절 가능 파라미터 선언. 엔진은 읽지 않는다 (아래 참조) |
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
| `custom` | `when` 조건식을 그대로 평가해 성립한 날 = 기준일 (아래 참조) |
| `none` | 기준일 개념 없이 전 종목 대상 |

#### `rule: "custom"` — 조건식으로 기준일 정의하기

`amount_spike` 같은 고정 규칙으로 표현할 수 없는 기준일은 `custom` + `when` 으로 쓴다.
`when` 은 진입/청산과 **완전히 같은 조건식 문법**이다.

```json
"reference_day": {
  "rule": "custom",
  "lookback_days": 20,
  "spike_amount_krw_eok": 1000,
  "volume_mult": 5,
  "close_change_min_pct": 15,
  "when": { "op": "and", "conditions": [
    { "op": ">=", "left": "amount", "right": { "expr": "spike_amount_krw_eok * 100000000" } },
    { "op": ">=", "left": "volume", "right": { "expr": "VMA20 * volume_mult" } },
    { "op": ">=", "left": "close",  "right": { "expr": "prev.close * (1 + close_change_min_pct / 100)" } },
    { "op": ">=", "left": "close",  "right": "HIGH252" }
  ]}
}
```

- `reference_day` 안의 **숫자 필드는 그대로 조건식의 이름**으로 쓸 수 있다
  (위의 `spike_amount_krw_eok`, `volume_mult`, `close_change_min_pct`).
  이렇게 두면 `params` 가 그 필드를 가리켜 화면에서 조절할 수 있다.
- `VMA20` / `HIGH252` 는 `indicators` 에 선언한 **지표 별칭**이다 (아래 "지표 별칭" 참조).
- **`expr` 안에서 함수 호출은 쓸 수 없다.** `sma(volume, 20)` 같은 표기는 지원하지 않는다.
  이동평균이 필요하면 `indicators` 에 별칭으로 선언하고 그 이름을 쓴다. 보안상 수식 파서가
  함수 호출 노드를 통째로 거부하기 때문이다.

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

### `universe.filters` — 종목 스크리닝

```json
"filters": {
  "market_cap_min_eok": 2000,
  "debt_ratio_max_pct": 200
}
```

엔진이 적용할 수 있는 키:

| 키 | 의미 |
|---|---|
| `market_cap_min_eok` / `market_cap_max_eok` | 시가총액 하한 / 상한 (억) |
| `price_min` / `price_max` | 주가 하한 / 상한 (원) |
| `amount_min_eok` | 거래대금 하한 (억) |
| `volume_min` | 거래량 하한 (주) |

DART 재무 데이터(`dart/fundamentals-YYYY.parquet`)가 연결되면 아래 키도 적용된다.
연결되지 않았으면 자동으로 무시 목록으로 간다.

| 키 | 의미 | 판정 |
|---|---|---|
| `debt_ratio_max_pct` | 부채비율 상한 (%) | `debt_ratio_pct < 값` |
| `current_ratio_min_pct` | 유동비율 하한 (%) | `current_ratio_pct > 값` |
| `profitable_quarters_min` | 영업이익 연속 흑자 분기 | `consecutive_profit_quarters >= 값` |

#### 재무 조건은 **그 시점에 공시된 것만** 본다 (as-of)

재무제표는 분기가 끝나고 45~90일 뒤에 공시된다.
2025-04-01 매매 판단에 2025Q1 재무를 쓰면 **그날 존재하지도 않던 정보**로 종목을 고른 것이다.

엔진은 기준일마다 `DartStore.as_of_panel(codes, 기준일)` 을 부른다.
즉 `disclosed_at <= 기준일` 인 행만 본다. 2025Q1 이 5/15 공시라면 5/14 판정에는 2024Q4 가 쓰인다.
전 구간에 최신 재무를 한 번 붙이는 식은 쓰지 않는다.

#### `on_missing` — 재무를 알 수 없는 종목

```json
"filters": { "debt_ratio_max_pct": 200, "on_missing": "include" }
```

| 값 | 동작 |
|---|---|
| `include` **(기본)** | 재무를 모르는 종목은 **통과**시킨다. 그 종목에는 재무 조건을 적용하지 않은 것으로 본다 |
| `exclude` | 제외한다. `warnings` 에 생존 편향 경고가 붙는다 |

**기본값이 `include` 인 이유가 중요하다.** DART 기업목록(`corp_map`)은 **현재 상장사만** 담는다.
상장폐지된 회사는 종목코드가 비어 있어 재무가 수집되지 않는다.
그래서 "재무를 모르면 제외"로 두면 **망한 회사만 골라서 빠지는 생존 편향**이 생기고
결과가 실제보다 좋게 나온다.

어느 쪽을 쓰든 몇 종목이 "재무 모름" 이었는지는 항상 집계되어 나온다
(`assumptions.stats.missing_financials` / `missing_financials_pct`,
`assumptions.dart.coverage_pct`).

판정 우선순위는 **"확실한 탈락" > "모름"** 이다.
부채비율은 알고 있는데 기준을 넘겼다면, 유동비율을 몰라도 그 종목은 탈락이다.

**그 밖의 키는 조용히 무시하지 않는다.** marcap 에도 DART 에도 없는 데이터를 요구하는 조건은
결과에 그대로 드러난다.

- `warnings` 에 한국어 한 줄
- `assumptions.notes` 에 같은 문장
- `assumptions.ignored_filters` 에 `[{"key": "debt_ratio_max_pct", "reason": "..."}]`

> "부채비율 상한 조건(200%)은 적용하지 않았습니다. marcap 에 재무 데이터가 없어 이 조건은
> 적용되지 않습니다. DART 연동이 필요합니다. 실제보다 종목이 많이 잡힙니다."

사유 문구는 같은 경로를 가리키는 `params[].unavailable_reason` 을 우선 쓴다.
그러니 데이터가 없는 조건은 `filters` 에 값을 넣어두고 `params` 에서
`available: false` + `unavailable_reason` 으로 선언해 두는 것이 좋다.
나중에 데이터가 붙으면 그때 엔진만 고치면 된다.

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
| `type` | 청산 전용: `take_profit` / `stop_loss` / `trailing_stop` / `time_exit` / `signal` |

### 부분 청산

청산 규칙의 `size_pct` 가 100 미만이면 **그만큼만 팔고 포지션이 남는다.**
남은 수량에 대해 나머지 청산 규칙이 같은 봉에서 이어 평가되고, 다음 날도 계속 평가된다.

```json
{ "id": "TP1", "type": "take_profit", "target_pct": 7, "size_pct": 50 },
{ "id": "TP2", "type": "signal", "when": {"op":"<","left":"close","right":"MA5"}, "size_pct": 100 }
```

- 결과 페이로드에서는 **청산 이벤트마다 `trades` 레코드가 1건** 생긴다.
  같은 진입에서 나온 것들은 **`group_id` 를 공유**한다 (문자열, 신규 필드).
- 부분 청산 후에도 평단가는 그대로 유지된다 (수량과 원가를 같은 비율로 줄인다).
- **한 청산 규칙은 한 포지션에서 한 번만 발동한다.** 그래야 `TP1` 이 매일 절반씩 파는 일이 없다.

### `time_exit` — 보유 기간 기준 청산

```json
{ "id": "TIME", "type": "time_exit", "hold_days": 7, "pnl_min_pct": 3,
  "when": { "op": "and", "conditions": [
    { "op": ">=", "left": "position.hold_days", "right": "hold_days" },
    { "op": "<",  "left": "position.pnl_pct",   "right": "pnl_min_pct" } ]},
  "price": "close", "size_pct": 100 }
```

`when` 을 생략하면 `max_hold_days`(또는 `hold_days`)만 보고 종가로 청산한다.
`position.hold_days` 는 **영업일(봉) 수**다.

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
| 시가총액 | `"marcap"` | 당일 시가총액(원). marcap 의 `Marcap` 컬럼을 그대로 쓴다 |
| 전일 값 | `"prev.close" "prev.high" "prev.volume" "prev.MA20"` | **직전 봉**의 값. 봉 필드·지표 별칭 모두 앞에 `prev.` 를 붙일 수 있다 |
| 기준일 참조 | `"ref.open" "ref.high" "ref.low" "ref.close" "ref.volume" "ref.amount" "ref.marcap"` | 기준일 봉 값 (전 필드) |
| 진입봉 참조 | `"entry.low" "entry.high" "entry.close" "entry.price"` | **진입이 체결된 봉**의 값. 청산 규칙에서만 쓸 수 있다 |
| 체결 참조 | `"fill.B1.price"` | 해당 규칙 체결가 |
| 포지션 참조 | `"position.avg_price" "position.pnl_pct" "position.hold_days"` | 현재 포지션 상태 |
| 지표 | `{"indicator":"SMA","period":45,"source":"close"}` | 지표 값 |
| 지표 별칭 | `"MA20"` `"HIGH252"` | `indicators[].key` 로 선언한 지표 (아래 참조) |
| 규칙 파라미터 | `"trigger_pct"` `"hold_days"` | 그 규칙 객체 안의 숫자 필드 |
| 수식 | `{"expr":"position.avg_price * 1.1"}` | 산술식 (`+ - * / % ** ( )` 와 위 참조들) |
| 상수 | `10` | 리터럴 |

- `entry.*` 를 **진입 규칙**에서 쓰면 검증 오류다 (진입 전에는 값이 없다).
- `position.hold_days` 는 **보유 영업일(봉) 수**다.
  결과 페이로드의 `trades[].hold_days`(달력일)와 값이 다를 수 있으니 주의한다.
- `expr` 은 `eval()` 을 쓰지 않는다. `ast` 로 파싱해 산술 노드만 통과시키며
  **함수 호출·람다·컴프리헨션·속성 접근(`__`)은 전부 거부**한다.

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
| `HIGHEST` | `period`, `source` | N봉 최고값 (52주 신고가 판정: `period: 252`) |
| `LOWEST` | `period`, `source` | N봉 최저값 |

`source` 로 쓸 수 있는 값: `close` `open` `high` `low` **`volume`** **`amount`** `hl2` `hlc3` `ohlc4`.
`{"indicator":"SMA","period":20,"source":"volume"}` 처럼 쓰면 **거래량 이동평균**이 된다.

### 지표 별칭 — `indicators[].key`

`indicators` 에 선언한 지표는 `key` 이름으로 조건식과 수식 어디서나 참조할 수 있다.

```json
"indicators": [
  { "key": "MA20",    "type": "SMA",     "period": 20,  "source": "close",  "plot": true },
  { "key": "VMA20",   "type": "SMA",     "period": 20,  "source": "volume", "plot": false },
  { "key": "HIGH252", "type": "HIGHEST", "period": 252, "source": "close",  "plot": true },
  { "key": "MACD_MAIN", "type": "MACD", "fast": 12, "slow": 26, "signal": 9, "field": "macd" }
]
```

```json
{ "op": "<=", "left": "low",   "right": "MA20" }
{ "op": ">=", "left": "close", "right": "HIGH252" }
{ "op": ">",  "left": "prev.close", "right": "prev.MA20" }
{ "op": ">=", "left": "volume", "right": { "expr": "VMA20 * volume_mult" } }
```

**별칭을 쓰면 `params` 가 지표 기간을 직접 가리킬 수 있다** (`indicators[0].period`).
조건식 안에 `{"indicator": ...}` 를 인라인으로 박아 넣으면 같은 숫자가 여러 곳에 중복되어
"기본값으로 되돌리기"가 제대로 동작하지 않는다. 되도록 별칭을 쓴다.

조건식에서 서브 필드는 `field`로 지정한다:

```json
{ "indicator": "BBANDS", "period": 20, "stddev": 2, "field": "lower" }
```

새 지표를 추가하려면 `engine/indicators.py`에 함수 하나를 등록하고 이 표에 한 줄 추가한다.

---

## `params` — 화면에서 조절할 파라미터 선언

전략 JSON 최상위의 `params` 는 **UI 전용 메타데이터**다.
**엔진 로직은 이 값을 절대 읽지 않는다.** 엔진은 `path` 가 가리키는 실제 값만 본다.
화면의 파라미터 폼은 이 선언으로 렌더한다 (하드코딩 금지).

```json
"params": [
  {
    "key": "market_cap_min_eok",
    "label": "최소 시가총액",
    "group": "1. 종목 선정",
    "path": "universe.filters.market_cap_min_eok",
    "type": "number",
    "unit": "억",
    "default": 2000,
    "min": 0, "max": 100000000, "step": 100,
    "help": "이 금액을 초과하는 종목만 대상으로 합니다",
    "available": true
  },
  {
    "key": "debt_ratio_max_pct",
    "label": "부채비율 상한",
    "group": "1. 종목 선정",
    "path": "universe.filters.debt_ratio_max_pct",
    "type": "number", "unit": "%", "default": 200,
    "available": false,
    "unavailable_reason": "marcap 에 재무 데이터가 없어 이 조건은 적용되지 않습니다. DART 연동이 필요합니다."
  }
]
```

| 키 | 필수 | 설명 |
|---|---|---|
| `key` | O | 고유 식별자 |
| `label` | O | 화면 표시명 |
| `group` | O | 폼에서 묶을 그룹명. 같은 문자열끼리 한 섹션 |
| `path` | O | 실제 값의 위치. `entries[0].size_pct` 형식 (점 구분, 대괄호 정수 인덱스) |
| `type` | O | `number` / `int` / `percent` / `select` / `bool` / `date` / `text` |
| `default` | O | **초기화 버튼이 되돌릴 값.** 저장해도 이 값은 바뀌지 않는다 |
| `unit` `min` `max` `step` `options` `help` | | 폼 렌더링 힌트 (`type: "select"` 는 `options` 필수) |
| `available` | | `false` 면 입력 비활성화 + `unavailable_reason` 표시. 기본 `true` |
| `requires` | | 이 파라미터가 실제로 동작하려면 필요한 외부 데이터. 현재 `"dart"` 만 |

**`available` 을 파일에 박아두지 마라.** 전략 JSON은 정적인데 DART 연결 여부는 실행 환경에 달렸다.
재무처럼 외부 데이터가 필요한 항목은 `requires: "dart"` 로 선언하고 `unavailable_reason` 을 함께 둔다.
프런트가 `/api/status` 로 DART 가용 여부를 확인해 `available` 을 **런타임에** 판단한다.

```json
{
  "key": "debt_ratio_max_pct",
  "label": "부채비율 상한",
  "group": "1. 종목 선정",
  "path": "universe.filters.debt_ratio_max_pct",
  "type": "number", "unit": "%", "default": 200,
  "available": true,
  "requires": "dart",
  "unavailable_reason": "marcap 에 재무 데이터가 없어 이 조건은 적용되지 않습니다. DART 연동이 필요합니다."
}
```

**초기화 동작** — "기본값으로 되돌리기"는 모든 `params[].default` 를 각 `path` 에 다시 써넣는다.
`available: false` 항목도 되돌린다. `params` 배열 자체는 절대 수정하지 않는다.

```python
from engine.params import get_by_path, set_by_path, reset_to_defaults, params_snapshot

reset_to_defaults(strategy)     # 새 문서를 돌려준다 (원본은 그대로)
params_snapshot(strategy)       # 각 파라미터의 현재 value / is_default 를 붙여 돌려준다
```

`validate_strategy` 는 모든 `path` 가 실제로 존재하는지 검사한다. 없으면
`{"path": "params[3].path", "message": "가리키는 위치가 없습니다: universe.foo"}`.

> **주의** — `path` 는 엔진이 실제로 읽는 위치를 가리켜야 한다.
> 같은 숫자가 문서 안 여러 곳에 중복돼 있으면(예: 조건식과 `price` 에 각각 인라인으로 박힌 기간)
> 한 곳만 바뀌어 동작이 어긋난다. 이동평균 기간 같은 값은 `indicators` 에 별칭으로 한 번만 두고
> `path` 를 `indicators[0].period` 로 잡는다.

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

#### 종가에 진입한 봉에서는 아예 청산하지 않는다

`same_day_exit` 과 **무관하게** 적용되는 규칙이 하나 더 있다.
`fill_model: "close"` 이거나 `price: "close"` 라서 **그 봉의 종가에 진입**했다면,
같은 봉에서는 어떤 청산도 평가하지 않는다. 장이 이미 끝나서 팔 시간 자체가 없기 때문이다.
(이걸 막지 않으면 "종가 매수 → 같은 종가 매도"가 성립해 수수료만 빠지는 가짜 거래가 대량으로 생긴다.)

---

---

## 데이터 품질 — 거래정지와 수정주가

marcap 은 **KRX 원본 시세**다. 두 가지를 엔진이 직접 보정한다.

### 거래정지일

KRX 는 거래정지일에 **시가·고가·저가를 0 으로, 종가만 기준가**로 발표한다.

```
2026-04-22   시0  고0  저0  종2,040  거래량 0      <- 파인텍(131760) 거래정지
2026-05-22   시6,160 고6,480 저4,760 종4,790      <- 16일 정지 후 재개, +495%
```

그대로 두면 `low <= 목표가` 같은 조건이 **저가가 0 이라 무조건 성립**한다.
아무도 거래할 수 없는 날에 체결된 것으로 계산되고, 재개 후 폭등을 그대로 먹는다.
실측으로 전 구간의 **4.1%(292,524행)** 가 여기 해당한다.

판정: `Volume == 0` 이고 `Open/High/Low` 중 0 이 있으면 거래정지.
여기에 더해 **OHLC 중 하나라도 0 이하면 거래량과 무관하게 체결 불가**로 본다.

| 대상 | 처리 |
|---|---|
| 진입·청산 판정 | **건너뛴다.** 그날은 어떤 체결도 일어나지 않는다 |
| 기준일 판정 | 기준일이 될 수 없다. 직전일이 정지면 "직전일 거래대금 N억 이하" 조건도 무효로 본다 (거래가 없었던 것이지 조용했던 게 아니다) |
| 보유 포지션 | **그대로 들고 간다.** 팔 수 없기 때문이다. 보유일수는 계속 센다 |
| 지표 계산 | 종가는 기준가로 유효하므로 이동평균 등에는 그대로 쓴다 |
| 정지 해제일 | 정상 거래일이므로 평소 규칙대로 체결한다. 정지 중에 생긴 신호는 애초에 만들어지지 않는다 |

> **주의** — 거래정지일은 거래량이 0 이라 `SMA(volume, N)` 같은 거래량 이동평균을 낮춘다.
> 거래량 배수 조건이 실제보다 쉽게 성립할 수 있다. 지표 입력에서 정지일을 빼지는 않는다.

### 수정주가

액면분할·무상증자가 반영돼 있지 않다. 삼성전자 2018-05-04 50:1 분할이면
2,650,000원이 51,900원이 되는데, **백테스트는 이걸 하루 -98% 폭락으로 계산한다.**

상장주식수(`Stocks`) 변화로 찾아낸다.

```
배율  = Stocks(당일) / Stocks(전일)          # 삼성전자 128,386,494 -> 6,419,324,700 = 50배
계수  = 1 / 배율                              # 과거 가격에 곱한다 (소급)
```

**다만 주식수가 변한 날의 약 60%는 분할이 아니다** (유상증자·합병 신주·전환사채 전환).
이런 날은 주식수만 늘고 가격은 그만큼 안 빠지므로, 그대로 조정하면 **없던 급락을 만들어낸다.**
그래서 시가총액 연속성으로 교차 검증한다.

```
(가격비 x 주식수비) 가 0.7 ~ 1.3 안에 들어야 진짜 분할로 인정한다
   삼성전자 2018-05-04 : (51,900/2,650,000) x 50 = 0.979  -> 분할 O
```

- `MarcapStore(adjusted=True)` 가 **기본값**이다. 조정하지 않으면 결과가 무의미하다
- OHLC × 계수, `Volume` ÷ 계수(거래대금 보존), `Amount`·`Marcap`·`Stocks` 는 건드리지 않는다
- 조정 기준 시점은 **불러온 구간의 마지막 거래일**이다
- 차트와 백테스트가 같은 가격을 쓴다

**반영되지 않는 것**: 유상증자, 배당락, 위 교차검증을 통과하지 못한 주식수 변동.
`assumptions.price_adjustment.skipped_not_split` 로 몇 건이 그랬는지 알 수 있다.

---

백테스트 결과의 `assumptions` 블록에 실제로 적용된 값과 사람이 읽을 수 있는 설명(`notes`),
그리고 아래 통계가 함께 담겨 나온다.

| `assumptions.stats` | 의미 |
|---|---|
| `ambiguous_bars` | 진입과 청산이 같은 봉 안에서 모두 성립해 **순서를 알 수 없었던** 봉의 수. 많을수록 결과가 가정에 크게 의존한다 |
| `missing_financials` / `_pct` | 재무를 알 수 없었던 종목 수와 비중 (`on_missing` 이 처리한 대상) |
| `halted_bars_skipped` / `halted_symbols` | 거래정지라서 건너뛴 (종목·날짜) 수와 종목 수 |
| `halted_reference_days_rejected` | 거래정지라 기준일로 채택하지 않은 건수 |
| `price_adjust_events` / `_symbols` | 수정주가를 적용한 이벤트 수와 종목 수 |
| `price_adjust_skipped_not_split` | 주식수는 변했지만 분할이 아니라 조정하지 않은 건수 (유상증자 등) |
| `extreme_moves_flagged` | 가격제한폭(±30%)을 넘는 일간 변동. 조정으로 못 잡은 권리락이나 정지 해제 갭 |
| `limit_price_fills` | 상한가·하한가로 마감한 봉에서 체결된 건수 (실제로는 잡기 어렵다) |
| `same_day_profit_exits_blocked` | 그중 `loss_only`/`never` 규칙 때문에 **당일 이익 청산이 차단되어** 다음 거래일로 넘어간 건수. 이 옵션이 실제로 얼마나 작동했는지를 보여준다 |
| `same_day_entry_exit` / `_pct` | 진입일과 청산일이 같은 거래 수와 비중 |

`assumptions.dart` 블록도 함께 나온다.

```json
"dart": {"available": true, "as_of": true, "coverage_pct": 87.3,
         "on_missing": "include", "last_fetch": "2026-08-05 14:20:00"}
```

`available: false` 면 재무 조건은 하나도 적용되지 않았다는 뜻이고,
그 조건들은 `ignored_filters` 에 사유와 함께 들어 있다.

```json
"price_adjustment": {"applied": true, "events": 777, "symbols": 640,
                     "method": "stocks_ratio+marcap_continuity",
                     "candidates": 2069, "skipped_not_split": 1292,
                     "limitations": ["유상증자는 반영되지 않습니다", "..."]}
```

---

## AI 생성 시 규칙

1. 출력은 JSON 객체 **하나**. 코드펜스·설명 문장 금지
2. 모르는 값은 만들지 말고 이 문서의 기본값을 쓴다
3. 분할 매수/매도는 `entries`/`exits` 배열 원소를 늘려서 표현한다 (별도 필드 만들지 않음)
4. 원문 자연어는 `description`에 그대로 보존한다 (나중에 재변환·검증용)
5. 생성 후 `engine/validate.py`로 스키마 검증을 통과해야 등록된다
6. 조절할 만한 숫자는 전부 `params` 에 노출한다. `path` 는 엔진이 읽는 위치를 정확히 가리켜야 하고,
   `default` 는 문서에 실제로 들어간 값과 같아야 한다
7. 이동평균·신고가 같은 지표는 `indicators` 에 `key` 를 붙여 선언하고 조건식에서는 그 이름을 쓴다
   (인라인 `{"indicator": ...}` 를 여기저기 박지 않는다)
8. `expr` 에 함수 호출을 쓰지 않는다 (`sma(...)` 불가). 지표는 반드시 별칭이나 `{"indicator": ...}` 로 쓴다
9. 재무 조건(부채비율·유동비율·연속 흑자)은 `universe.filters` 의 정해진 키를 쓰고,
   `params` 에는 `requires: "dart"` + `unavailable_reason` 을 붙인다. `available: false` 를 박지 않는다
10. marcap 에도 DART 에도 없는 조건은 지어내지 말고 `universe.filters` 에 값만 두면
   엔진이 `ignored_filters` 로 사용자에게 알린다

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
