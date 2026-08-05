"""marcap parquet 로더 + 캐시.

``marcap/data/marcap-YYYY.parquet`` 을 **필요한 연도만** lazy 로 읽는다.
읽은 연도는 프로세스 메모리에 캐시하고, ``panel()`` 결과는 ``cache/panel-<start>-<end>.feather``
로도 저장한다.

marcap 폴더가 없으면 ``available == False`` 이고 모든 조회는 ``DataUnavailable`` 을 던진다.
**단, 이 모듈의 import 는 데이터가 없어도 반드시 성공한다.**
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .errors import DataUnavailable

__all__ = ["MarcapStore", "MARCAP_COLUMNS", "NUMERIC_COLUMNS"]


#: marcap parquet 의 컬럼 (``ChagesRatio`` 오타는 원본 그대로 유지한다)
MARCAP_COLUMNS = [
    "Date", "Rank", "Code", "Name", "Open", "High", "Low", "Close",
    "Volume", "Amount", "Changes", "ChangeCode", "ChagesRatio",
    "Marcap", "Stocks", "MarketId", "Market", "Dept",
]

NUMERIC_COLUMNS = [
    "Rank", "Open", "High", "Low", "Close", "Volume", "Amount",
    "Changes", "ChagesRatio", "Marcap", "Stocks",
]

_YEAR_RE = re.compile(r"marcap-(\d{4})\.parquet$", re.IGNORECASE)


def _concat_columnwise(frames: List[pd.DataFrame], beat) -> pd.DataFrame:
    """여러 해치 프레임을 **컬럼 단위로** 이어붙인다.

    ``pd.concat(frames)`` 은 700만 행에서 수 초가 걸리는 단일 연산이라 그 사이 취소를 받을 수 없다.
    컬럼마다 나눠 붙이면 각 단계가 수백 ms 라 진행률·취소가 계속 살아 있다.
    """
    cols = list(frames[0].columns)
    n = len(cols)
    data = {}
    for i, c in enumerate(cols):
        parts = [f[c] for f in frames if c in f.columns]
        data[c] = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0].reset_index(drop=True)
        beat(0.05 + 0.2 * (i + 1) / max(n, 1))
    return pd.DataFrame(data, columns=cols)


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
    ):
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
    #: 연도 단위 메모리 캐시로 이미 재사용되고, 쓰기가 수 초씩 걸려 취소를 막는다.
    CACHE_MAX_ROWS = 1_500_000

    def load_year(self, year: int, columns: Sequence[str] | None = None,
                  on_chunk: Optional[Callable[[float], None]] = None) -> pd.DataFrame:
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
        self._years[ckey] = df
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
        return pa.Table.from_batches(batches).to_pandas()

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
                df[c] = pd.to_numeric(df[c], errors="coerce")
        beat(0.6)
        for c in ("Name", "Market", "MarketId", "Dept"):
            if c in df.columns:
                df[c] = df[c].astype(str).fillna("")
        beat(0.8)
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

        cache_path = self.cache_dir / f"panel-{s.isoformat()}-{e.isoformat()}.feather"
        if use_cache and columns is None and cache_path.is_file():
            try:
                df = pd.read_feather(cache_path)
                df["Date"] = pd.to_datetime(df["Date"])
                return df
            except Exception:  # pragma: no cover - 깨진 캐시는 무시하고 재생성
                pass

        cols = None
        if columns is not None:
            cols = list(dict.fromkeys(["Date", "Code", *columns]))

        frames = []
        years = self._range_years(s, e)
        for i, y in enumerate(years):
            if on_year is not None:
                on_year(i, len(years), y)
            sub_cb = None
            if on_year is not None:
                def sub_cb(frac, _i=i, _n=len(years), _y=y):   # noqa: F811
                    on_year(_i + frac, _n, _y)
            df = self.load_year(y, columns=cols, on_chunk=sub_cb)
            if on_year is not None:
                on_year(i + 1.0, len(years), y)
            if cols is not None and len(df.columns) != len(cols):
                keep = [c for c in cols if c in df.columns]
                df = df[keep]
            m = (df["Date"] >= pd.Timestamp(s)) & (df["Date"] <= pd.Timestamp(e))
            if m.any():
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
        beat(0.3)

        if use_cache and columns is None and len(out) <= self.CACHE_MAX_ROWS:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                out.to_feather(cache_path)
            except Exception:  # pragma: no cover - 캐시 실패는 치명적이지 않다
                pass
        beat(0.3)
        return out

    def bars(self, code: str, start=None, end=None) -> pd.DataFrame:
        """한 종목의 일봉. index=Date, 컬럼은 marcap 원본 표기(Open/High/.../Amount/Name)."""
        files = self._require()
        code = str(code).zfill(6)
        s = _to_date(start) if start is not None else dt.date(min(files), 1, 1)
        e = _to_date(end) if end is not None else dt.date(max(files), 12, 31)
        frames = []
        for y in self._range_years(s, e):
            df = self.load_year(y)
            m = (
                (df["Code"] == code)
                & (df["Date"] >= pd.Timestamp(s))
                & (df["Date"] <= pd.Timestamp(e))
            )
            if m.any():
                frames.append(df.loc[m])
        if not frames:
            raise DataUnavailable(f"{code} 의 {s} ~ {e} 구간 데이터가 없습니다")
        out = pd.concat(frames, ignore_index=True)
        return out.set_index("Date").sort_index()

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
