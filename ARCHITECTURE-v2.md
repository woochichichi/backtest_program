# 구현 계약 v2 — 파라미터 선언 · 진행률/취소 · DSL 확장

`ARCHITECTURE.md`(v1)에 더해지는 계약. v1 의 기존 필드는 그대로 유지된다(하위 호환).

---

## 1. `params` — 조절 가능한 파라미터 선언 (DSL 확장)

전략 JSON 최상위에 `params` 배열을 둔다. **UI 전용 메타데이터이며 엔진 로직은 이를 읽지 않는다.**
엔진은 `path` 가 가리키는 실제 값만 본다. 화면의 파라미터 폼은 이 선언으로 렌더한다(하드코딩 금지).

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
    "help": "이 금액 이상인 종목만 대상으로 합니다",
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
| `path` | O | 실제 값의 위치. `entries[0].size_pct` 형식(대괄호 인덱스, 점 구분) |
| `type` | O | `number` / `int` / `percent` / `select` / `bool` / `date` |
| `default` | O | **초기화 버튼이 되돌릴 값.** 저장 후에도 이 값은 바뀌지 않는다 |
| `unit` `min` `max` `step` `options` `help` | | 폼 렌더링 힌트 |
| `available` | | `false` 면 입력을 비활성화하고 `unavailable_reason` 을 표시. 기본 `true` |

**초기화 동작** — "기본값으로 되돌리기"는 모든 `params[].default` 를 각 `path` 에 다시 써넣는다.
`available: false` 항목도 되돌린다. `params` 자체는 절대 수정하지 않는다.

`validate_strategy` 는 모든 `path` 가 실제로 존재하는지 검사하고, 없으면
`{"path": "params[3].path", "message": "가리키는 위치가 없습니다: universe.foo"}` 를 낸다.

---

## 2. 진행률 · 취소

### 2-1. 엔진

```python
run_backtest(strategy, store, progress=None, should_cancel=None)
```

- `should_cancel() -> bool` — 주기적으로 호출한다. `True` 면 즉시 `BacktestCancelled` 예외를 던진다.
  **최소 1초에 한 번 이상** 확인되어야 한다(종목 루프 안, 연도 로딩 사이 등).
- `progress(done, total, message, phase=None, eta_sec=None)` — `total` 은 항상 100.
  `eta_sec` 은 남은 예상 초. 근거가 없으면 `None`.
  **긴 구간에서 진행률이 멈춰 보이면 안 된다.** 10년 백테스트에서 최소 2초에 한 번은 갱신돼야 한다.

`phase` 는 사용자에게 보여줄 단계 이름:
`데이터 읽는 중` / `기준일 찾는 중` / `종목별 매매 계산 중` / `성과 계산 중`

`message` 는 한국어 한 줄이며 **진척이 보이는 구체적 정보**를 담는다.
예: `"2019년 데이터 읽는 중 (3/11년)"`, `"종목별 매매 계산 중 (1,240/2,700 종목)"`.

### 2-2. 서버

- `POST /api/backtest/cancel/{job_id}` → `{"ok": true, "cancelled": true}`.
  없는 job_id 는 404. 이미 끝난 작업은 `{"ok": true, "cancelled": false, "reason": "이미 완료됨"}`
- 백테스트를 **스레드에서 실행**하고 취소 플래그를 `should_cancel` 로 연결한다.
- 취소되면 `POST /api/backtest` 는 **499** 가 아니라 `200 + {"ok": false, "cancelled": true, "error": "사용자가 취소했습니다."}`
  (프런트가 오류 배너를 띄우지 않도록)
- SSE 이벤트에 `phase` 와 `eta_sec` 를 그대로 실어 보낸다.
- `/api/status` 의 `features` 에 `backtest_cancel: true`, `sync_status: true` 를 추가한다.
- **`POST /api/backtest` 의 60초 타임아웃을 600초로 늘린다.** 10년 백테스트가 60초를 넘는다.

### 2-3. 프런트

- 진행바에 **단계명 · 퍼센트 · 경과 시간 · 남은 예상 시간**을 함께 표시
- `eta_sec` 이 `null` 이면 남은 시간 칸은 `계산 중…`
- **취소 버튼은 실행 즉시 노출**한다(지금은 5초 후). 누르면 `POST /api/backtest/cancel/{job_id}`
- 취소 성공 시 오류가 아니라 중립 토스트: "백테스트를 취소했습니다"
- 기간이 5년을 넘으면 실행 **전에** 안내: "구간이 길어 수 분 걸릴 수 있습니다. 실행 중 취소할 수 있습니다."

---

## 3. DSL 확장 (전략3에 필요)

### 3-1. `universe.reference_day.rule: "custom"`

기존 `amount_spike` 등은 그대로 두고, `custom` 을 추가한다.
`when` 에 v1 의 조건식을 그대로 쓸 수 있다.

```json
"reference_day": {
  "rule": "custom",
  "lookback_days": 20,
  "when": { "op": "and", "conditions": [
    { "op": ">=", "left": "amount", "right": { "expr": "spike_amount_krw_eok * 100000000" } },
    { "op": ">=", "left": "volume", "right": { "expr": "sma(volume, 20)[-1] * volume_mult" } }
  ]}
}
```

### 3-2. 새 피연산자

| 형태 | 의미 |
|---|---|
| `"marcap"` | 시가총액(원). marcap 의 `Marcap` × 1e6 |
| `"prev.close"` `"prev.high"` … | 전일 봉 값 |
| `"ref.high"` `"ref.low"` `"ref.amount"` `"ref.volume"` | 기준일 봉 값 (v1 에 일부 존재, 전 필드로 확장) |
| `"entry.low"` `"entry.high"` `"entry.close"` `"entry.date"` | **진입이 체결된 봉**의 값 |
| `"position.hold_days"` | 보유 영업일 수 (v1 존재) |
| `{"indicator": "HIGHEST", "period": 252, "source": "close"}` | N봉 최고값 (52주 신고가 판정용) |
| `{"indicator": "SMA", "period": 20, "source": "volume"}` | 거래량 이동평균 (source 확장) |

`entry.*` 는 청산 규칙에서만 유효하다. 진입 전에 참조하면 검증 오류.

### 3-3. 부분 청산

`size_pct: 50` 인 청산이 발동하면 절반만 팔고 포지션이 남아야 한다.
남은 포지션에 대해 나머지 청산 규칙이 계속 평가된다.
결과 페이로드에서는 **청산 이벤트마다 trade 레코드 1건**을 만들되,
같은 진입에서 나온 것들은 `group_id` 를 공유한다(신규 필드). `trades[].group_id` 는 문자열.

### 3-4. `time_exit`

```json
{ "id": "TIME", "type": "time_exit", "hold_days": 7,
  "when": { "op": "and", "conditions": [
    { "op": ">=", "left": "position.hold_days", "right": 5 },
    { "op": "<",  "left": "position.pnl_pct",   "right": 3 } ]},
  "price": "close", "size_pct": 100 }
```

### 3-5. 데이터 없는 조건의 처리

`universe.filters` 에 엔진이 지원하지 않는 키가 있으면 **조용히 무시하지 말고**
`warnings` 와 `assumptions.notes` 에 한국어로 남긴다.

> "부채비율 200% 미만 조건은 재무 데이터가 없어 적용하지 않았습니다. 실제보다 종목이 많이 잡힙니다."

`assumptions` 에 `ignored_filters: [{"key": "...", "reason": "..."}]` 를 추가한다.

---

## 4. 전략3 사양 (PDF 원문 기준)

`strategies/strategy3.json` — **중단기 스윙 주도주 눌림목**

### 종목 스크리닝
| 조건 | 기본값 | 데이터 |
|---|---|---|
| 시가총액 초과 | 2,000억 | 있음 |
| 부채비율 미만 | 200% | **없음** |
| 유동비율 초과 | 100% | **없음** |
| 영업이익 연속 흑자 분기 | 4 | **없음** |

### 기준일 (전부 AND)
| 조건 | 기본값 |
|---|---|
| 거래대금 초과 | 1,000억 |
| 거래량 ≥ 직전 20일 평균 × 배수 | 5배 |
| 종가 전일 대비 상승률 이상 | +15% |
| 시가 갭상승 미만 | +5% |
| 52주(252봉) 종가 신고가 | on |
| 전일 종가 > 20일선 | on |

### 눌림목 (기준일 후 N일 이내)
- 일일 거래대금 ≤ 기준일 거래대금 × 1/3
- 유효 기간 15거래일

### 진입 (3조건 AND, 종가 매수, 비중 100%)
- 저가 ≤ 20일선
- 종가 > 20일선
- MACD(12,26,9) > 0

### 청산
| 규칙 | 조건 | 비중 |
|---|---|---|
| SL | 종가 < 진입일 저가 **또는** 종가 < 20일선 | 100% |
| TP1 | 평단 +7% 도달 | 50% |
| TP2 | 종가 < 5일선 | 잔량 100% |
| TIME | 보유 7일 경과 & 수익률 < +3% | 100% |

`exit_priority`: `["SL", "TIME", "TP1", "TP2"]`

`params` 에는 위 표의 모든 숫자를 노출한다. 재무 3종은 `available: false`.
