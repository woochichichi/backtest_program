# KRX Backtester

국내 주식(KOSPI/KOSDAQ) 일봉 백테스팅 웹 플랫폼. TradingView 스타일 UI에서 전략을 고르고,
파라미터를 직접 바꾸고, 어떤 종목을 언제 사고팔아 얼마를 벌었는지 차트와 표로 확인한다.

현재 단계: **목업 디자인 + 데이터 파이프라인 + 전략 포맷** 완료. 백테스트 엔진은 다음 단계.

---

## 빠른 시작

```bat
REM 1. 데이터 최초 내려받기 (수 분 소요, 약 1.8GB)
update_marcap.bat

REM 2. 매일 자동 갱신 등록 (선택, 기본 18:30)
setup_daily_update.bat
```

목업 화면은 `mockup/index.html`을 브라우저로 열면 된다. 서버·빌드 없이 바로 열린다.

---

## 폴더 구조

```
backtest_program/
├─ marcap/                    # 시가총액/OHLCV 데이터 (git clone, .gitignore 대상)
├─ mockup/index.html          # UI 목업 (단일 HTML, 의존성 없음)
├─ strategies/
│   ├─ strategy1.json         # 전략1 — 거래대금 급증 눌림목 (등록 완료)
│   └─ SCHEMA.md              # 전략 DSL v1 명세 (AI 생성용 포맷)
├─ update_marcap.bat          # 데이터 수동 갱신
├─ setup_daily_update.bat     # 매일 자동 갱신 등록 (작업 스케줄러)
├─ remove_daily_update.bat    # 자동 갱신 해제
├─ data_status.json           # 마지막 갱신 시각 (웹 헤더에 표시)
└─ logs/                      # 갱신 로그
```

---

## 데이터

[FinanceData/marcap](https://github.com/FinanceData/marcap) 저장소를 그대로 사용한다.
`marcap/data/marcap-YYYY.parquet` 형태로 연도별 파일이 있고, 1995-05-02 부터 현재까지
매일 자동으로 갱신된다.

| 컬럼 | 의미 | | 컬럼 | 의미 |
|---|---|---|---|---|
| `Date` | 날짜 (인덱스) | | `Volume` | 거래량 |
| `Code` | 종목코드 | | `Amount` | **거래대금** |
| `Name` | 종목명 | | `Marcap` | 시가총액(백만원) |
| `Open` `High` `Low` `Close` | 시고저종 | | `Market` | KOSPI / KOSDAQ |
| `Changes` `ChagesRatio` | 전일대비 / 등락률 | | `Stocks` | 상장주식수 |

전략1의 "거래대금 1000억"은 `Amount >= 100_000_000_000` 으로 판정한다.

### 갱신 방식

| 방법 | 실행 | 동작 |
|---|---|---|
| 수동 | `update_marcap.bat` 더블클릭 | `marcap` 폴더가 없으면 clone, 있으면 `git pull --ff-only` |
| 자동 | `setup_daily_update.bat` 1회 실행 | 작업 스케줄러 `KRXBacktesterDataSync` 등록, 매일 지정 시각 실행 |
| 해제 | `remove_daily_update.bat` | 스케줄 삭제 (수동 갱신은 계속 가능) |

갱신이 끝나면 `data_status.json`에 다음이 기록되고, 웹 헤더의 **데이터 갱신** 표시에 그대로 나온다.

```json
{
  "last_sync": "2026-07-25 08:12:04",
  "result": "updated",
  "repo_path": "marcap",
  "latest_file": "marcap-2026.parquet",
  "file_count": 32,
  "git_rev": "a1b2c3d"
}
```

---

## 전략1 — 거래대금 급증 눌림목

`strategies/strategy1.json` 에 등록됨. 웹 좌측 패널에서 모든 숫자를 직접 수정할 수 있고,
수정 즉시 JSON 탭에 반영된다.

**기준일 = 일 거래대금 1,000억 이상 발생한 날**

### 1. 종목 선정
- 최근 **20영업일** 안에 일 거래대금 **1,000억 이상**인 날이 있는 종목
- 그 **기준일 직전 영업일**의 거래대금이 **200억 이하**
- 기준일 이후 하락하여 **기준일 시가**에 도달한 종목

### 2. 매매
| 구분 | 조건 | 비중 |
|---|---|---|
| 매수 1 | 기준일 시가까지 하락 시, 그 가격에 체결 | 50% |
| 매수 2 | 매수 1 체결가 대비 **-10%** 하락 시 | 50% |
| 매도 1 (익절) | 평단가 **+10%** 도달 | 전량 |
| 매도 2 (손절) | **45일 이동평균선** 도달 | 전량 |

같은 봉에서 익절과 손절이 동시에 성립하면 손절을 우선한다(`exit_priority`).

### 3. 실행 기준
- 일봉으로 신호 판정, **1분 단위**로 가격 확인 및 체결 (`trade_resolution: "1m"`)
- 체결 모델 `touch` — 장중 가격이 목표가에 닿으면 그 가격에 체결
- 슬리피지 0.1%, 수수료+세금 0.23% (매도 시 일괄 차감)

---

## 전략 포맷 (AI 연동)

모든 전략은 `krx-backtest-strategy/v1` JSON 하나로 표현된다. 명세는 [`strategies/SCHEMA.md`](strategies/SCHEMA.md).

자연어를 이 포맷으로 변환하면 그대로 실행된다. 웹의 **AI 전략 생성** 탭이 이 경로를 쓴다.

```
자연어 조건  ->  LLM (SCHEMA.md를 시스템 프롬프트로)  ->  DSL v1 JSON  ->  스키마 검증  ->  백테스트
```

분할 매수·분할 매도는 `entries` / `exits` 배열의 원소를 늘려서 표현하고,
`fill.B1.price`, `position.avg_price`, `{"indicator":"SMA","period":45}` 같은 참조로 규칙끼리 연결한다.

### LAG 지표

`SMA` `EMA` `WMA` `BBANDS` `RSI` `MACD` `STOCH` `ATR` `ADX` `CCI` `OBV` `VWAP` `ENVELOPE` `DONCHIAN`
— 조건식과 차트 양쪽에서 쓸 수 있다. 새 지표는 함수 하나를 등록하고 `SCHEMA.md` 표에 한 줄 추가하면 된다.

---

## 화면 구성 (목업)

| 영역 | 내용 |
|---|---|
| 헤더 | 데이터 갱신 시각, 최신 거래일, 지금 갱신 버튼, 매일 자동 갱신 토글, 백테스트 실행 |
| 좌측 | 전략 목록 / 파라미터 편집 (모든 값 수정 가능) / 표시 지표 선택 |
| 중앙 | 캔들 + MA + 거래대금 + 1000억 기준선 + 기준일 시가 라인 + B1·B2·매도 마커 + 보유 구간 음영 |
| 하단 | 거래 내역 / 시그널 로그 / 전략 JSON / AI 전략 생성 / 데이터 상태 |
| 우측 | 총수익률·CAGR·MDD·샤프·승률·손익비, 누적 수익 곡선, 월별 수익률, 종목별 기여도 |

목업의 숫자와 차트는 고정 시드로 생성한 예시 데이터다. 엔진 연결 시 실제 결과로 교체된다.

---

## 다음 단계

- [ ] `engine/` — parquet 로더, 지표 계산, DSL 인터프리터, 체결 시뮬레이터
- [ ] 1분봉 소스 연결 (marcap은 일봉만 제공)
- [ ] `engine/validate.py` — DSL 스키마 검증
- [ ] 웹 서버(FastAPI) + 목업 화면을 실제 API에 연결
- [ ] AI 전략 생성 엔드포인트
