"""합성 OHLCV 픽스처.

marcap 데이터가 **전혀 없어도** 엔진 전체(스캔 → 진입 → 청산 → 성과)를 검증할 수 있도록
가짜 시세를 만들고 ``MarcapStore`` 와 같은 인터페이스를 갖는 ``FakeStore`` 를 제공한다.

핵심 시나리오 (전략1 = strategies/strategy1.json):

    bar 0..79    종가 1,000 평보합            (45일선을 낮게 깔아둔다)
    bar 80..99   1,000 → 4,000 상승           (bar 99 거래대금 100억)
    bar 100      기준일: 시가 4,000 / 거래대금 1,200억
    bar 101..104 눌림 — bar 104 저가 3,900 ≤ 4,000  → B1 체결 @ 4,000
    bar 105..107 추가 하락 — bar 107 저가 3,550 ≤ 3,600 → B2 체결 @ 3,600 (B1 -10%)
    TP 시나리오   bar 108..111 반등 — bar 111 고가 4,250 ≥ 평단×1.1 → TP 체결
    SL 시나리오   bar 108..111 급락 — bar 110 저가 1,900 ≤ MA45   → SL 체결
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.dart import DartStore  # noqa: E402
from engine.errors import DataUnavailable  # noqa: E402

EOK = 100_000_000.0

#: 시나리오의 주요 봉 인덱스
REF_BAR = 100
B1_BAR = 104
B2_BAR = 107
TP_BAR = 111
SL_BAR = 110

N_BARS = 200
START_BAR = 90  # period.start 로 쓰는 봉

MARCAP_COLUMNS = [
    "Date", "Rank", "Code", "Name", "Open", "High", "Low", "Close",
    "Volume", "Amount", "Changes", "ChangeCode", "ChagesRatio",
    "Marcap", "Stocks", "MarketId", "Market", "Dept",
]


# ======================================================================================
# 가격 경로
# ======================================================================================


def _warmup_rows() -> List[tuple]:
    """bar 0..99 — (open, high, low, close)."""
    rows = []
    for _ in range(80):                       # 0..79 : 1,000 평보합
        rows.append((1000.0, 1010.0, 990.0, 1000.0))
    for k in range(1, 21):                    # 80..99 : 1,000 → 4,000
        c = 1000.0 + 150.0 * k
        rows.append((c - 30.0, c + 30.0, c - 60.0, c))
    return rows


#: bar 100..111 — 기준일 + 눌림 (TP/SL 공통)
_COMMON_TAIL = [
    (4000.0, 5200.0, 3980.0, 4900.0),   # 100 기준일 (시가 4,000)
    (4880.0, 4900.0, 4600.0, 4700.0),   # 101
    (4690.0, 4710.0, 4400.0, 4500.0),   # 102
    (4490.0, 4510.0, 4150.0, 4200.0),   # 103
    (4150.0, 4180.0, 3900.0, 3950.0),   # 104  저가 3,900 ≤ 4,000  → B1
    (3940.0, 3960.0, 3800.0, 3820.0),   # 105
    (3810.0, 3830.0, 3700.0, 3720.0),   # 106
    (3710.0, 3730.0, 3550.0, 3600.0),   # 107  저가 3,550 ≤ 3,600  → B2
]

_TP_TAIL = [
    (3610.0, 3750.0, 3600.0, 3740.0),   # 108
    (3750.0, 3920.0, 3730.0, 3900.0),   # 109
    (3910.0, 4070.0, 3890.0, 4050.0),   # 110
    (4060.0, 4250.0, 4040.0, 4200.0),   # 111  고가 4,250 ≥ 평단×1.1 → TP
]

_SL_TAIL = [
    (3550.0, 3560.0, 3000.0, 3050.0),   # 108
    (3040.0, 3050.0, 2500.0, 2550.0),   # 109
    (2540.0, 2550.0, 1900.0, 1950.0),   # 110  저가 1,900 ≤ MA45 → SL
    (1900.0, 1920.0, 1500.0, 1550.0),   # 111
]


def price_path(kind: str, n: int = N_BARS) -> List[tuple]:
    """``kind`` 는 ``"tp"`` / ``"sl"`` / ``"flat"``."""
    rows = _warmup_rows()
    if kind != "flat":
        rows += _COMMON_TAIL
        rows += _TP_TAIL if kind == "tp" else _SL_TAIL
    last = rows[-1][3]
    while len(rows) < n:
        rows.append((last, last * 1.01, last * 0.99, last))
    return rows[:n]


def amount_path(kind: str, n: int = N_BARS) -> List[float]:
    """거래대금(원). 기준일만 1,200억, 그 직전일 100억, 나머지 50억."""
    amt = [50.0 * EOK] * max(n, N_BARS)
    if kind != "flat":
        amt[REF_BAR - 1] = 100.0 * EOK      # ≤ 200억 (prev_day_amount_max_eok)
        amt[REF_BAR] = 1200.0 * EOK         # ≥ 1,000억 (spike)
    return amt[:n]


def trading_dates(n: int = N_BARS, start: str = "2024-06-03") -> List[dt.date]:
    return [d.date() for d in pd.bdate_range(start=start, periods=n)]


# ======================================================================================
# 합성 마캡 프레임
# ======================================================================================


def make_frame(
    code: str,
    name: str,
    kind: str,
    dates: Sequence[dt.date],
    market: str = "KOSPI",
    dept: str = "",
) -> pd.DataFrame:
    rows = price_path(kind, len(dates))
    amt = amount_path(kind, len(dates))
    o = np.array([r[0] for r in rows], dtype="float64")
    h = np.array([r[1] for r in rows], dtype="float64")
    lo = np.array([r[2] for r in rows], dtype="float64")
    c = np.array([r[3] for r in rows], dtype="float64")
    a = np.array(amt, dtype="float64")
    v = np.maximum(a / np.maximum(c, 1.0), 1.0)
    chg = np.concatenate(([0.0], np.diff(c)))

    return pd.DataFrame(
        {
            "Date": pd.to_datetime(list(dates)),
            "Rank": np.arange(1, len(dates) + 1),
            "Code": code,
            "Name": name,
            "Open": o,
            "High": h,
            "Low": lo,
            "Close": c,
            "Volume": v,
            "Amount": a,
            "Changes": chg,
            "ChangeCode": "0",
            "ChagesRatio": np.where(c - chg > 0, chg / np.maximum(c - chg, 1.0) * 100.0, 0.0),
            "Marcap": c * 1_000_000.0,
            "Stocks": 1_000_000.0,
            "MarketId": "STK" if market == "KOSPI" else "KSQ",
            "Market": market,
            "Dept": dept,
        },
        columns=MARCAP_COLUMNS,
    )


# ======================================================================================
# FakeStore — MarcapStore 와 같은 인터페이스
# ======================================================================================


class FakeStore:
    """메모리 상의 합성 패널을 MarcapStore 처럼 노출한다."""

    def __init__(self, frames: Sequence[pd.DataFrame]):
        df = pd.concat(list(frames), ignore_index=True)
        df["Date"] = pd.to_datetime(df["Date"])
        self._df = df.sort_values(["Date", "Code"], kind="stable").reset_index(drop=True)
        self.panel_calls: List[tuple] = []

    # -- 상태 -------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return True

    def status(self) -> dict:
        return {
            "available": True,
            "last_sync": None,
            "result": "synthetic",
            "latest_trade_date": self.latest_date().isoformat(),
            "first_trade_date": self.first_date().isoformat(),
            "file_count": 1,
            "git_rev": None,
            "row_count": int(len(self._df)),
            "auto_sync": {"registered": False, "time": None, "task": "KRXBacktesterDataSync"},
        }

    def latest_date(self) -> dt.date:
        return self._df["Date"].max().date()

    def first_date(self) -> dt.date:
        return self._df["Date"].min().date()

    # -- 조회 -------------------------------------------------------------------
    def panel(self, start, end, columns=None, use_cache=True) -> pd.DataFrame:
        self.panel_calls.append((start, end))
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        out = self._df[(self._df["Date"] >= s) & (self._df["Date"] <= e)]
        if out.empty:
            raise DataUnavailable(f"{start} ~ {end} 구간에 데이터가 없습니다")
        if columns is not None:
            keep = list(dict.fromkeys(["Date", "Code", *columns]))
            out = out[[c for c in keep if c in out.columns]]
        return out.reset_index(drop=True)

    def bars(self, code, start=None, end=None) -> pd.DataFrame:
        m = self._df["Code"] == str(code)
        if start is not None:
            m &= self._df["Date"] >= pd.Timestamp(start)
        if end is not None:
            m &= self._df["Date"] <= pd.Timestamp(end)
        out = self._df[m]
        if out.empty:
            raise DataUnavailable(f"{code} 데이터가 없습니다")
        return out.set_index("Date").sort_index()

    def names(self, on=None) -> Dict[str, str]:
        snap = self._df.drop_duplicates("Code", keep="last")
        return dict(zip(snap["Code"], snap["Name"]))


# ======================================================================================
# 픽스처
# ======================================================================================


@pytest.fixture(scope="session")
def dates() -> List[dt.date]:
    return trading_dates()


@pytest.fixture(scope="session")
def strategy1_json() -> dict:
    return json.loads((ROOT / "strategies" / "strategy1.json").read_text(encoding="utf-8"))


@pytest.fixture
def strategy1(strategy1_json, dates) -> dict:
    """전략1 원본 + 합성 데이터 구간에 맞춘 period."""
    s = json.loads(json.dumps(strategy1_json))
    s["period"] = {"start": dates[START_BAR].isoformat(), "end": "auto"}
    return s


def _filler(dates):
    return [
        make_frame("100030", "필러에이", "flat", dates),
        make_frame("100040", "필러비", "flat", dates, market="KOSDAQ"),
    ]


@pytest.fixture
def tp_store(dates) -> FakeStore:
    """익절 시나리오 종목 + 아무 일도 없는 필러 2종목."""
    return FakeStore([make_frame("100010", "테스트에이", "tp", dates), *_filler(dates)])


@pytest.fixture
def sl_store(dates) -> FakeStore:
    """손절 시나리오 종목 + 필러 2종목."""
    return FakeStore([make_frame("100020", "테스트비", "sl", dates), *_filler(dates)])


@pytest.fixture
def flat_store(dates) -> FakeStore:
    """기준일이 아예 안 나오는 스토어."""
    return FakeStore(_filler(dates))


@pytest.fixture
def ohlcv(dates) -> pd.DataFrame:
    """지표/DSL 테스트용 단일 종목 일봉 (index=Date, marcap 컬럼 표기)."""
    return make_frame("100010", "테스트에이", "tp", dates).set_index("Date")


@pytest.fixture
def rng_ohlcv() -> pd.DataFrame:
    """난수 워크 일봉 — 지표 수치 검증용."""
    rs = np.random.default_rng(20260725)
    n = 300
    close = 10000 * np.exp(np.cumsum(rs.normal(0, 0.015, n)))
    high = close * (1 + np.abs(rs.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rs.normal(0, 0.008, n)))
    open_ = np.concatenate(([close[0]], close[:-1]))
    vol = rs.integers(10_000, 500_000, n).astype("float64")
    idx = pd.to_datetime(trading_dates(n, "2023-01-02"))
    return pd.DataFrame(
        {
            "Open": open_, "High": np.maximum(high, np.maximum(open_, close)),
            "Low": np.minimum(low, np.minimum(open_, close)), "Close": close,
            "Volume": vol, "Amount": close * vol,
        },
        index=idx,
    )


# ======================================================================================
# FakeDartStore — 실제 DartStore 를 그대로 쓰되 데이터만 메모리로 바꾼다
# ======================================================================================


class FakeDartStore(DartStore):
    """파일 대신 메모리 프레임을 쓰는 ``DartStore``.

    **as-of 로직은 진짜 ``DartStore`` 것을 그대로 쓴다.** 이 클래스는 ``_files`` / ``load`` /
    ``_last_fetch`` 만 갈아끼운다. 그래야 룩어헤드 테스트가 실제 구현을 검증한다.
    """

    def __init__(self, rows, last_fetch="2026-08-05 14:20:00", available=True):
        super().__init__(root="__fake_dart__")
        self._available = bool(available)
        self._last = last_fetch
        df = pd.DataFrame(list(rows)) if rows else pd.DataFrame(columns=["code"])
        self._fake = DartStore._normalize(df) if len(df) else None
        if self._fake is None:
            self._fake = DartStore._normalize(
                pd.DataFrame([{"code": "000000", "year": 2000, "quarter": 4}])
            ).iloc[0:0]

    # -- 데이터 소스만 교체 -------------------------------------------------------
    def _files(self):
        return {2025: Path("__fake__/fundamentals-2025.parquet")} if self._available else {}

    def load(self):
        if not self._available:
            raise DataUnavailable("가짜 DART 저장소에 데이터가 없습니다")
        return self._fake

    def _last_fetch(self, files):
        return self._last


def dart_row(code, year, quarter, disclosed_at, *,
             debt_ratio_pct=None, current_ratio_pct=None, op_income_quarter=None,
             부채총계=None, 자본총계=None, 유동자산=None, 유동부채=None):
    """``fundamentals-YYYY.parquet`` 한 줄."""
    return {
        "code": str(code), "corp_code": f"C{code}", "year": int(year), "quarter": int(quarter),
        "disclosed_at": pd.Timestamp(disclosed_at), "fs_div": "CFS", "currency": "KRW",
        "유동자산": 유동자산, "유동부채": 유동부채, "자산총계": None,
        "부채총계": 부채총계, "자본총계": 자본총계,
        "매출액": None, "영업이익": None, "당기순이익": None,
        "debt_ratio_pct": debt_ratio_pct, "current_ratio_pct": current_ratio_pct,
        "op_income_quarter": op_income_quarter,
    }


def dart_history(code, *, debt_ratio_pct, current_ratio_pct, op_income_quarter,
                 years=(2024, 2025, 2026), disclose_month_day=(("Q1", 5, 15), ("Q2", 8, 14),
                                                                ("Q3", 11, 14), ("Q4", 3, 20))):
    """한 종목의 여러 분기 재무를 한 번에 만든다 (합성 시나리오 기간을 덮도록)."""
    rows = []
    for y in years:
        for q, (label, mm, dd) in enumerate(disclose_month_day, start=1):
            disclosed = dt.date(y + 1, mm, dd) if label == "Q4" else dt.date(y, mm, dd)
            rows.append(dart_row(
                code, y, q, disclosed,
                debt_ratio_pct=debt_ratio_pct, current_ratio_pct=current_ratio_pct,
                op_income_quarter=op_income_quarter,
            ))
    return rows


@pytest.fixture
def dart_rows():
    return dart_row


@pytest.fixture
def make_dart():
    return FakeDartStore
