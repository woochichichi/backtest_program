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

자본잠식 함정 — 부채비율 필터만 믿으면 안 된다
---------------------------------------------
자본총계가 0 이하면 부채비율(부채총계/자본총계)은 음수나 무한대가 되어 의미가 없다.
그래서 수집기는 이 경우 ``debt_ratio_pct`` 를 **비워 둔다**(``pd.NA``).

문제는 여기서 생긴다. ``fund["debt_ratio_pct"] < 200`` 같은 비교는 ``pd.NA`` 를 떨어뜨리지만,
``universe.filters.on_missing`` 이 ``"include"``(현재 기본값)면 **값이 없는 종목은 그냥 통과한다.**
즉 **가장 위험한 자본잠식 종목이 "부채비율 200% 미만" 을 통과해 버린다.**

그래서 ``capital_impaired`` 를 따로 실어 보낸다. 부채비율과 **별개로** 명시적으로 걸러라.

``capital_impaired`` 는 ``True``(자본잠식) / ``False``(정상) / ``pd.NA``(자본총계를 모름)
**3값**이다. ``pd.NA`` 가 섞인 마스크를 그대로 인덱서에 넣으면 pandas 판마다 동작이 달라지므로
**항상 ``fillna`` 로 "모름" 을 어떻게 볼지 명시**해라::

    fund = store.as_of_panel(codes, today)
    imp = fund["capital_impaired"]

    # on_missing == "include" — 모르는 종목은 통과시킨다 (자본잠식만 떨군다)
    not_impaired = ~imp.fillna(False).astype(bool)

    # on_missing == "exclude" — 정상이라고 확인된 종목만 남긴다 (모름도 제외)
    not_impaired = (imp == False).fillna(False).astype(bool)   # noqa: E712

    ok = (fund["debt_ratio_pct"] < 200) & not_impaired

``imp != True`` 같은 축약은 쓰지 마라. ``pd.NA != True`` 는 ``pd.NA`` 라서
"모름" 을 어느 쪽으로 처리할지가 pandas 구현에 맡겨진다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
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
#:
#: ``capital_impaired`` 는 **3값 논리**다. ``True``=자본잠식 / ``False``=정상 /
#: ``pd.NA``=자본총계를 모름. "모름" 을 ``False`` 로 뭉개면 안 된다 (아래 주의 참고).
DERIVED_COLUMNS = [
    "debt_ratio_pct", "current_ratio_pct", "op_income_quarter", "capital_impaired",
]

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


def _opt_bool(v) -> Optional[bool]:
    """``True`` / ``False`` / ``None``(모름) 을 그대로 보존한다.

    **``None`` 을 ``False`` 로 바꾸지 마라.** "자본잠식이 아니다" 와 "자본총계를 모른다" 는
    전략 입장에서 전혀 다른 이야기다.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return bool(v)


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


#: ``code_id`` 와 공시일을 정수 하나로 합칠 때 쓰는 자릿수.
#: 공시일은 1970-01-01 이후 일수(2015년 ≈ 16,400) 이므로 2^32 면 충분히 넉넉하다.
_KEY_SCALE = 1 << 32

#: ``as_of_panel`` 이 종목 집합 -> id 변환 결과를 캐시하는 최대 개수.
#: 백테스트는 보통 유니버스 1~2개만 반복해 쓴다. 항목당 종목 수 x 8바이트라
#: 2,700종목 기준 22KB 정도. 상한을 넘으면 통째로 버린다.
_CIDS_CACHE_MAX = 4

#: ``rank``(연도*4+분기, 최대 2026*4+4 = 8,108) 위에 종목 id 를 얹을 때 쓰는 자릿수.
_RANK_SCALE = 1 << 16


class _AsOfIndex:
    """as-of 조회를 ``searchsorted`` 한 번으로 끝내기 위한 색인.

    왜 필요한가
    -----------
    ``disclosed_at <= date`` 를 매 호출마다 전 행(12만 행)에 걸어 비교하면
    한 번에 20~35ms 가 든다. 백테스트는 이걸 수백 번 부른다.

    어떻게 하나
    -----------
    행을 ``(종목, 공시일, 연도, 분기)`` 순으로 한 번만 정렬해 두고,
    ``key = code_id * 2^32 + 공시일`` 이라는 **단조 증가 정수 키**를 만든다.
    그러면 "이 종목의 공시된 행 범위" 는 ``np.searchsorted`` 로 O(log n) 에 잘린다.
    여러 종목을 한꺼번에 물어도 벡터 연산 한 번이다.

    ``best_pos[i]`` 에는 "그 종목의 처음부터 i 번째까지 중 가장 최신 (연도,분기)"
    행의 위치를 미리 넣어 둔다. 그래서 자른 구간의 **마지막 원소 하나만 보면** 답이 나온다.

    날짜 순서에 의존하지 않는다
    ---------------------------
    상태를 들고 있지 않으므로 조회일이 뒤로 갔다가 앞으로 와도 결과가 같다.
    **as-of 컷오프는 ``DartStore._date_ordinal`` 한 곳에서만 걸린다.**

    메모리
    ------
    행당 int64 4개(키·행위치·최적위치·구간) 정도. 12만 행이면 약 4MB 다.
    원본 프레임을 복사하지 않고 **위치만** 들고 있다.
    """

    __slots__ = ("codes", "code_to_id", "keys", "row_pos", "best_pos",
                 "seg_start", "seg_end", "day_min", "n", "all_cids")

    def __init__(self, df: pd.DataFrame):
        disc = df["disclosed_at"]
        # 공시일을 모르는 행은 어떤 시점에도 '공시된' 적이 없으므로 색인에서 뺀다.
        pos = np.flatnonzero(disc.notna().to_numpy())
        # factorize(sort=True) 는 np.unique 와 결과가 같으면서 7배 빠르다
        # (pandas string dtype 을 object 로 풀어헤치지 않는다).
        code_id, uniques = pd.factorize(df["code"].iloc[pos], sort=True)
        self.codes = np.asarray(uniques, dtype=object).astype(str)
        self.code_to_id = {c: i for i, c in enumerate(self.codes)}
        self.all_cids = np.arange(len(self.codes), dtype=np.int64)

        day = (disc.to_numpy()[pos].astype("datetime64[D]").astype("int64"))
        year = pd.to_numeric(df["year"], errors="coerce").to_numpy(
            dtype="float64", na_value=0.0)[pos].astype("int64")
        quarter = pd.to_numeric(df["quarter"], errors="coerce").to_numpy(
            dtype="float64", na_value=0.0)[pos].astype("int64")

        # (종목, 공시일, 연도, 분기) 오름차순
        order = np.lexsort((quarter, year, day, code_id))
        pos, code_id, day = pos[order], code_id[order], day[order]
        rank = (year * 4 + quarter)[order]      # 회계 시점 순서

        self.n = int(pos.size)
        self.row_pos = pos.astype(np.int64)
        self.day_min = int(day.min()) if self.n else 0
        self.keys = code_id.astype(np.int64) * _KEY_SCALE + (day - self.day_min)

        n_codes = len(self.codes)
        self.seg_start = np.searchsorted(code_id, np.arange(n_codes), side="left")
        self.seg_end = np.searchsorted(code_id, np.arange(n_codes), side="right")

        # best_pos[i] = 구간 시작부터 i 까지 중 (연도,분기) 가 가장 큰 행의 위치.
        # 같은 (연도,분기) 가 여러 벌이면 뒤에 오는 것(= 나중에 공시된 정정)이 이긴다.
        #
        # 종목마다 루프를 돌지 않고 한 번에 계산한다. rank 에 code_id * 2^16 을 얹으면
        # 뒤 종목의 값이 앞 종목의 어떤 값보다도 크므로, 전역 누적 최대값이
        # 종목 경계에서 저절로 초기화된다 (rank 최대값 2026*4+4 < 2^16).
        if self.n:
            keyed = code_id.astype(np.int64) * _RANK_SCALE + rank
            is_best = np.maximum.accumulate(keyed) == keyed
            best_local = np.maximum.accumulate(
                np.where(is_best, np.arange(self.n, dtype=np.int64), -1)
            )
            self.best_pos = self.row_pos[best_local]
        else:
            self.best_pos = np.empty(0, np.int64)

    # -- 조회 -------------------------------------------------------------------------
    def _offset(self, day_ordinal: int) -> int:
        """``day_min`` 기준 오프셋. 데이터 시작보다 이르면 음수가 되어 구간이 빈다."""
        return int(day_ordinal) - self.day_min

    def end_of(self, cid: int, day_ordinal: int) -> int:
        """종목 ``cid`` 에서 ``disclosed_at <= day_ordinal`` 인 행의 끝 위치."""
        off = self._offset(day_ordinal)
        if off < 0:
            return int(self.seg_start[cid])
        return int(np.searchsorted(self.keys, cid * _KEY_SCALE + off, side="right"))

    def ends_of(self, cids: np.ndarray, day_ordinal: int) -> np.ndarray:
        """여러 종목을 한 번에. ``cids`` 는 오름차순이어야 한다."""
        off = self._offset(day_ordinal)
        if off < 0:
            return self.seg_start[cids].copy()
        return np.searchsorted(self.keys, cids * _KEY_SCALE + off, side="right")


class DartStore:
    """연도별 ``fundamentals-YYYY.parquet`` 를 읽어 as-of 조회를 제공한다.

    전체 데이터가 12년 × 2,700종목 × 4분기 ≈ 13만 행 정도라 한 번에 읽어 캐시한다.
    조회는 ``_AsOfIndex`` 를 통해 ``searchsorted`` 로 처리한다.
    """

    def __init__(self, root: str | os.PathLike = "dart"):
        self.root = Path(root)
        self._frame: Optional[pd.DataFrame] = None
        self._corp_map: Optional[pd.DataFrame] = None
        self._asof: Optional[_AsOfIndex] = None
        self._cids_cache: Dict[tuple, np.ndarray] = {}

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
        # 3값 논리를 지키려면 nullable boolean 이어야 한다. 예전 파일에는 이 컬럼이
        # 아예 없어서 통째로 pd.NA 가 되는데, 그게 정확히 "모름" 이라 옳다.
        df["capital_impaired"] = pd.array(
            [_opt_bool(v) for v in df["capital_impaired"]], dtype="boolean"
        )
        # 같은 (code, year, quarter) 가 여러 벌이면(정정공시) **여기서 지우지 않는다.**
        # 지우면 정정 전 시점의 조회가 정정 후 숫자를 보게 되거나(룩어헤드),
        # 정정 전 숫자가 통째로 사라진다. 어느 것을 쓸지는 as-of 필터를 통과한
        # 뒤에 고른다. 정렬만 해 둔다.
        return df.sort_values(
            ["code", "year", "quarter", "disclosed_at"], kind="stable"
        ).reset_index(drop=True)

    def unload(self) -> None:
        """메모리 캐시 비우기. 색인도 함께 버린다."""
        self._frame = None
        self._corp_map = None
        self._asof = None
        self._cids_cache.clear()

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

    # ---------------------------------------------------------------- as-of 색인
    def _index(self) -> "_AsOfIndex":
        """as-of 조회용 색인. 한 번만 만들고 재사용한다 (``unload()`` 로 버린다)."""
        if self._asof is None:
            self._asof = _AsOfIndex(self.load())
            self._cids_cache.clear()      # 종목 id 매핑이 바뀌므로 반드시 함께 버린다
        return self._asof

    def _date_ordinal(self, date) -> int:
        """조회일을 '1970-01-01 이후 일수' 로.

        **as-of 컷오프가 실제로 걸리는 유일한 지점이다.** 색인은 공시일 오름차순으로
        정렬되어 있고, 여기서 나온 값보다 큰 공시일은 ``searchsorted`` 가 잘라낸다.
        """
        return (pd.Timestamp(_to_date(date)).to_datetime64()
                .astype("datetime64[D]").astype("int64").item())

    def _disclosed(self, date) -> pd.DataFrame:
        """``disclosed_at <= date`` 인 행 전부.

        느린 참조 구현이다. 실제 조회(``as_of`` / ``as_of_panel`` /
        ``consecutive_profit_quarters``)는 색인을 쓴다. 검증·디버깅용으로 남겨 둔다.
        """
        df = self.load()
        cutoff = pd.Timestamp(_to_date(date))
        return df[df["disclosed_at"].notna() & (df["disclosed_at"] <= cutoff)]

    # ---------------------------------------------------------------- as-of 조회
    def as_of(self, code: str, date) -> Optional[dict]:
        """``date`` 시점에 **이미 공시되어 있던** 가장 최신 재무 1건.

        2025Q1 이 2025-05-15 에 공시됐다면 ``as_of(code, "2025-05-14")`` 는 2024Q4 를 준다.
        해당 종목의 공시가 하나도 없으면 ``None``.
        """
        idx = self._index()
        cid = idx.code_to_id.get(str(code).zfill(6))
        if cid is None:
            return None
        end = idx.end_of(cid, self._date_ordinal(date))
        if end <= idx.seg_start[cid]:
            return None
        return self._row_to_dict(self.load().iloc[int(idx.best_pos[end - 1])])

    def as_of_panel(self, codes: Optional[Iterable[str]], date) -> pd.DataFrame:
        """백테스트 스캔용 벡터 조회.

        ``codes`` 의 각 종목에 대해 ``date`` 시점 최신 재무 1행씩. index 는 ``code``.
        ``codes=None`` 이면 그 시점에 공시가 있는 전 종목.
        데이터가 하나도 없으면 규격만 맞는 **빈 DataFrame** 을 돌려준다(예외 아님).
        """
        idx = self._index()
        if codes is None:
            cids = idx.all_cids
        else:
            # 백테스트는 같은 종목 집합으로 날짜만 바꿔 가며 부른다.
            # 문자열 → id 변환(1,800종목에 0.7ms)이 매번 반복되지 않게 작게 캐시한다.
            key = tuple(codes)
            cids = self._cids_cache.get(key)
            if cids is None:
                lookup = idx.code_to_id
                cids = np.fromiter(
                    (lookup[c] for c in {str(x).zfill(6) for x in codes} if c in lookup),
                    dtype=np.int64,
                )
                cids.sort()
                if len(self._cids_cache) >= _CIDS_CACHE_MAX:
                    self._cids_cache.clear()      # 상한을 넘으면 통째로 버린다
                self._cids_cache[key] = cids
        if cids.size == 0:
            return self._empty_panel()

        ends = idx.ends_of(cids, self._date_ordinal(date))
        ok = ends > idx.seg_start[cids]
        if not ok.any():
            return self._empty_panel()

        rows = idx.best_pos[ends[ok] - 1]
        out = self.load().iloc[rows]
        return out.set_index("code").sort_index()

    def _empty_panel(self) -> pd.DataFrame:
        return self.load().iloc[0:0].set_index("code")

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
        out["capital_impaired"] = _opt_bool(row["capital_impaired"])
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
        idx = self._index()
        cid = idx.code_to_id.get(str(code).zfill(6))
        if cid is None:
            return None
        start = int(idx.seg_start[cid])
        end = idx.end_of(cid, self._date_ordinal(date))
        if end <= start:
            return None
        # 이 종목의 '이미 공시된' 행들만 잘라낸다. 공시일 오름차순이라
        # 같은 (연도,분기) 가 여러 벌이면 뒤에 오는 정정공시가 앞을 덮는다.
        sub = self.load().iloc[idx.row_pos[start:end]]

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
