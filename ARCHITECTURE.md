# 구현 계약 (ARCHITECTURE)

이 문서는 `engine` / `server` / `web` 세 계층이 서로 지켜야 할 인터페이스다.
각 계층은 이 문서만 보고 독립적으로 구현·수정할 수 있어야 한다.

```
backtest_program/
├─ marcap/                     # 데이터 (git clone, gitignore)
├─ engine/                     # 순수 파이썬 백테스트 엔진 (웹 의존성 없음)
│   ├─ data.py                 # parquet 로더 + 캐시
│   ├─ indicators.py           # LAG 지표
│   ├─ dsl.py                  # DSL v1 조건식 평가
│   ├─ validate.py             # DSL 스키마 검증
│   ├─ backtest.py             # 스캔 → 진입 → 청산 → 포트폴리오
│   └─ metrics.py              # 성과 지표
├─ server/
│   ├─ app.py                  # FastAPI
│   └─ web/                    # 정적 프런트엔드 (index.html, css, js)
├─ strategies/*.json
├─ cache/                      # feather 캐시 (gitignore)
└─ *.bat
```

---

## 1. engine 파이썬 API

```python
from engine.data import MarcapStore

store = MarcapStore(root="marcap", cache_dir="cache")
store.available            # -> bool   marcap 폴더 존재 여부
store.status()             # -> dict   ARCHITECTURE 4-1 의 status 페이로드
store.latest_date()        # -> datetime.date
store.bars(code, start=None, end=None)   # -> pd.DataFrame (index=Date, OHLCV+Amount+Name)
store.panel(start, end, columns=None)    # -> pd.DataFrame (전 종목, MultiIndex 없음, Date/Code 컬럼)
store.names()              # -> dict {code: name}
```

- `MarcapStore`는 연도별 `marcap/data/marcap-YYYY.parquet`을 **필요한 연도만** 읽는다.
- 읽은 연도는 프로세스 메모리에 캐시하고, `cache/panel-<start>-<end>.feather`로도 저장한다.
- 데이터가 없으면 `available=False`이고 모든 조회는 `DataUnavailable` 예외를 던진다.

```python
from engine.indicators import compute, REGISTRY

compute("SMA", {"period": 45, "source": "close"}, df)   # -> np.ndarray (len == len(df), 앞부분 NaN)
REGISTRY                                                 # -> {"SMA": spec, "RSI": spec, ...}
```

```python
from engine.validate import validate_strategy
ok, errors = validate_strategy(obj)      # errors: [{"path": "entries[1].size_pct", "message": "..."}]
```

```python
from engine.backtest import run_backtest
result = run_backtest(strategy: dict, store: MarcapStore, progress: callable | None = None)
# progress(done: int, total: int, message: str)
```

`run_backtest`의 반환값은 4-5의 `/api/backtest` 응답과 **동일한 dict**다. 서버는 그대로 직렬화만 한다.

---

## 2. 체결 규칙 (엔진 핵심)

| 항목 | 규칙 |
|---|---|
| 신호 판정 | 일봉. 당일 `low`/`high`가 목표가에 닿으면 성립 |
| 체결가 | `fill_model: "touch"` → 목표가 그대로. 단 갭으로 목표가를 지나쳤으면 **당일 시가**로 체결 |
| 슬리피지 | 매수는 체결가 × (1+slippage), 매도는 × (1-slippage) |
| 비용 | 매도 시 `fee_pct`를 거래대금에 일괄 부과 |
| 같은 날 진입+청산 | 허용. 단 진입 규칙이 먼저 평가된다 |
| 청산 우선순위 | `exit_priority` 순서대로 평가. 기본 `["SL","TP"]` (손절 우선, 보수적) |
| 분할 매수 | `size_of: "planned_position"` → 종목당 배정 자본(총자본/max_positions)에 대한 비율 |
| 수량 | 정수 주. 내림 |
| 중복 진입 | `allow_duplicate_symbol=false`면 보유 중인 종목은 재진입 불가 |
| 기준일 만료 | `valid_days_after_reference` 영업일 내 B1 미체결이면 후보 폐기 |
| 미청산 포지션 | 백테스트 종료일 종가로 강제 청산, `exit_reason="기간종료"` |

`trade_resolution: "1m"` 은 **1분봉 데이터가 연결되기 전까지 일봉 근사로 동작**하며,
결과 페이로드의 `warnings`에 그 사실을 넣는다. (marcap은 일봉만 제공)

---

## 3. 성능 요구

- 1년 전 종목 스캔(약 2,700종목 × 250일)이 **3초 이내**.
- 종목 선정은 pandas 벡터 연산으로 처리한다. 종목별 파이썬 루프는 **후보 종목에 한해서만**.
- 차트 API는 컬럼 지향 배열로 응답한다(객체 배열 금지). 프런트에서 TypedArray로 바로 받는다.

---

## 4. HTTP API (FastAPI, 모두 `/api` 프리픽스)

### 4-1. `GET /api/status`
```json
{
  "available": true,
  "last_sync": "2026-07-25 08:12:04",
  "result": "updated",
  "latest_trade_date": "2026-07-24",
  "first_trade_date": "1995-05-02",
  "file_count": 32,
  "git_rev": "a1b2c3d",
  "row_count": 11043912,
  "auto_sync": { "registered": true, "time": "18:30", "task": "KRXBacktesterDataSync" }
}
```
`available:false`면 나머지 필드는 null 허용. 프런트는 이때 "데이터 없음" 안내를 띄운다.

### 4-2. `POST /api/sync`
`update_marcap.bat`(Windows) 또는 `git -C marcap pull`(그 외)을 실행하고 4-1과 같은 형태를 반환.
실패 시 `{"ok": false, "error": "...", "log_tail": "..."}`.

### 4-3. 전략
| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/strategies` | `[{id,name,description,enabled,updated_at}]` |
| GET | `/api/strategies/{id}` | DSL JSON 전문 |
| PUT | `/api/strategies/{id}` | 저장 (검증 통과해야 함). 실패 시 422 + errors |
| POST | `/api/strategies` | 신규 생성 |
| DELETE | `/api/strategies/{id}` | 삭제 |
| POST | `/api/strategies/validate` | `{valid: bool, errors: [...]}` |

### 4-4. `GET /api/indicators`
```json
[{"key":"SMA","label":"단순이동평균","params":[{"name":"period","type":"int","default":20}],"fields":null,"overlay":true}]
```
프런트의 지표 칩/추가 다이얼로그는 이 응답으로 그린다. 하드코딩 금지.

### 4-5. `POST /api/backtest`
요청: `{"strategy": <DSL 객체>, "period": {"start":"2025-01-02","end":"auto"}}`

응답:
```json
{
  "run_id": "r_20260725_081204",
  "elapsed_sec": 2.7,
  "warnings": ["1분봉 데이터가 없어 일봉 근사로 체결했습니다."],
  "metrics": {
    "total_return_pct": 38.4, "cagr_pct": 24.1, "mdd_pct": -12.7, "sharpe": 1.42,
    "win_rate_pct": 63.0, "profit_factor": 2.08, "trades": 27, "wins": 17, "losses": 10,
    "avg_win_pct": 8.1, "avg_loss_pct": -3.9, "avg_hold_days": 18.4,
    "initial_capital": 100000000, "final_capital": 138400000,
    "period": {"start": "2025-01-02", "end": "2026-07-24", "years": 1.56}
  },
  "equity":  {"dates": ["2025-01-02", "..."], "values": [100.0, "..."], "drawdown": [0.0, "..."]},
  "monthly": [{"month": "2025-01", "return_pct": 3.2}],
  "trades": [{
    "no": 1, "code": "042700", "name": "한미반도체",
    "ref_date": "2026-04-07", "ref_amount_eok": 1432, "ref_open": 45439,
    "fills": [{"rule": "B1", "date": "2026-04-14", "price": 45439, "qty": 44},
              {"rule": "B2", "date": "2026-04-22", "price": 40895, "qty": 49}],
    "avg_price": 43167, "exit_date": "2026-05-11", "exit_price": 47484,
    "exit_rule": "TP", "exit_reason": "평단 +10% 익절",
    "hold_days": 27, "return_pct": 10.0, "pnl": 341000
  }],
  "by_stock": [{"code": "042700", "name": "한미반도체", "trades": 2, "win_rate": 100.0, "pnl": 682000}],
  "signals": [{"ts": "2026-04-08 09:00:00", "level": "MATCH", "code": "042700", "message": "..."}]
}
```
결과는 서버 메모리에 `run_id`로 보관(최근 10건). 차트 API가 이를 참조한다.

### 4-6. `GET /api/chart`
쿼리: `code`, `start`, `end`, `indicators`(콤마 구분, 예 `SMA:20,SMA:45`), `run_id`(선택)

```json
{
  "code": "042700", "name": "한미반도체", "n": 132,
  "t": [20260112, 20260113],
  "o": [], "h": [], "l": [], "c": [], "v": [], "amt": [],
  "halted": [0, 1],
  "indicators": {"SMA:20": [null, "..."], "SMA:45": [null, "..."]},
  "markers": [{"i": 60, "type": "ref", "price": 45439, "label": "기준일", "note": "..."},
              {"i": 74, "type": "buy", "price": 45439, "label": "B1", "note": "..."}],
  "bands": [{"from": 74, "to": 101, "type": "holding"}],
  "levels": [{"price": 45439, "from": 60, "label": "기준일 시가", "type": "ref_open"}]
}
```
`t`는 `YYYYMMDD` 정수. 모든 배열 길이는 `n`으로 동일. 결측은 `null`.

`halted`는 거래정지일 표시(0/1). marcap은 거래정지일 OHLC를 `0`으로 발표하는데, 그대로 그리면
캔들이 0까지 늘어난다. 그래서 **OHLC 중 하나라도 0 이하인 날은 거래정지로 보고**
`o/h/l/c/v/amt`를 `null`로 내리고 `halted[i] = 1`로 표시한다. 프런트는 그 자리를 빈 칸으로 남기고
툴팁에 "거래정지"라고 쓴다. 지표도 0이 아닌 NaN 기준으로 계산한다(이동평균 왜곡 방지).
거래정지일이 있으면 `warnings`에 한 줄이 추가된다.

### 4-7. `POST /api/ai/strategy`
요청 `{"prompt": "자연어 조건", "base": <선택, 기존 DSL>}`
응답 `{"ok": true, "strategy": <DSL>, "warnings": []}` 또는
`{"ok": false, "error": "ANTHROPIC_API_KEY 가 설정되지 않았습니다.", "how_to": "..."}`

API 키는 환경변수 `ANTHROPIC_API_KEY`. 키가 없으면 **서버는 기동은 되고 이 엔드포인트만 실패**한다.
모델은 `claude-sonnet-5`를 기본으로 쓰고 `strategies/SCHEMA.md` 전문을 시스템 프롬프트에 넣는다.

---

## 5. 프런트엔드 요구

### 5-1. 테마
- 다크 / 라이트 두 벌. CSS 변수로만 색을 쓰고, `:root[data-theme="light"]`에서 덮어쓴다.
- 최초 진입은 `prefers-color-scheme`을 따르고, 사용자가 고르면 `localStorage.theme`에 저장.
- **차트 색도 CSS 변수에서 읽는다.** 테마 전환 시 캔버스 색상 캐시를 무효화하고 다시 그린다.
- 라이트 모드 대비 기준: 본문 `#1a1f2e` 이상, 보조 텍스트 `#5b6478` 이상, 보더는 눈에 보일 것.

### 5-2. 차트 렌더링 성능 (필수)
1. **캔버스 2장.** `base`(격자·캔들·이평·거래대금)와 `overlay`(크로스헤어·툴팁 앵커·호버 강조)를 분리한다.
   마우스 이동 시 `overlay`만 지우고 다시 그린다. `base`는 범위/테마/지표가 바뀔 때만 다시 그린다.
2. **TypedArray.** 서버 응답을 `Float64Array`/`Int32Array`로 변환해 보관한다. 객체 배열 금지.
3. **가시 구간만 그린다.** `[i0, i1)` 범위만 순회한다. 전체 순회 금지.
4. **다운샘플링.** 가시 봉 수가 캔버스 픽셀 폭을 넘으면 픽셀당 min/max/first/last로 집계해 그린다.
5. **패스 배칭.** 상승/하락 캔들을 각각 한 번의 `beginPath`로 묶는다. 캔들마다 `beginPath` 금지.
6. **rAF 코얼레싱.** `mousemove`/`wheel`/`resize`는 즉시 그리지 말고 `requestAnimationFrame` 한 프레임에 1회로 합친다.
7. **DPR 처리.** `devicePixelRatio`를 반영하되 상한 2로 제한한다.
8. 목표: 3,000봉에서 팬/줌 60fps, 초기 렌더 16ms 이내.

### 5-3. 상호작용
- 휠 = 줌, 드래그 = 팬, 더블클릭 = 전체 보기
- 거래 내역 행 클릭 → 해당 종목·구간으로 차트 이동 + 마커 강조
- 좌측 파라미터 수정 → 디바운스 300ms 후 JSON 탭 갱신 (백테스트는 실행 버튼으로만)
- 지표 칩은 `/api/indicators` 응답으로 렌더

### 5-4. 접근성
- 모든 인터랙티브 요소에 포커스 링, 아이콘 버튼에 `aria-label`
- 색만으로 정보 전달 금지 (매수/매도는 삼각형 방향 + 라벨 병행)
- `prefers-reduced-motion` 존중
