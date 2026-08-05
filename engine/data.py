"""marcap parquet 로더 + 캐시.

``marcap/data/marcap-YYYY.parquet`` 을 **필요한 연도만** lazy 로 읽는다.
읽은 연도는 프로세스 메모리에 캐시하고, ``panel()`` 결과는 ``cache/panel-<start>-<end>.feather``
로도 저장한다.

marcap 폴더가 없으면 ``available == False`` 이고 모든 조회는 ``DataUnavailable`` 을 던진다.
**단, 이 모듈의 import 는 데이터가 없어도 반드시 성공한다.**
"""

from __future__ import annotations

import datetime as dt
import gc
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .errors import DataUnavailable

__all__ = ["MarcapStore", "MARCAP_COLUMNS", "NUMERIC_COLUMNS",
           "halted_mask", "HALTED_COLUMN", "PRICE_LIMIT_PCT", "code_variants",
           "adjustment_factors", "apply_price_adjustment", "ADJUST_STOCKS_RATIO",
           "ADJUST_LIMITATIONS"]


#: marcap parquet 의 컬럼 (``ChagesRatio`` 오타는 원본 그대로 유지한다)
MARCAP_COLUMNS = [
    "Date", "Rank", "Code", "Name", "Open", "High", "Low", "Close",
    "Volume", "Amount", "Changes", "ChangeCode", "ChagesRatio",
    "Marcap", "Stocks", "MarketId", "Market", "Dept",
]

NUMERIC_COLUMNS = [
    "Rank", "Open", "High", "Low", "Close", "Volume", "Amount",
    "Changes", "ChagesRatio", "ChangesRatio", "Marcap", "Stocks",
]

#: float32 로 저장해도 **값이 정확히 보존되는** 컬럼.
#: KRX 주가는 정수이고 최댓값이 7,749,000 이라 float32 의 정확 정수 한계(16,777,216) 안이다.
#: 전체 1,553만 행에 대해 float64→float32→float64 왕복 불일치 0건을 확인했다.
FLOAT32_COLUMNS = ("Open", "High", "Low", "Close")

#: 반드시 float64 로 둬야 하는 컬럼 (조 단위라 float32 로는 값이 달라진다).
FLOAT64_COLUMNS = ("Volume", "Amount", "Marcap", "Stocks")

#: 반복 문자열이라 category 로 두면 메모리가 크게 준다.
CATEGORY_COLUMNS = ("Code", "Name", "Market", "MarketId", "Dept", "ChangeCode")

#: 거래정지 판정 결과가 담기는 파생 컬럼 (parquet 에는 없다)
HALTED_COLUMN = "halted"

#: KRX 일간 가격제한폭(%). 이걸 넘는 변동은 정지해제 갭이거나 권리락/액면분할이다.
PRICE_LIMIT_PCT = 30.0

#: 수정주가 이벤트 판정 임계값. ``Stocks`` 가 이 배수 이상 늘거나 그 역수 이하로 줄면 이벤트로 본다.
ADJUST_STOCKS_RATIO = 1.5

#: 수정주가로 잡히지 않는 것들 — 결과 페이로드에 그대로 실어 사용자에게 알린다
ADJUST_LIMITATIONS = [
    "액면분할·무상증자·액면병합은 상장주식수 변화로 정확히 반영됩니다.",
    "유상증자는 반영되지 않습니다. 주식수 증가율과 가격 조정 비율이 다르기 때문입니다.",
    "배당락은 반영되지 않습니다. 상장주식수가 변하지 않습니다.",
    "합병·분할 등 주식수와 가격이 함께 바뀌는 사건은 근사치입니다.",
]

_YEAR_RE = re.compile(r"marcap-(\d{4})\.parquet$", re.IGNORECASE)

#: ``bars()`` 기본 컬럼. 차트/UI 가 실제로 쓰는 것만 읽는다 (읽기 비용이 컬럼 수에 비례한다).
#: 더 필요하면 ``columns=`` 로 지정하거나 ``columns="all"`` 을 쓴다.
BARS_DEFAULT_COLUMNS = [
    "Date", "Code", "Name", "Open", "High", "Low", "Close", "Volume", "Amount",
]

#: 종목별 캐시 폴더 (``cache/sym/<code>.parquet``)
SYMBOL_CACHE_DIRNAME = "sym"

#: 종목 캐시 폴더 총 크기 상한. 넘으면 오래된 것부터 지운다.
SYMBOL_CACHE_MAX_BYTES = 200 * 1024 * 1024

#: 이 개수 이하의 연도만 바뀌었으면 그 연도만 다시 읽어 캐시를 이어붙인다(증분 갱신).
SYMBOL_CACHE_INCREMENTAL_MAX_YEARS = 3


def halted_mask(df: pd.DataFrame):
    """**체결에 쓸 수 없는 봉**(거래정지일) 마스크. 컬럼이 없으면 ``None``.

    KRX 는 거래정지일에 시가·고가·저가를 0 으로, 종가만 **기준가**로 발표한다.
    이걸 그대로 두면 ``low <= 목표가`` 같은 조건이 **무조건 참**이 되어
    아무도 거래할 수 없는 날에 체결된 것으로 계산된다.

    판정: ``Volume == 0`` 이고 ``Open/High/Low`` 중 0 이 있으면 거래정지.
    여기에 더해 **OHLC 중 하나라도 0 이하면 거래량과 무관하게 체결 불가**로 본다
    (실제로 2026-05-08 라피치처럼 OHLC=0 인데 거래량이 찍힌 행이 존재한다).

    종가는 기준가로 유효하므로 이동평균 등 지표 계산에는 그대로 쓴다.
    """
    need = ("Open", "High", "Low", "Close")
    if not all(c in df.columns for c in need):
        return None
    o = pd.to_numeric(df["Open"], errors="coerce").to_numpy("float64")
    h = pd.to_numeric(df["High"], errors="coerce").to_numpy("float64")
    l = pd.to_numeric(df["Low"], errors="coerce").to_numpy("float64")
    c = pd.to_numeric(df["Close"], errors="coerce").to_numpy("float64")
    bad = ~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c))
    return bad | (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)


def adjustment_factors(df: pd.DataFrame, threshold: float = ADJUST_STOCKS_RATIO):
    """상장주식수(``Stocks``) 변화로 수정주가 계수를 계산한다.

    marcap 은 **KRX 원본 시세**라 액면분할·무상증자가 반영돼 있지 않다.
    삼성전자 2018-05-04 50:1 분할이면 2,650,000원이 51,900원이 되는데,
    조정하지 않으면 백테스트가 이걸 **하루 -98% 폭락**으로 계산한다.

    ``배율 = Stocks(당일) / Stocks(전일)`` 이 임계값을 넘으면 이벤트로 보고,
    그 이전 모든 봉에 ``1/배율`` 을 **소급** 적용한다.

    Returns
    -------
    (factor, stats) : (np.ndarray | None, dict)
        ``factor`` 는 각 행의 과거 가격에 곱할 계수 (마지막 시점 기준 = 1.0).
    """
    if "Stocks" not in df.columns or "Code" not in df.columns or len(df) == 0:
        return None, {"events": 0, "symbols": 0}
    stocks = pd.to_numeric(df["Stocks"], errors="coerce").astype("float64")
    code = df["Code"]
    prev = stocks.groupby(code, sort=False, observed=True).shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = stocks / prev
    lo = 1.0 / float(threshold)
    ev = (
        prev.notna() & (prev > 0) & stocks.notna() & (stocks > 0)
        & np.isfinite(ratio)
        & ((ratio >= float(threshold)) | (ratio <= lo))
    )
    n_ev = int(ev.sum())
    if n_ev == 0:
        return None, {"events": 0, "symbols": 0}

    r = ratio.where(ev, 1.0)
    grp = r.groupby(code, sort=False, observed=True)
    cum = grp.cumprod()
    last = cum.groupby(code, sort=False, observed=True).transform("last")
    with np.errstate(divide="ignore", invalid="ignore"):
        factor = np.array((cum / last).to_numpy("float64"), dtype="float64", copy=True)
    factor[~np.isfinite(factor)] = 1.0
    return factor, {"events": n_ev, "symbols": int(code[ev].nunique())}


def apply_price_adjustment(df: pd.DataFrame,
                           threshold: float = ADJUST_STOCKS_RATIO) -> dict:
    """``df`` 의 OHLC 를 제자리에서 수정주가로 바꾼다.

    * OHLC × 계수 (과거를 현재 기준으로 끌어내린다)
    * Volume ÷ 계수 (거래대금이 보존된다)
    * Amount·Marcap·Stocks 는 **건드리지 않는다** (이미 금액/원본 수치다)
    * 거래정지 봉의 0 은 0 × 계수 = 0 이라 그대로 남는다
    """
    factor, stats = adjustment_factors(df, threshold)
    stats = {"events": 0, "symbols": 0, **stats}
    if factor is None:
        stats["applied"] = False
        return stats
    for c in ("Open", "High", "Low", "Close"):
        if c in df.columns:
            dt_ = df[c].dtype
            df[c] = (df[c].to_numpy("float64") * factor).astype(dt_, copy=False)
    if "Volume" in df.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            v = df["Volume"].to_numpy("float64") / factor
        v[~np.isfinite(v)] = 0.0
        df["Volume"] = v
    stats["applied"] = True
    return stats


def _is_zero_padded(pf) -> bool:
    """이 parquet 의 ``Code`` 가 6자리로 0 채워 저장돼 있는가 (푸터 통계만 본다)."""
    try:
        rg = pf.metadata.row_group(0)
        for i in range(rg.num_columns):
            col = rg.column(i)
            if col.path_in_schema != "Code":
                continue
            st = col.statistics
            if st is None or not st.has_min_max:
                return False
            lo, hi = str(st.min), str(st.max)
            return len(lo) == 6 and len(hi) == 6
    except Exception:  # pragma: no cover - 통계가 없으면 안전한 쪽으로
        return False
    return False


def code_variants(code: str) -> List[str]:
    """종목코드의 표기 변형.

    marcap 은 **1995~2000년 파일에서 앞자리 0 을 떼고 저장한다** (``005930`` → ``5930``).
    parquet 푸시다운 필터는 문자열을 그대로 비교하므로 변형을 전부 넣어야 과거 데이터가 누락되지 않는다.
    (삼성전자의 경우 이걸 빠뜨리면 7,876행 중 1,420행이 조용히 사라진다)
    """
    padded = str(code).strip().zfill(6)
    out = [padded]
    stripped = padded
    while stripped.startswith("0") and len(stripped) > 1:
        stripped = stripped[1:]
        out.append(stripped)
    return list(dict.fromkeys(out))


def _concat_columnwise(frames: List[pd.DataFrame], beat, consume: bool = True) -> pd.DataFrame:
    """여러 해치 프레임을 **컬럼 단위로** 이어붙인다.

    ``pd.concat(frames)`` 은 700만 행에서 수 초가 걸리는 단일 연산이라 그 사이 취소를 받을 수 없다.
    컬럼마다 나눠 붙이면 각 단계가 수백 ms 라 진행률·취소가 계속 살아 있다.
    """
    cols = list(frames[0].columns)
    n = len(cols)
    data = {}
    for i, c in enumerate(cols):
        parts = [f[c] for f in frames if c in f.columns]
        cat = bool(parts) and all(isinstance(x.dtype, pd.CategoricalDtype) for x in parts)
        if consume:
            # 붙인 컬럼은 원본에서 떼어내 바로 메모리를 돌려준다 (데이터를 두 벌 들지 않도록)
            for f in frames:
                if c in f.columns:
                    del f[c]
        if cat:
            from pandas.api.types import union_categoricals

            data[c] = pd.Series(union_categoricals([x.array for x in parts]), copy=False)
        elif len(parts) > 2:
            # 문자열 컬럼은 한 번에 붙이면 1초를 넘길 수 있어 절반씩 나눈다
            half = len(parts) // 2
            a = pd.concat(parts[:half], ignore_index=True)
            beat(0.05 + 0.2 * (i + 0.5) / max(n, 1))
            b = pd.concat(parts[half:], ignore_index=True)
            beat(0.05 + 0.2 * (i + 0.8) / max(n, 1))
            data[c] = pd.concat([a, b], ignore_index=True)
        else:
            data[c] = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0].reset_index(drop=True)
        parts.clear()
        beat(0.05 + 0.2 * (i + 1) / max(n, 1))
    out = pd.DataFrame(data, columns=cols)
    data.clear()
    beat(0.28)
    return out


def _to_date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.Timestamp(v).date()


class MarcapStore:
    """연도별 parquet 를 lazy 로드하는 시세 저장소."""

    def __init__(
        self,
        root: str | os.PathLike = "marcap",
        cache_dir: str | os.PathLike = "cache",
        status_file: str | os.PathLike | None = None,
        adjusted: bool = True,
        adjust_threshold: float = ADJUST_STOCKS_RATIO,
    ):
        #: 수정주가 적용 여부. marcap 은 KRX 원본이라 액면분할이 반영돼 있지 않다.
        #: 조정하지 않으면 삼성전자 2018-05-04 분할이 하루 -98% 폭락으로 계산된다.
        self.adjusted = bool(adjusted)
        self.adjust_threshold = float(adjust_threshold)
        #: 마지막 조회에서 적용된 수정주가 통계 (백테스트가 assumptions 에 실어 보낸다)
        self.last_adjustment: Dict[str, object] = {"applied": False, "events": 0, "symbols": 0}
        self.root = Path(root)
        self.data_dir = self.root / "data"
        self.cache_dir = Path(cache_dir)
        self.status_file = (
            Path(status_file)
            if status_file is not None
            else self.root.parent / "data_status.json"
        )
        self._years: Dict[int, pd.DataFrame] = {}
        self._names: Optional[Dict[str, str]] = None
        self._bounds: Optional[tuple] = None

    # ---------------------------------------------------------------- 파일 탐색
    def _files(self) -> Dict[int, Path]:
        out: Dict[int, Path] = {}
        for d in (self.data_dir, self.root):
            if not d.is_dir():
                continue
            for p in d.glob("marcap-*.parquet"):
                m = _YEAR_RE.search(p.name)
                if m:
                    out.setdefault(int(m.group(1)), p)
            if out:
                break
        return out

    @property
    def available(self) -> bool:
        """marcap 데이터가 실제로 읽을 수 있는 상태인지."""
        try:
            return bool(self._files())
        except OSError:
            return False

    @property
    def years(self) -> List[int]:
        return sorted(self._files())

    def _require(self) -> Dict[int, Path]:
        files = self._files()
        if not files:
            raise DataUnavailable(
                f"marcap 데이터를 찾을 수 없습니다: {self.data_dir}/marcap-YYYY.parquet "
                "— update_marcap.bat 또는 `git clone https://github.com/FinanceData/marcap` 을 실행하세요."
            )
        return files

    # ---------------------------------------------------------------- 연도 로드
    #: parquet 를 나눠 읽는 단위 (행). 취소 확인 간격을 짧게 유지하기 위한 값.
    CHUNK_ROWS = 250_000

    #: 이 행 수를 넘는 패널은 feather 캐시를 만들지 않는다.
    #: 쓰기가 수 초씩 걸려 취소를 막고, 파일도 수백 MB 가 된다.
    CACHE_MAX_ROWS = 1_500_000

    #: 메모리에 붙들고 있을 연도 프레임 최대 개수 (LRU).
    #: 여러 해를 한 번에 읽는 백테스트에서는 아예 캐시하지 않는다 — 안 그러면 데이터를 두 벌 든다.
    MAX_CACHED_YEARS = 3

    def load_year(self, year: int, columns: Sequence[str] | None = None,
                  on_chunk: Optional[Callable[[float], None]] = None,
                  cache: bool = True) -> pd.DataFrame:
        """한 해치 parquet 를 읽어 정규화된 DataFrame 으로 돌려준다 (Date 는 컬럼).

        ``on_chunk(fraction)`` 을 주면 파일을 **행 묶음 단위로 나눠 읽으면서** 매번 호출한다.
        ``fraction`` 은 0.0~1.0 의 진척도. 콜백이 예외를 던지면 그대로 전파된다
        (백테스트 취소가 연도 하나를 다 읽을 때까지 기다리지 않도록 하기 위한 훅).
        """
        files = self._require()
        path = files.get(int(year))
        if path is None:
            raise DataUnavailable(f"{year}년 marcap 파일이 없습니다: {self.data_dir}")
        ckey = (int(year), tuple(sorted(columns)) if columns else None)
        cached = self._years.get(ckey)
        if cached is None and columns:
            full = self._years.get((int(year), None))
            if full is not None:
                keep = [c for c in full.columns if c in set(columns) | {"Date", "Code"}]
                cached = full[keep]
        if cached is not None:
            if on_chunk is not None:
                on_chunk(1.0)
            return cached
        try:
            df = self._read_parquet(path, on_chunk, columns)
        except DataUnavailable:
            raise
        except Exception as e:
            if on_chunk is not None and not isinstance(e, (OSError, ValueError)):
                raise           # 콜백이 던진 취소 예외는 그대로 올린다
            raise DataUnavailable(f"{path} 를 읽을 수 없습니다: {e}") from e
        df = self._normalize(df, on_chunk)
        if cache:
            self._years[ckey] = df
            while len(self._years) > self.MAX_CACHED_YEARS:
                self._years.pop(next(iter(self._years)))
        return df

    def _read_parquet(self, path: Path,
                      on_chunk: Optional[Callable[[float], None]],
                      columns: Sequence[str] | None = None) -> pd.DataFrame:
        """행 묶음 단위로 나눠 읽는다 (취소 확인 지점을 파일 내부에도 만들기 위해).

        ``columns`` 를 주면 그 컬럼만 읽는다 — 읽기·concat 비용이 크게 줄어든다.
        """
        cols = list(columns) if columns else None
        if on_chunk is None:
            return pd.read_parquet(path, columns=cols)
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:  # pragma: no cover
            on_chunk(0.0)
            out = pd.read_parquet(path, columns=cols)
            on_chunk(0.9)
            return out

        pf = pq.ParquetFile(path)
        if cols:
            have = set(pf.schema_arrow.names)
            cols = [c for c in cols if c in have] or None
        total = max(int(pf.metadata.num_rows), 1)
        batches = []
        done = 0
        on_chunk(0.0)
        for batch in pf.iter_batches(batch_size=self.CHUNK_ROWS, columns=cols):
            batches.append(batch)
            done += batch.num_rows
            on_chunk(min(0.9 * done / total, 0.9))
        if not batches:
            return pd.read_parquet(path, columns=cols)
        tbl = pa.Table.from_batches(batches)
        batches.clear()
        # self_destruct: 변환하면서 arrow 버퍼를 바로 반납한다 (같은 데이터를 두 벌 들지 않는다)
        try:
            return tbl.to_pandas(self_destruct=True)
        except TypeError:  # pragma: no cover - 구버전 pyarrow
            return tbl.to_pandas()

    @staticmethod
    def _normalize(df: pd.DataFrame,
                   on_chunk: Optional[Callable[[float], None]] = None) -> pd.DataFrame:
        def beat(f: float) -> None:
            if on_chunk is not None:
                on_chunk(min(0.9 + 0.1 * f, 1.0))

        if "Date" not in df.columns:
            df = df.reset_index()
        if "Date" not in df.columns:
            raise DataUnavailable("marcap 파일에 Date 컬럼이 없습니다")
        df["Date"] = pd.to_datetime(df["Date"])
        beat(0.2)
        if "Code" in df.columns:
            df["Code"] = df["Code"].astype(str).str.zfill(6)
        beat(0.4)
        for c in NUMERIC_COLUMNS:
            if c in df.columns:
                col = pd.to_numeric(df[c], errors="coerce")
                if c in FLOAT32_COLUMNS:
                    col = col.astype("float32")      # 값 손실 없음 (FLOAT32_COLUMNS 주석 참고)
                elif c in FLOAT64_COLUMNS:
                    col = col.astype("float64")
                df[c] = col
        beat(0.6)
        for c in ("Name", "Market", "MarketId", "Dept"):
            if c in df.columns:
                df[c] = df[c].astype(str).fillna("")
        for c in CATEGORY_COLUMNS:
            if c in df.columns:
                df[c] = df[c].astype("category")
        beat(0.8)
        halted = halted_mask(df)
        if halted is not None:
            df[HALTED_COLUMN] = halted
        beat(0.9)
        # 이미 Date 오름차순이면 정렬을 건너뛴다 (7백만 행 정렬은 몇 초가 걸리고 중단할 수 없다)
        if not df["Date"].is_monotonic_increasing:
            df = df.sort_values(["Date", "Code"], kind="stable")
        beat(1.0)
        return df.reset_index(drop=True)

    def unload(self) -> None:
        """메모리 캐시 비우기."""
        self._years.clear()
        self._names = None

    # ---------------------------------------------------------------- 날짜 정보
    def _year_bounds(self) -> tuple:
        if self._bounds is not None:
            return self._bounds
        files = self._require()
        ys = sorted(files)
        first = self._read_dates(files[ys[0]]).min()
        last = self._read_dates(files[ys[-1]]).max()
        self._bounds = (_to_date(first), _to_date(last))
        return self._bounds

    @staticmethod
    def _read_dates(path: Path) -> pd.Series:
        try:
            d = pd.read_parquet(path, columns=["Date"])["Date"]
        except Exception:
            d = pd.read_parquet(path).reset_index()["Date"]
        return pd.to_datetime(d)

    def latest_date(self) -> dt.date:
        return self._year_bounds()[1]

    def first_date(self) -> dt.date:
        return self._year_bounds()[0]

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        """ARCHITECTURE 4-1 의 ``GET /api/status`` 페이로드."""
        meta = self._read_status_file()
        auto = meta.get("auto_sync") or {}
        payload = {
            "available": False,
            "last_sync": meta.get("last_sync") or None,
            "result": meta.get("result") or "never",
            "latest_trade_date": None,
            "first_trade_date": None,
            "file_count": 0,
            "git_rev": meta.get("git_rev") or None,
            "row_count": None,
            "auto_sync": {
                "registered": bool(auto.get("registered", False)),
                "time": auto.get("time"),
                "task": auto.get("task", "KRXBacktesterDataSync"),
            },
        }
        files = self._files()
        if not files:
            return payload

        payload["available"] = True
        payload["file_count"] = len(files)
        try:
            first, last = self._year_bounds()
            payload["first_trade_date"] = first.isoformat()
            payload["latest_trade_date"] = last.isoformat()
        except DataUnavailable:  # pragma: no cover
            pass
        payload["row_count"] = self._row_count(files)
        if not payload["git_rev"]:
            payload["git_rev"] = self._git_rev()
        return payload

    def _read_status_file(self) -> dict:
        for p in (self.status_file, Path("data_status.json")):
            try:
                if p and p.is_file():
                    # utf-8-sig: PowerShell 이 BOM 을 붙여 쓴 경우도 읽는다
                    return json.loads(p.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
        return {}

    @staticmethod
    def _row_count(files: Dict[int, Path]) -> Optional[int]:
        """parquet 메타데이터만 읽어 총 행 수를 센다 (데이터 로드 없음)."""
        try:
            import pyarrow.parquet as pq
        except ImportError:  # pragma: no cover
            return None
        total = 0
        try:
            for p in files.values():
                total += pq.ParquetFile(p).metadata.num_rows
        except Exception:  # pragma: no cover
            return None
        return total

    def _git_rev(self) -> Optional[str]:
        if not (self.root / ".git").exists():
            return None
        try:
            out = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            rev = out.stdout.strip()
            return rev or None
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None

    # ---------------------------------------------------------------- 조회
    def _range_years(self, start, end) -> List[int]:
        files = self._require()
        s = _to_date(start) if start is not None else min(files) and dt.date(min(files), 1, 1)
        e = _to_date(end) if end is not None else dt.date(max(files), 12, 31)
        return [y for y in sorted(files) if s.year <= y <= e.year]

    def panel(
        self,
        start,
        end,
        columns: Sequence[str] | None = None,
        use_cache: bool = True,
        on_year: Optional[Callable[[float, int, int], None]] = None,
        adjusted: Optional[bool] = None,
    ) -> pd.DataFrame:
        """전 종목 구간 데이터. MultiIndex 없이 ``Date`` / ``Code`` 컬럼을 갖는다.

        ``on_year(idx, total, year)`` 를 주면 진행 상황을 알린다. ``idx`` 는 **소수**로,
        연도 파일을 읽는 도중에도 (행 묶음 단위로) 여러 번 호출된다.
        진행률 보고와 취소 확인에 쓴다 — 콜백이 예외를 던지면 그대로 전파되므로
        parquet 한 개를 다 읽을 때까지 기다리지 않고 즉시 중단할 수 있다.
        """
        self._require()
        s = _to_date(start)
        e = _to_date(end)
        if e < s:
            raise DataUnavailable(f"구간이 뒤집혔습니다: {s} ~ {e}")

        adj = self.adjusted if adjusted is None else bool(adjusted)
        tag = "adj" if adj else "raw"
        cache_path = self.cache_dir / f"panel-{s.isoformat()}-{e.isoformat()}-{tag}.feather"
        if use_cache and columns is None and cache_path.is_file():
            try:
                df = pd.read_feather(cache_path)
                df["Date"] = pd.to_datetime(df["Date"])
                return df
            except Exception:  # pragma: no cover - 깨진 캐시는 무시하고 재생성
                pass

        cols = None
        drop_stocks = False
        if columns is not None:
            wanted = list(columns)
            if adj and "Stocks" not in wanted:
                wanted.append("Stocks")      # 수정주가 계수 계산에만 쓰고 끝나면 버린다
                drop_stocks = True
            cols = list(dict.fromkeys(["Date", "Code", *wanted]))

        frames = []
        years = self._range_years(s, e)
        cache_years = len(years) <= self.MAX_CACHED_YEARS
        for i, y in enumerate(years):
            if on_year is not None:
                on_year(i, len(years), y)
            sub_cb = None
            if on_year is not None:
                def sub_cb(frac, _i=i, _n=len(years), _y=y):   # noqa: F811
                    on_year(_i + frac, _n, _y)
            df = self.load_year(y, columns=cols, on_chunk=sub_cb, cache=cache_years)
            if on_year is not None:
                on_year(i + 1.0, len(years), y)
            gc.collect()          # 직전 해의 중간 산출물을 바로 반납해 피크를 낮춘다
            if cols is not None:
                # halted 는 parquet 에 없는 파생 컬럼이라 cols 에 안 들어 있다.
                # 여기서 떨어뜨리면 백테스트가 거래정지일을 못 걸러낸다.
                keep = [c for c in cols if c in df.columns]
                if HALTED_COLUMN in df.columns and HALTED_COLUMN not in keep:
                    keep.append(HALTED_COLUMN)
                if len(keep) != len(df.columns):
                    df = df[keep]
            m = (df["Date"] >= pd.Timestamp(s)) & (df["Date"] <= pd.Timestamp(e))
            if bool(m.all()):
                # 그 해가 통째로 구간 안이면 복사하지 않는다.
                # (캐시 중이면 아래 concat 이 컬럼을 떼어가므로 반드시 사본을 넘긴다)
                frames.append(df.copy() if cache_years else df)
            elif m.any():
                frames.append(df.loc[m])
        if not frames:
            raise DataUnavailable(f"{s} ~ {e} 구간에 데이터가 없습니다")
        last_year = years[-1] if years else 0

        def beat(msg_idx: float) -> None:
            if on_year is not None:
                on_year(len(years) + msg_idx, len(years), last_year)

        beat(0.0)
        if len(frames) == 1:
            out = frames[0].reset_index(drop=True)
        else:
            out = _concat_columnwise(frames, beat)
            frames.clear()
        beat(0.3)

        self.last_adjustment = {"applied": False, "events": 0, "symbols": 0}
        if adj:
            self.last_adjustment = apply_price_adjustment(out, self.adjust_threshold)
            if drop_stocks and "Stocks" in out.columns:
                del out["Stocks"]
            beat(0.35)

        if use_cache and columns is None and len(out) <= self.CACHE_MAX_ROWS:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                out.to_feather(cache_path)
            except Exception:  # pragma: no cover - 캐시 실패는 치명적이지 않다
                pass
        beat(0.3)
        return out

    def bars(self, code: str, start=None, end=None,
             columns: Sequence[str] | str | None = None,
             use_cache: bool = True,
             adjusted: Optional[bool] = None) -> pd.DataFrame:
        """한 종목의 일봉. index=Date, 컬럼은 marcap 원본 표기(Open/High/.../Amount/Name).

        연도 파일을 통째로 읽지 않고 **parquet 푸시다운으로 그 종목만** 읽는다.
        전체 히스토리를 읽었으면 ``cache/sym/<code>.parquet`` 에 저장해 다음부터는 그걸 쓴다.

        Parameters
        ----------
        columns : list | "all" | None
            ``None`` 이면 ``BARS_DEFAULT_COLUMNS``. ``"all"`` 이면 파일의 전 컬럼.
        """
        files = self._require()
        code = str(code).zfill(6)
        s = _to_date(start) if start is not None else dt.date(min(files), 1, 1)
        e = _to_date(end) if end is not None else dt.date(max(files), 12, 31)
        if e < s:
            raise DataUnavailable(f"구간이 뒤집혔습니다: {s} ~ {e}")

        adj = self.adjusted if adjusted is None else bool(adjusted)
        asked = None if columns == "all" else list(columns or BARS_DEFAULT_COLUMNS)
        cols = asked
        drop_stocks = False
        if cols is not None and adj and "Stocks" not in cols:
            cols = cols + ["Stocks"]
            drop_stocks = True
        years = self._range_years(s, e)
        if adj:
            # 수정주가는 **최신 시점 기준**이라 요청 구간 뒤의 분할도 알아야 한다.
            # 그래야 같은 종목을 어떤 구간으로 물어도 같은 가격이 나온다.
            years = [y for y in sorted(files) if y >= min(years or [s.year])]

        frame = None
        if use_cache:
            frame = self._symbol_cache_get(code, cols, years, adj)
        if frame is None:
            frame = self._read_symbol(code, cols, years)
            if frame is not None and len(frame) and adj:
                self.last_adjustment = apply_price_adjustment(frame, self.adjust_threshold)
            if use_cache and frame is not None and len(frame):
                self._symbol_cache_put(code, frame, cols, years, adj)

        if frame is None or not len(frame):
            raise DataUnavailable(f"{code} 의 {s} ~ {e} 구간 데이터가 없습니다")

        m = (frame["Date"] >= pd.Timestamp(s)) & (frame["Date"] <= pd.Timestamp(e))
        out = frame.loc[m]
        if not len(out):
            raise DataUnavailable(f"{code} 의 {s} ~ {e} 구간 데이터가 없습니다")
        if drop_stocks and "Stocks" in out.columns:
            out = out.drop(columns=["Stocks"])
        return out.set_index("Date").sort_index()

    # ---------------------------------------------------------------- 단일 종목 읽기
    def _year_from_memory(self, year: int, cols: Sequence[str] | None) -> Optional[pd.DataFrame]:
        """이미 메모리에 올라온 연도 프레임 중 필요한 컬럼을 다 가진 것."""
        need = set(cols or ())
        for (y, _ck), df in self._years.items():
            if y != int(year):
                continue
            if not need or need <= set(df.columns):
                return df
        return None

    def _read_symbol(self, code: str, cols: Sequence[str] | None,
                     years: Sequence[int]) -> Optional[pd.DataFrame]:
        """연도별로 그 종목만 읽어 붙인다. **연도 캐시(``_years``)를 오염시키지 않는다.**"""
        files = self._require()
        variants = code_variants(code)
        frames = []
        for y in years:
            path = files.get(int(y))
            if path is None:
                continue
            mem = self._year_from_memory(y, cols)
            if mem is not None:
                m = mem["Code"] == code
                if bool(m.any()):
                    keep = [c for c in (cols or mem.columns) if c in mem.columns]
                    if HALTED_COLUMN in mem.columns and HALTED_COLUMN not in keep:
                        keep.append(HALTED_COLUMN)
                    frames.append(mem.loc[m, keep])
                continue
            part = self._read_symbol_year(path, variants, cols)
            if part is not None and len(part):
                frames.append(part)
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        return self._normalize(out)

    @staticmethod
    def _read_symbol_year(path: Path, variants: Sequence[str],
                          cols: Sequence[str] | None) -> Optional[pd.DataFrame]:
        """parquet 필터 푸시다운으로 한 종목만 읽는다 (연도 전체를 메모리에 올리지 않는다)."""
        try:
            import pyarrow.parquet as pq
        except ImportError:  # pragma: no cover
            df = pd.read_parquet(path, columns=list(cols) if cols else None)
            return df[df["Code"].astype(str).isin(list(variants))]
        try:
            pf = pq.ParquetFile(path)
            have = set(pf.schema_arrow.names)
            use = [c for c in cols if c in have] if cols else None
            # 그 파일이 6자리로 저장돼 있으면 == 하나로 끝난다 (in 보다 눈에 띄게 빠르다).
            # 앞자리 0 을 떼고 저장한 옛 파일에서만 변형 목록을 쓴다.
            key = variants[0] if _is_zero_padded(pf) else list(variants)
            flt = [("Code", "==", key)] if isinstance(key, str) else [("Code", "in", key)]
            tb = pq.read_table(path, columns=use, filters=flt)
        except Exception:  # pragma: no cover - 구버전 pyarrow / 이상한 파일은 통째로 읽는다
            df = pd.read_parquet(path, columns=list(cols) if cols else None)
            return df[df["Code"].astype(str).isin(list(variants))]
        if tb.num_rows == 0:
            return None
        return tb.to_pandas()

    # ---------------------------------------------------------------- 종목 캐시
    @property
    def symbol_cache_dir(self) -> Path:
        return self.cache_dir / SYMBOL_CACHE_DIRNAME

    def _symbol_cache_paths(self, code: str, adjusted: bool = True) -> tuple:
        """조정/미조정 캐시는 **절대 섞이면 안 된다.** 파일명으로 분리한다."""
        d = self.symbol_cache_dir
        tag = "" if adjusted else ".raw"
        return d / f"{code}{tag}.parquet", d / f"{code}{tag}.meta.json"

    def _source_fingerprint(self) -> Dict[str, int]:
        """연도 파일들의 mtime. 데이터가 갱신되면 값이 바뀐다."""
        out: Dict[str, int] = {}
        for y, p in self._files().items():
            try:
                out[str(y)] = int(p.stat().st_mtime_ns)
            except OSError:  # pragma: no cover
                continue
        return out

    def _symbol_cache_get(self, code: str, cols: Sequence[str] | None,
                          want_years: Sequence[int],
                          adjusted: bool = True) -> Optional[pd.DataFrame]:
        """유효한 종목 캐시를 돌려준다.

        * 요청 연도가 캐시가 담고 있는 연도에 **없으면 그 연도만** 읽어 이어붙인다(점진 로딩)
        * 원본 파일이 갱신된 연도는 그 연도만 다시 읽는다(증분 갱신)
        """
        pq_path, meta_path = self._symbol_cache_paths(code, adjusted)
        if not (pq_path.is_file() and meta_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        cached_cols = meta.get("columns")
        if cols is not None and cached_cols is not None and not set(cols) <= set(cached_cols):
            return None                       # 캐시에 없는 컬럼을 요구한다 → 다시 읽는다
        if cols is None and cached_cols is not None:
            return None                       # columns="all" 인데 캐시는 일부만 갖고 있다

        cur = self._source_fingerprint()
        old = {str(k): int(v) for k, v in (meta.get("files") or {}).items()}
        stale = sorted({int(y) for y in cur if old.get(y) != cur[y]}
                       | {int(y) for y in old if y not in cur})
        if meta.get("last_sync") != (self._read_status_file().get("last_sync") or None):
            stale = sorted({int(y) for y in cur})

        try:
            df = pd.read_parquet(pq_path)
            df["Date"] = pd.to_datetime(df["Date"])
        except Exception:  # pragma: no cover - 깨진 캐시
            return None

        covered = {int(y) for y in (meta.get("years") or [])}
        missing = sorted({int(y) for y in want_years} - covered)
        refresh = sorted(set(stale) | set(missing))
        if not refresh:
            return df
        if len(refresh) > SYMBOL_CACHE_INCREMENTAL_MAX_YEARS and len(missing) == 0:
            return None                       # 원본이 많이 바뀌었다 → 전체 재생성

        # 바뀐 연도의 행은 버리고, 부족한/바뀐 연도만 읽어 이어붙인다
        keep = df[~df["Date"].dt.year.isin(refresh)]
        known = {int(k) for k in cur}
        # 조정된 캐시에 **원본**을 이어붙이면 가격 기준이 어긋난다 → 통째로 다시 만든다
        if adjusted:
            return None
        fresh = self._read_symbol(code, cols, [y for y in refresh if y in known])
        parts = [x for x in (keep, fresh) if x is not None and len(x)]
        if not parts:
            return None
        merged = self._normalize(pd.concat(parts, ignore_index=True))
        self._symbol_cache_put(code, merged, cols, sorted(covered | set(refresh)), adjusted)
        return merged

    def _symbol_cache_put(self, code: str, df: pd.DataFrame,
                          cols: Sequence[str] | None,
                          years: Sequence[int] = (),
                          adjusted: bool = True) -> None:
        pq_path, meta_path = self._symbol_cache_paths(code, adjusted)
        try:
            self.symbol_cache_dir.mkdir(parents=True, exist_ok=True)
            df.reset_index(drop=True).to_parquet(pq_path, index=False)
            meta_path.write_text(
                json.dumps(
                    {
                        "code": code,
                        "columns": list(cols) if cols is not None else None,
                        "years": sorted(int(y) for y in years),
                        "adjusted": bool(adjusted),
                        "rows": int(len(df)),
                        "first": str(df["Date"].min().date()) if len(df) else None,
                        "last": str(df["Date"].max().date()) if len(df) else None,
                        "files": self._source_fingerprint(),
                        "last_sync": self._read_status_file().get("last_sync") or None,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:  # pragma: no cover - 캐시 실패는 치명적이지 않다
            return
        self._trim_symbol_cache()

    def _trim_symbol_cache(self, max_bytes: int = SYMBOL_CACHE_MAX_BYTES) -> None:
        """폴더 크기 상한을 넘으면 오래된 것부터 지운다."""
        d = self.symbol_cache_dir
        try:
            items = []
            total = 0
            for p in d.glob("*.parquet"):
                st = p.stat()
                items.append((st.st_mtime, st.st_size, p))
                total += st.st_size
            if total <= max_bytes:
                return
            for _mt, size, p in sorted(items):
                p.unlink(missing_ok=True)
                p.with_suffix("").with_suffix(".meta.json").unlink(missing_ok=True)
                (d / f"{p.stem}.meta.json").unlink(missing_ok=True)
                total -= size
                if total <= max_bytes:
                    break
        except OSError:  # pragma: no cover
            return

    def clear_symbol_cache(self) -> int:
        """종목 캐시를 전부 지운다. 지운 파일 수를 돌려준다."""
        n = 0
        try:
            for p in self.symbol_cache_dir.glob("*"):
                p.unlink(missing_ok=True)
                n += 1
        except OSError:  # pragma: no cover
            pass
        return n

    def names(self, on: dt.date | None = None) -> Dict[str, str]:
        """``{code: name}``. 가장 최근 거래일 기준."""
        if self._names is not None and on is None:
            return self._names
        files = self._require()
        day = _to_date(on) if on is not None else self.latest_date()
        df = self.load_year(day.year)
        snap = df.loc[df["Date"] == pd.Timestamp(day), ["Code", "Name"]]
        if snap.empty:
            snap = df[["Code", "Name"]].drop_duplicates(subset="Code", keep="last")
        mapping = dict(zip(snap["Code"], snap["Name"]))
        if on is None:
            self._names = mapping
        return mapping

    # ---------------------------------------------------------------- 편의
    def trading_days(self, start, end) -> List[dt.date]:
        p = self.panel(start, end, columns=["Close"])
        return [d.date() for d in pd.DatetimeIndex(sorted(p["Date"].unique()))]

    def __repr__(self) -> str:  # pragma: no cover
        return f"MarcapStore(root={str(self.root)!r}, available={self.available})"
