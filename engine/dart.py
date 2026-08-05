"""DART 재무 데이터 저장소 (``dart/fundamentals-YYYY.parquet``).

``MarcapStore`` 와 같은 감각으로 쓴다.

    from engine.dart import DartStore

    store = DartStore(root="dart")
    store.available                                # -> bool
    store.status()                                 # -> dict
    store.as_of("005930", "2025-04-01")            # -> dict | None
    store.as_of_panel(["005930", "000660"], "2025-04-01")   # -> pd.DataFrame
    store.consecutive_profit_quarters("005930", "2025-04-01")  # -> int | None

데이터가 없으면 ``available == False`` 이고 조회는 ``DataUnavailable`` 을 던진다.
**단, 이 모듈의 import 는 데이터가 없어도 반드시 성공한다.**

as-of 규칙 — 이게 이 모듈의 존재 이유다
--------------------------------------
재무제표는 분기 종료 후 45~90일 뒤에 공시된다. 2025년 1분기 재무를 2025-04-01 에 쓰면
**그때는 존재하지도 않던 정보로 매매한 셈**이 된다(룩어헤드 편향). 그래서 모든 조회는
``disclosed_at <= 조회일`` 인 행만 본다. ``as_of()`` 를 쓰지 않고 원시 프레임을 직접
필터링하는 코드는 이 보호를 우회하므로 쓰지 마라.

이 프로젝트에는 같은 성격의 편향으로 전략 수익률이 +122% → -14% 로 뒤집힌 전례가 있다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .errors import DataUnavailable

__all__ = [
    "DartStore",
    "AMOUNT_COLUMNS",
    "DERIVED_COLUMNS",
    "FUNDAMENTAL_COLUMNS",
    "STATE_FILENAME",
    "CORP_MAP_FILENAME",
]

#: 원 단위 금액 컬럼 (nullable Int64)
AMOUNT_COLUMNS = [
    "유동자산", "유동부채", "자산총계", "부채총계", "자본총계",
    "매출액", "영업이익", "당기순이익",
]

#: 파생 컬럼
DERIVED_COLUMNS = ["debt_ratio_pct", "current_ratio_pct", "op_income_quarter"]

META_COLUMNS = ["code", "corp_code", "year", "quarter", "disclosed_at", "fs_div", "currency"]

FUNDAMENTAL_COLUMNS = META_COLUMNS + AMOUNT_COLUMNS + DERIVED_COLUMNS

STATE_FILENAME = ".fetch_state.json"
CORP_MAP_FILENAME = "corp_map.parquet"

_YEAR_RE = re.compile(r"fundamentals-(\d{4})\.parquet$", re.IGNORECASE)

#: ``consecutive_profit_quarters`` 가 거슬러 올라갈 최대 분기 수
DEFAULT_PROFIT_LOOKBACK = 8


def _to_date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.Timestamp(v).date()


def _opt_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DartStore:
    """연도별 ``fundamentals-YYYY.parquet`` 를 읽어 as-of 조회를 제공한다.

    전체 데이터가 12년 × 2,700종목 × 4분기 ≈ 13만 행 정도라 한 번에 읽어 캐시한다.
    """

    def __init__(self, root: str | os.PathLike = "dart"):
        self.root = Path(root)
        self._frame: Optional[pd.DataFrame] = None
        self._corp_map: Optional[pd.DataFrame] = None

    # ---------------------------------------------------------------- 파일 탐색
    def _files(self) -> Dict[int, Path]:
        out: Dict[int, Path] = {}
        try:
            if not self.root.is_dir():
                return out
            for p in self.root.glob("fundamentals-*.parquet"):
                m = _YEAR_RE.search(p.name)
                if m:
                    out[int(m.group(1))] = p
        except OSError:  # pragma: no cover
            return {}
        return out

    @property
    def available(self) -> bool:
        """DART 재무 데이터를 실제로 읽을 수 있는 상태인지. 폴더가 없으면 ``False``."""
        try:
            return bool(self._files())
        except OSError:  # pragma: no cover
            return False

    @property
    def years(self) -> List[int]:
        return sorted(self._files())

    def _require(self) -> Dict[int, Path]:
        files = self._files()
        if not files:
            raise DataUnavailable(
                f"DART 재무 데이터를 찾을 수 없습니다: {self.root}/fundamentals-YYYY.parquet "
                "— test_dart.bat 으로 연결을 확인한 뒤 update_dart.bat 을 실행하세요."
            )
        return files

    # ---------------------------------------------------------------- 로드
    def load(self) -> pd.DataFrame:
        """전 연도를 하나의 프레임으로 읽어 캐시한다 (``code``, ``year``, ``quarter`` 정렬)."""
        if self._frame is not None:
            return self._frame
        files = self._require()
        frames = []
        for _y, p in sorted(files.items()):
            try:
                frames.append(pd.read_parquet(p))
            except Exception as e:  # pragma: no cover - 손상 파일
                raise DataUnavailable(f"{p} 를 읽을 수 없습니다: {e}") from e
        if not frames:  # pragma: no cover
            raise DataUnavailable(f"{self.root} 에 읽을 수 있는 재무 데이터가 없습니다")
        df = pd.concat(frames, ignore_index=True)
        self._frame = self._normalize(df)
        return self._frame

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        for c in FUNDAMENTAL_COLUMNS:
            if c not in df.columns:
                df[c] = pd.NA
        df = df[FUNDAMENTAL_COLUMNS].copy()
        df["code"] = df["code"].astype("string").str.zfill(6)
        df["corp_code"] = df["corp_code"].astype("string")
        df["fs_div"] = df["fs_div"].astype("string")
        df["currency"] = df["currency"].astype("string")
        df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int32")
        df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce").astype("Int8")
        df["disclosed_at"] = pd.to_datetime(df["disclosed_at"], errors="coerce")
        for c in AMOUNT_COLUMNS + ["op_income_quarter"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        for c in ("debt_ratio_pct", "current_ratio_pct"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        # 같은 (code, year, quarter) 가 여러 벌이면(정정공시) **여기서 지우지 않는다.**
        # 지우면 정정 전 시점의 조회가 정정 후 숫자를 보게 되거나(룩어헤드),
        # 정정 전 숫자가 통째로 사라진다. 어느 것을 쓸지는 as-of 필터를 통과한
        # 뒤에 고른다. 정렬만 해 둔다.
        return df.sort_values(
            ["code", "year", "quarter", "disclosed_at"], kind="stable"
        ).reset_index(drop=True)

    def unload(self) -> None:
        """메모리 캐시 비우기."""
        self._frame = None
        self._corp_map = None

    def corp_map(self) -> pd.DataFrame:
        """``corp_map.parquet`` (corp_code ↔ 종목코드). 없으면 빈 DataFrame."""
        if self._corp_map is not None:
            return self._corp_map
        p = self.root / CORP_MAP_FILENAME
        if not p.is_file():
            self._corp_map = pd.DataFrame(columns=["corp_code", "corp_name", "code"])
            return self._corp_map
        try:
            self._corp_map = pd.read_parquet(p)
        except Exception:  # pragma: no cover
            self._corp_map = pd.DataFrame(columns=["corp_code", "corp_name", "code"])
        return self._corp_map

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        """``{"available","last_fetch","year_range","row_count","codes"}``."""
        payload = {
            "available": False,
            "last_fetch": None,
            "year_range": None,
            "row_count": 0,
            "codes": 0,
        }
        files = self._files()
        if not files:
            return payload

        payload["available"] = True
        payload["year_range"] = [min(files), max(files)]
        payload["last_fetch"] = self._last_fetch(files)
        try:
            df = self.load()
        except DataUnavailable:  # pragma: no cover
            return payload
        payload["row_count"] = int(len(df))
        payload["codes"] = int(df["code"].nunique())
        return payload

    def _last_fetch(self, files: Dict[int, Path]) -> Optional[str]:
        p = self.root / STATE_FILENAME
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8-sig"))
                for k in ("updated_at", "corp_map_fetched_at"):
                    v = raw.get(k)
                    if v:
                        return str(v)
                quarters = raw.get("quarters") or {}
                stamps = [
                    str(v.get("fetched_at"))
                    for v in quarters.values()
                    if isinstance(v, dict) and v.get("fetched_at")
                ]
                if stamps:
                    return max(stamps)
        except (OSError, ValueError):  # pragma: no cover
            pass
        try:
            newest = max(p.stat().st_mtime for p in files.values())
            return dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:  # pragma: no cover
            return None

    # ---------------------------------------------------------------- as-of 조회
    def _disclosed(self, date) -> pd.DataFrame:
        """``disclosed_at <= date`` 인 행만. **모든 조회의 입구.**"""
        df = self.load()
        cutoff = pd.Timestamp(_to_date(date))
        return df[df["disclosed_at"].notna() & (df["disclosed_at"] <= cutoff)]

    def as_of(self, code: str, date) -> Optional[dict]:
        """``date`` 시점에 **이미 공시되어 있던** 가장 최신 재무 1건.

        2025Q1 이 2025-05-15 에 공시됐다면 ``as_of(code, "2025-05-14")`` 는 2024Q4 를 준다.
        해당 종목의 공시가 하나도 없으면 ``None``.
        """
        code = str(code).zfill(6)
        sub = self._disclosed(date)
        sub = sub[sub["code"] == code]
        if sub.empty:
            return None
        sub = sub.sort_values(
            ["year", "quarter", "disclosed_at"], kind="stable", ascending=True
        )
        return self._row_to_dict(sub.iloc[-1])

    def as_of_panel(self, codes: Optional[Iterable[str]], date) -> pd.DataFrame:
        """백테스트 스캔용 벡터 조회.

        ``codes`` 의 각 종목에 대해 ``date`` 시점 최신 재무 1행씩. index 는 ``code``.
        ``codes=None`` 이면 그 시점에 공시가 있는 전 종목.
        데이터가 하나도 없으면 규격만 맞는 **빈 DataFrame** 을 돌려준다(예외 아님).
        """
        sub = self._disclosed(date)
        if codes is not None:
            wanted = {str(c).zfill(6) for c in codes}
            sub = sub[sub["code"].isin(wanted)]
        if sub.empty:
            return self.load().iloc[0:0].set_index("code")
        sub = sub.sort_values(
            ["code", "year", "quarter", "disclosed_at"], kind="stable"
        ).drop_duplicates(subset="code", keep="last")
        return sub.set_index("code").sort_index()

    @staticmethod
    def _row_to_dict(row: pd.Series) -> dict:
        disclosed = row["disclosed_at"]
        out: Dict[str, object] = {
            "code": str(row["code"]),
            "corp_code": None if pd.isna(row["corp_code"]) else str(row["corp_code"]),
            "year": _opt_int(row["year"]),
            "quarter": _opt_int(row["quarter"]),
            "disclosed_at": None if pd.isna(disclosed) else pd.Timestamp(disclosed).date().isoformat(),
            "fs_div": None if pd.isna(row["fs_div"]) else str(row["fs_div"]),
            "currency": None if pd.isna(row["currency"]) else str(row["currency"]),
        }
        for c in AMOUNT_COLUMNS + ["op_income_quarter"]:
            out[c] = _opt_int(row[c])
        for c in ("debt_ratio_pct", "current_ratio_pct"):
            out[c] = _opt_float(row[c])
        return out

    # ---------------------------------------------------------------- 연속 흑자
    def consecutive_profit_quarters(
        self,
        code: str,
        date,
        limit: int = DEFAULT_PROFIT_LOOKBACK,
    ) -> Optional[int]:
        """``date`` 시점 기준 **확인된** 연속 흑자 분기 수. 모르면 ``None``.

        가장 최근 공시 분기부터 (연도, 분기) 를 1씩 거슬러 내려가며 ``op_income_quarter > 0``
        인 분기를 센다.

        - 적자(<= 0) 를 만나면 거기서 멈추고 지금까지 센 수를 돌려준다
        - **결측(값이 없거나 그 분기 행 자체가 없음) 은 흑자로 세지 않는다.** 거기서 멈춘다
        - 가장 최근 분기의 값 자체를 모르면(행 없음 / ``op_income_quarter`` 결측) ``None``

        결측 때문에 멈춘 경우와 적자로 멈춘 경우가 같은 숫자로 나오므로, 이 값은
        "최소 이만큼은 확실하다" 로 읽어야 한다. ``n >= 4`` 판정은 항상 보수적으로(적게)
        나오며 **없는 흑자를 만들어내지 않는다.**
        """
        code = str(code).zfill(6)
        sub = self._disclosed(date)
        sub = sub[sub["code"] == code]
        if sub.empty:
            return None

        by_period: Dict[tuple, Optional[int]] = {}
        for y, q, v in zip(sub["year"], sub["quarter"], sub["op_income_quarter"]):
            yi, qi = _opt_int(y), _opt_int(q)
            if yi is None or qi is None:
                continue
            by_period[(yi, qi)] = _opt_int(v)
        if not by_period:
            return None

        cur = max(by_period)
        if by_period.get(cur) is None:
            return None  # 가장 최근 분기를 모른다

        count = 0
        lim = max(1, int(limit))
        while count < lim:
            v = by_period.get(cur)
            if v is None or v <= 0:
                break
            count += 1
            y, q = cur
            cur = (y - 1, 4) if q == 1 else (y, q - 1)
        return count

    # ---------------------------------------------------------------- 편의
    def codes(self) -> List[str]:
        """데이터에 들어 있는 전체 종목코드."""
        return sorted(str(c) for c in self.load()["code"].dropna().unique())

    def __repr__(self) -> str:  # pragma: no cover
        return f"DartStore(root={str(self.root)!r}, available={self.available})"
