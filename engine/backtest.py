"""백테스트 엔진 — 스캔 → 진입 → 청산 → 포트폴리오.

    from engine.backtest import run_backtest
    result = run_backtest(strategy, store, progress=None)

``result`` 는 ARCHITECTURE 4-5 의 ``POST /api/backtest`` 응답과 **키 이름까지 동일한 dict** 다.
서버는 그대로 직렬화만 하면 된다.

체결 규칙은 ARCHITECTURE 2장을 그대로 따른다.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .dsl import EvalContext, evaluate_condition, evaluate_operand, safe_eval_expr
from .errors import BacktestCancelled, DataUnavailable, DSLError, StrategyError
from .indicators import REGISTRY
from .metrics import _i, build_equity, by_stock_summary, compute_metrics, monthly_returns
from .validate import validate_strategy

__all__ = ["run_backtest", "EOK", "SAME_DAY_EXIT_MODES", "DEFAULT_SAME_DAY_EXIT",
           "SUPPORTED_FILTERS"]

#: 1억
EOK = 100_000_000.0

#: signals 배열 최대 길이 (프런트 로그 탭 보호)
MAX_SIGNALS = 4000

#: ``execution.same_day_exit`` — 진입 체결 당일의 청산 평가 정책
SAME_DAY_EXIT_MODES = ("loss_only", "never", "always")

#: 기본값. 일봉만으로는 저가·고가 순서를 알 수 없으므로 보수적으로 잡는다.
DEFAULT_SAME_DAY_EXIT = "loss_only"

_PRICE_COLS = ["Open", "High", "Low", "Close", "Volume", "Amount", "Marcap"]

#: 진행률/취소 확인 주기 (초). 계약상 최소 1초에 한 번은 should_cancel 을 봐야 한다.
CANCEL_INTERVAL_SEC = 0.25
PROGRESS_INTERVAL_SEC = 0.5

PHASE_LOAD = "데이터 읽는 중"
PHASE_SCAN = "기준일 찾는 중"
PHASE_SIM = "종목별 매매 계산 중"
PHASE_METRICS = "성과 계산 중"

_MARKET_ALIASES = {
    "KOSPI": {"KOSPI", "STK", "유가증권", "유가증권시장"},
    "KOSDAQ": {"KOSDAQ", "KSQ", "코스닥", "코스닥시장"},
    "KONEX": {"KONEX", "KNX", "코넥스"},
}


# ======================================================================================
# 작은 헬퍼
# ======================================================================================


def _r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def _d(v) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.Timestamp(v).date()


class _Signals:
    """SCAN / MATCH / WATCH / FILL / POS / PNL / DONE 로그 수집기."""

    def __init__(self, limit: Optional[int] = None):
        self.items: List[dict] = []
        self.limit = int(limit if limit is not None else MAX_SIGNALS)
        self.truncated = False
        self.dropped = 0

    def add(self, day, level: str, code: Optional[str], message: str, hhmmss="09:00:00"):
        if len(self.items) >= self.limit:
            self.truncated = True
            self.dropped += 1
            return
        d = _d(day) if day is not None else None
        self.items.append(
            {
                "ts": f"{d.isoformat()} {hhmmss}" if d else "",
                "level": level,
                "code": code,
                "message": message,
            }
        )


class _Reporter:
    """진행률 보고 + 취소 확인 (ARCHITECTURE-v2 §2-1).

    * ``progress`` 는 v1 3인자 ``(done, total, message)`` 와
      v2 5인자 ``(done, total, message, phase, eta_sec)`` 를 **둘 다** 지원한다.
    * ``should_cancel()`` 은 최소 ``CANCEL_INTERVAL_SEC`` 마다 확인한다.
    * ``eta_sec`` 은 현재 단계의 실제 진척 속도로 추정하고, 근거가 없으면 ``None``.
    """

    def __init__(self, progress=None, should_cancel=None):
        self.progress = progress
        self.should_cancel = should_cancel
        self._ext = self._detect(progress)
        self._t0 = time.perf_counter()
        self._last_emit = 0.0
        self._last_cancel = 0.0
        self._anchor_done = 0.0
        self._anchor_t = self._t0
        self.phase = PHASE_LOAD
        self.max_gap = 0.0          # 진행률 호출 간격 최댓값 (계측용)
        self.calls = 0

    @staticmethod
    def _detect(progress) -> bool:
        if progress is None:
            return False
        try:
            inspect.signature(progress).bind(0, 100, "", None, None)
            return True
        except (TypeError, ValueError):
            return False

    # -- 취소 --------------------------------------------------------------------
    def check(self, force: bool = False) -> None:
        """취소 여부 확인. ``True`` 면 ``BacktestCancelled``."""
        if self.should_cancel is None:
            return
        now = time.perf_counter()
        if not force and (now - self._last_cancel) < CANCEL_INTERVAL_SEC:
            return
        self._last_cancel = now
        try:
            cancelled = bool(self.should_cancel())
        except Exception:  # pragma: no cover - 콜백 오류로 백테스트를 죽이지 않는다
            return
        if cancelled:
            raise BacktestCancelled("사용자가 취소했습니다.", phase=self.phase)

    # -- 진행률 ------------------------------------------------------------------
    def set_phase(self, phase: str, done: float) -> None:
        self.phase = phase
        self._anchor_done = float(done)
        self._anchor_t = time.perf_counter()

    def _eta(self, done: float, now: float):
        prog = done - self._anchor_done
        elapsed = now - self._anchor_t
        if prog <= 0.0 or elapsed < 0.5 or done >= 100:
            return None
        remaining = max(100.0 - done, 0.0)
        return round(elapsed / prog * remaining, 1)

    def emit(self, done: float, message: str, phase: str | None = None,
             force: bool = False) -> None:
        now = time.perf_counter()
        if not force and (now - self._last_emit) < PROGRESS_INTERVAL_SEC:
            return
        if self.calls:
            self.max_gap = max(self.max_gap, now - self._last_emit)
        self._last_emit = now
        self.calls += 1
        if phase:
            self.phase = phase
        if self.progress is None:
            return
        eta = self._eta(float(done), now)
        d = int(max(0, min(100, round(done))))
        try:
            if self._ext:
                self.progress(d, 100, message, self.phase, eta)
            else:
                self.progress(d, 100, message)
        except TypeError:
            self._ext = not self._ext
            try:
                if self._ext:
                    self.progress(d, 100, message, self.phase, eta)
                else:
                    self.progress(d, 100, message)
            except Exception:  # pragma: no cover
                pass
        except Exception:  # pragma: no cover - 콜백 오류가 백테스트를 죽이면 안 된다
            pass

    def tick(self, done: float, message: str, phase: str | None = None) -> None:
        """취소 확인 + 진행률 보고를 한 번에 (긴 루프 안에서 호출)."""
        self.check()
        self.emit(done, message, phase)


def _fmt(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ======================================================================================
# 유니버스 / 기준일 스캔  (1단계 — 전부 벡터 연산)
# ======================================================================================


def _market_mask(series: pd.Series, markets: Sequence[str]) -> pd.Series:
    allowed = set()
    for m in markets or []:
        allowed |= _MARKET_ALIASES.get(str(m).upper(), {str(m).upper()})
    s = series.astype(str).str.upper().str.strip()
    return s.isin(allowed)


#: ``universe.filters`` 중 시세(marcap)만으로 판정 가능한 키
SUPPORTED_FILTERS = {
    "market_cap_min_eok": ("시가총액 하한", "억"),
    "market_cap_max_eok": ("시가총액 상한", "억"),
    "price_min": ("주가 하한", "원"),
    "price_max": ("주가 상한", "원"),
    "amount_min_eok": ("거래대금 하한", "억"),
    "volume_min": ("거래량 하한", "주"),
}

#: DART 재무 데이터가 연결됐을 때만 판정 가능한 키
FINANCIAL_FILTERS = {
    "debt_ratio_max_pct": ("부채비율 상한", "%"),
    "current_ratio_min_pct": ("유동비율 하한", "%"),
    "profitable_quarters_min": ("영업이익 연속 흑자 분기", "분기"),
}

#: 필터가 아니라 필터의 동작을 정하는 키
FILTER_OPTION_KEYS = {"on_missing", "comment"}

#: 재무를 알 수 없는 종목 처리 정책
ON_MISSING_MODES = ("include", "exclude")
DEFAULT_ON_MISSING = "include"

_DEFAULT_IGNORED_REASON = "이 조건에 필요한 데이터가 marcap 에 없어 적용하지 않았습니다."
_NO_DART_REASON = (
    "DART 재무 데이터가 연결되지 않아 이 조건은 적용되지 않습니다. "
    "update_dart.bat 으로 재무 데이터를 받으면 자동으로 적용됩니다."
)


#: 항상 필요한 컬럼
_BASE_COLS = ["Date", "Code", "Name", "Open", "High", "Low", "Close", "Volume", "Amount"]


def _needed_columns(strategy: Mapping) -> List[str]:
    """전략이 실제로 쓰는 컬럼만 고른다 — 안 읽으면 그만큼 메모리가 안 든다."""
    cols = list(_BASE_COLS)
    universe = strategy.get("universe") or {}
    if universe.get("markets"):
        cols += ["Market", "MarketId"]
    ex = {str(e).upper() for e in (universe.get("exclude") or [])}
    if ex & {"ADMIN_ISSUE", "TRADE_HALT", "ETF", "ETN"}:
        cols.append("Dept")
    filters = universe.get("filters") or {}
    blob = json.dumps(strategy, ensure_ascii=False, default=str)
    if any(k.startswith("market_cap") for k in filters) or '"marcap"' in blob:
        cols.append("Marcap")
    return list(dict.fromkeys(cols))


def _indicator_aliases(strategy: Mapping) -> Dict[str, dict]:
    """``strategy["indicators"][].key`` → 지표 스펙. 조건식/수식에서 이름으로 참조한다."""
    out: Dict[str, dict] = {}
    for spec in strategy.get("indicators") or []:
        if not isinstance(spec, Mapping):
            continue
        key = spec.get("key")
        name = spec.get("type") or spec.get("indicator")
        if not isinstance(key, str) or not key.strip() or not isinstance(name, str):
            continue
        if name.upper() not in REGISTRY:
            continue
        params = {
            k: v for k, v in spec.items()
            if k not in ("key", "type", "indicator", "plot", "color", "label", "pane")
        }
        params["indicator"] = name
        out[key] = params
    return out


def _split_filters(strategy: Mapping, dart_ok: bool = False) -> Tuple[dict, dict, List[dict]]:
    """``universe.filters`` 를 (시세 필터, 재무 필터, 무시됨) 으로 나눈다.

    무시된 항목의 사유는 같은 경로를 가리키는 ``params[].unavailable_reason`` 을 우선 쓴다.
    ``dart_ok`` 가 False 면 재무 필터는 전부 무시 목록으로 간다.
    """
    filters = (strategy.get("universe") or {}).get("filters") or {}
    if not isinstance(filters, Mapping):
        return {}, {}, []

    by_path = {}
    for prm in strategy.get("params") or []:
        if isinstance(prm, Mapping) and isinstance(prm.get("path"), str):
            by_path[prm["path"]] = prm

    applied: dict = {}
    financial: dict = {}
    ignored: List[dict] = []
    for key, value in filters.items():
        if str(key).startswith("_") or key in FILTER_OPTION_KEYS:
            continue
        if value is None:
            continue
        if key in SUPPORTED_FILTERS:
            try:
                applied[key] = float(value)
            except (TypeError, ValueError):
                pass
            continue
        if key in FINANCIAL_FILTERS and dart_ok:
            try:
                financial[key] = float(value)
            except (TypeError, ValueError):
                pass
            continue

        prm = by_path.get(f"universe.filters.{key}") or {}
        label = prm.get("label") or FINANCIAL_FILTERS.get(key, (key, ""))[0]
        unit = prm.get("unit") or FINANCIAL_FILTERS.get(key, ("", ""))[1]
        if key in FINANCIAL_FILTERS:
            reason = _NO_DART_REASON if not prm.get("unavailable_reason") else prm["unavailable_reason"]
        else:
            reason = prm.get("unavailable_reason") or _DEFAULT_IGNORED_REASON
        ignored.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "reason": reason,
                "message": (
                    f"{label} 조건({_fmt(value) if isinstance(value, (int, float)) else value}"
                    f"{unit})은 적용하지 않았습니다. {reason} 실제보다 종목이 많이 잡힙니다."
                ),
            }
        )
    return applied, financial, ignored


def _eligible_codes(panel: pd.DataFrame, universe: Mapping,
                    filters: Mapping | None = None,
                    beat: Optional[Callable[[str], None]] = None) -> Tuple[pd.Index, dict]:
    """시장 / 제외 조건 / universe.filters 를 종목 단위로 적용한다 (각 종목의 최신 행 기준)."""
    def _b(msg: str) -> None:
        if beat is not None:
            beat(msg)

    _b("종목별 최신 시세 추리는 중")
    dup = panel["Code"].duplicated(keep="last")
    _b("종목 목록 만드는 중")
    last = panel.loc[~dup]
    _b("종목 선정 조건 확인 중")
    stats = {"total": int(len(last))}
    keep = pd.Series(True, index=last.index)

    markets = universe.get("markets") or []
    if markets and "Market" in last.columns:
        mm = _market_mask(last["Market"], markets)
        if "MarketId" in last.columns:
            mm |= _market_mask(last["MarketId"], markets)
        keep &= mm

    ex = {str(e).upper() for e in (universe.get("exclude") or [])}
    name = last["Name"].astype(str) if "Name" in last.columns else pd.Series("", index=last.index)
    dept = last["Dept"].astype(str) if "Dept" in last.columns else pd.Series("", index=last.index)
    code = last["Code"].astype(str)

    if "PREFERRED" in ex:
        # 우선주는 표준코드 끝자리가 0 이 아니거나 종목명이 '우'/'우B'/'우C' 로 끝난다
        keep &= ~((code.str[-1] != "0") | name.str.contains(r"우(?:B|C)?$", regex=True))
    if "SPAC" in ex:
        keep &= ~name.str.contains("스팩|기업인수목적", regex=True)
    if "ETF" in ex:
        keep &= ~dept.str.contains("ETF", regex=False)
        keep &= ~name.str.contains(
            r"KODEX|TIGER|KBSTAR|ARIRANG|KINDEX|HANARO|SOL |ACE |PLUS |RISE ", regex=True
        )
    if "ETN" in ex:
        keep &= ~dept.str.contains("ETN", regex=False)
        keep &= ~name.str.contains("ETN", regex=False)
    if "REIT" in ex:
        keep &= ~name.str.contains("리츠", regex=False)
    if "ADMIN_ISSUE" in ex:
        keep &= ~dept.str.contains("관리", regex=False)
    if "TRADE_HALT" in ex:
        keep &= ~dept.str.contains("정지", regex=False)

    f = dict(filters or {})
    if f:
        if "market_cap_min_eok" in f and "Marcap" in last.columns:
            keep &= last["Marcap"] >= f["market_cap_min_eok"] * EOK
        if "market_cap_max_eok" in f and "Marcap" in last.columns:
            keep &= last["Marcap"] <= f["market_cap_max_eok"] * EOK
        if "price_min" in f and "Close" in last.columns:
            keep &= last["Close"] >= f["price_min"]
        if "price_max" in f and "Close" in last.columns:
            keep &= last["Close"] <= f["price_max"]
        if "amount_min_eok" in f and "Amount" in last.columns:
            keep &= last["Amount"] >= f["amount_min_eok"] * EOK
        if "volume_min" in f and "Volume" in last.columns:
            keep &= last["Volume"] >= f["volume_min"]

    codes = pd.Index(last.loc[keep.fillna(False), "Code"].unique())
    stats["eligible"] = int(len(codes))
    return codes, stats


_PANEL_COLS = {
    "open": "Open", "high": "High", "low": "Low", "close": "Close",
    "volume": "Volume", "amount": "Amount", "marcap": "Marcap",
}


def _rule_params(rd: Mapping) -> dict:
    """reference_day 딕셔너리의 숫자 필드 = 조건식에서 쓸 수 있는 파라미터."""
    return {
        k: v for k, v in rd.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _cheap_operand(op: Any, params: Mapping):
    """패널 전체에 바로 적용 가능한 피연산자인가.

    ``("col", "Amount")`` / ``("scalar", 1e11)`` / ``None``(비쌈).
    """
    if isinstance(op, bool):
        return None
    if isinstance(op, (int, float)):
        return ("scalar", float(op))
    if isinstance(op, str):
        low = op.strip().lower()
        if low in _PANEL_COLS:
            return ("col", _PANEL_COLS[low])
        try:
            return ("scalar", float(op.strip()))
        except ValueError:
            pass
        v = params.get(op.strip())
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return ("scalar", float(v))
        return None
    if isinstance(op, Mapping) and "expr" in op:
        try:
            v = safe_eval_expr(op["expr"], EvalContext(pd.DataFrame(), params=params))
        except Exception:
            return None
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return ("scalar", float(v))
    return None


_CHEAP_CMP = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}


def _cheap_prefilter(when: Any, panel: pd.DataFrame, params: Mapping):
    """``when`` 중 패널 전체에 바로 걸 수 있는 조건만 뽑아 사전 필터를 만든다.

    이게 있으면 무거운 종목별 평가를 후보 종목에만 돌릴 수 있다 (ARCHITECTURE §3 성능 요구).
    """
    if not isinstance(when, Mapping):
        return None
    op = str(when.get("op") or "").lower()
    conds = when.get("conditions") or [] if op == "and" else [when]
    masks = []
    for c in conds:
        if not isinstance(c, Mapping):
            continue
        cop = str(c.get("op") or "").lower()
        fn = _CHEAP_CMP.get(cop)
        if fn is None:
            continue
        L = _cheap_operand(c.get("left"), params)
        R = _cheap_operand(c.get("right"), params)
        if not L or not R or (L[0] == "scalar" and R[0] == "scalar"):
            continue
        if (L[0] == "col" and L[1] not in panel.columns) or (R[0] == "col" and R[1] not in panel.columns):
            continue
        lv = panel[L[1]] if L[0] == "col" else L[1]
        rv = panel[R[1]] if R[0] == "col" else R[1]
        masks.append(fn(lv, rv))
    if not masks:
        return None
    m = masks[0]
    for x in masks[1:]:
        m = m & x
    return m.fillna(False)


def _custom_events(panel: pd.DataFrame, universe: Mapping, start: dt.date,
                   aliases: Mapping | None, rep) -> pd.DataFrame:
    """``reference_day.rule = "custom"`` — when 조건식으로 기준일을 찾는다."""
    rd = dict(universe.get("reference_day") or {})
    when = rd.get("when")
    params = _rule_params(rd)

    cheap = _cheap_prefilter(when, panel, params)
    if cheap is not None:
        hot = panel.loc[cheap & (panel["Date"] >= pd.Timestamp(start)), "Code"]
        codes = pd.Index(hot.unique())
    else:
        codes = pd.Index(panel["Code"].unique())
    if len(codes) == 0:
        return panel.iloc[0:0][[c for c in ("Code", "Date", "Amount", "Open", "High", "Low", "Close") if c in panel.columns]]

    cols = [c for c in _PRICE_COLS if c in panel.columns]
    total = len(codes)
    rows = []
    for j, (code, g) in enumerate(_iter_candidate_bars(
            panel, codes, rep, total, base=18.0, span=2.0, label="기준일 조건 확인 중")):
        df = g.set_index("Date")[cols]
        ctx = EvalContext(df, params=params, aliases=aliases)
        try:
            hits = evaluate_condition(when, ctx)
        except DSLError:
            continue
        if hits is True:
            idx = np.arange(len(df))
        else:
            idx = np.flatnonzero(np.asarray(hits, dtype=bool))
        if idx.size == 0:
            continue
        take = g.iloc[idx]
        rows.append(take.loc[take["Date"] >= pd.Timestamp(start)])
    if not rows:
        keep = [c for c in ("Code", "Date", "Amount", "Open", "High", "Low", "Close")
                if c in panel.columns]
        return panel.iloc[0:0][keep]
    out = pd.concat(rows, ignore_index=True)
    keep = [c for c in ("Code", "Date", "Amount", "Open", "High", "Low", "Close") if c in out.columns]
    return out[keep]


def _reference_events(panel: pd.DataFrame, universe: Mapping, start: dt.date,
                      rep=None) -> pd.DataFrame:
    """기준일 후보 (Code, Date) 를 벡터 연산으로 뽑는다."""
    rd = dict(universe.get("reference_day") or {})
    rule = str(rd.get("rule") or "amount_spike")

    def beat(msg: str) -> None:
        if rep is not None:
            rep.check(force=True)
            rep.emit(19, msg, PHASE_SCAN, force=True)

    beat("기준일 조건 계산 중 (종목별 그룹핑)")
    g = panel.groupby("Code", sort=False, observed=True)

    if rule == "none":
        mask = pd.Series(True, index=panel.index)
    elif rule == "amount_spike":
        spike = float(rd.get("spike_amount_krw_eok") or 0) * EOK
        mask = panel["Amount"] >= spike
        prev_max = rd.get("prev_day_amount_max_eok")
        if prev_max is not None:
            beat("기준일 조건 계산 중 (직전일 거래대금)")
            prev = g["Amount"].shift(1)
            mask &= prev.notna() & (prev <= float(prev_max) * EOK)
    elif rule == "volume_spike":
        n = int(rd.get("volume_ma_period") or 20)
        mult = float(rd.get("volume_mult") or 3.0)
        ma = g["Volume"].transform(lambda s: s.rolling(n, min_periods=n).mean())
        mask = ma.notna() & (panel["Volume"] >= mult * ma)
    elif rule == "range_breakout":
        n = int(rd.get("breakout_period") or rd.get("lookback_days") or 20)
        hh = g["High"].transform(lambda s: s.rolling(n, min_periods=n).max().shift(1))
        mask = hh.notna() & (panel["Close"] > hh)
    else:  # pragma: no cover - validate 에서 차단됨
        mask = pd.Series(False, index=panel.index)

    beat("기준일 조건 계산 중 (구간 필터)")
    mask &= panel["Date"] >= pd.Timestamp(start)
    cols = [c for c in ("Code", "Date", "Amount", "Open", "High", "Low", "Close") if c in panel.columns]
    out = panel.loc[mask, cols]
    beat(f"기준일 {len(out):,}건 확인")
    return out


# ======================================================================================
# 지표 워밍업 구간 산정
# ======================================================================================


def _walk_indicator_specs(node: Any, out: List[Mapping]) -> None:
    if isinstance(node, Mapping):
        key = node.get("indicator") or node.get("type")
        if isinstance(key, str) and key.upper() in REGISTRY:
            out.append(node)
        for v in node.values():
            _walk_indicator_specs(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk_indicator_specs(v, out)


def _warmup_bars(strategy: Mapping) -> int:
    specs: List[Mapping] = []
    _walk_indicator_specs(strategy.get("entries"), specs)
    _walk_indicator_specs(strategy.get("exits"), specs)
    _walk_indicator_specs(strategy.get("indicators"), specs)
    longest = 0
    for s in specs:
        key = str(s.get("indicator") or s.get("type")).upper()
        reg = REGISTRY.get(key)
        if not reg:
            continue
        for p in reg["params"]:
            if p["type"] != "int":
                continue
            v = s.get(p["name"], p["default"])
            try:
                longest = max(longest, int(v))
            except (TypeError, ValueError):
                pass
    rd = (strategy.get("universe") or {}).get("reference_day") or {}
    try:
        longest = max(longest, int(rd.get("lookback_days") or 0))
    except (TypeError, ValueError):
        pass
    return max(longest + 10, 30)


# ======================================================================================
# 체결
# ======================================================================================


def _fills_at_close(rule: Mapping, fill_model: str) -> bool:
    """이 규칙이 **그 봉의 종가**로 체결되는가.

    종가에 산 뒤 같은 봉에서 다시 파는 것은 물리적으로 불가능하다(장이 이미 끝났다).
    ``same_day_exit`` 과 무관하게 막아야 하는 조건이라 따로 판정한다.
    """
    if fill_model == "close":
        return True
    spec = rule.get("price")
    if spec is None:
        return True                       # price 미지정 = 조건 성립 봉의 종가
    return isinstance(spec, str) and spec.strip().lower() == "close"


def _direction(rule: Mapping, side: str) -> str:
    """목표가를 위/아래 어느 쪽에서 만나는지. 갭 체결 판정에 쓴다."""
    when = rule.get("when")
    if isinstance(when, Mapping):
        op = str(when.get("op") or "").lower()
        if op in ("<", "<="):
            return "down"
        if op in (">", ">="):
            return "up"
        if op == "cross_below":
            return "down"
        if op == "cross_above":
            return "up"
    t = str(rule.get("type") or "")
    if t in ("stop_loss", "trailing_stop"):
        return "down"
    if t == "take_profit":
        return "up"
    return "down" if side == "buy" else "up"


def _fill_price(
    rule: Mapping,
    ctx: EvalContext,
    li: int,
    bar: Mapping[str, float],
    nxt: Optional[Mapping[str, float]],
    side: str,
    fill_model: str,
) -> Optional[float]:
    """ARCHITECTURE 2장 체결가 규칙.

    * ``touch``     — 목표가 그대로. 단 **갭으로 목표가를 지나쳤으면 당일 시가**
    * ``next_open`` — 다음 봉 시가
    * ``close``     — 당일 종가
    """
    if fill_model == "close":
        return float(bar["close"])
    if fill_model == "next_open":
        return float(nxt["open"]) if nxt else None

    spec = rule.get("price")
    if spec is None:
        return float(bar["close"])
    if isinstance(spec, str) and spec.strip().lower() in ("open", "high", "low", "close"):
        # 그 봉의 값 자체를 체결가로 쓰는 경우 — 목표가가 아니므로 갭 보정을 하지 않는다
        return float(bar[spec.strip().lower()])
    try:
        target = evaluate_operand(spec, ctx, li)
    except DSLError:
        return float(bar["close"])
    if target is None or not math.isfinite(float(target)):
        return float(bar["close"])
    target = float(target)

    direction = _direction(rule, side)
    o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
    if direction == "down":
        if o <= target:          # 갭하락으로 목표가를 지나쳐 시작 → 시가 체결
            return o
    else:
        if o >= target:          # 갭상승으로 목표가를 지나쳐 시작 → 시가 체결
            return o
    return min(max(target, l), h)


# ======================================================================================
# 메인
# ======================================================================================


def run_backtest(
    strategy: Mapping,
    store,
    dart=None,
    progress: Optional[Callable[..., None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """백테스트를 실행한다.

    Parameters
    ----------
    dart : DartStore | None
        DART 재무 저장소. ``None`` 이거나 ``available == False`` 면 재무 필터는
        적용되지 않고 ``assumptions.ignored_filters`` 에 사유가 남는다(기존 동작 그대로).
        연결되면 **반드시 그 시점에 이미 공시된 재무만** 본다 (``as_of``).
    progress : callable | None
        ``progress(done, total, message)`` (v1) 또는
        ``progress(done, total, message, phase, eta_sec)`` (v2). ``total`` 은 항상 100.
    should_cancel : callable | None
        ``True`` 를 돌려주면 ``BacktestCancelled`` 를 던진다. 최소 1초에 한 번 확인한다.
    """
    t0 = time.perf_counter()
    run_id = "r_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rep = _Reporter(progress, should_cancel)

    def report(done: int, message: str, phase: str | None = None) -> None:
        rep.emit(done, message, phase, force=True)

    rep.check(force=True)
    ok, errors = validate_strategy(strategy)
    if not ok:
        raise StrategyError("전략 스키마 검증에 실패했습니다", errors)

    warnings: List[str] = []
    sig = _Signals()

    # ---------------------------------------------------------------- 설정 파싱
    market = dict(strategy.get("market") or {})
    universe = dict(strategy.get("universe") or {})
    portfolio = dict(strategy.get("portfolio") or {})
    execution = dict(strategy.get("execution") or {})
    period = dict(strategy.get("period") or {})

    entries = list(strategy.get("entries") or [])
    exits = list(strategy.get("exits") or [])
    exit_priority = list(strategy.get("exit_priority") or ["SL", "TP"])
    ordered_exits = _order_exits(exits, exit_priority)

    fill_model = str(execution.get("fill_model") or "touch")
    slippage = float(execution.get("slippage_pct") or 0.0) / 100.0
    fee = float(execution.get("fee_pct") or 0.0) / 100.0

    if "initial_capital_manwon" in portfolio:
        initial_capital = float(portfolio["initial_capital_manwon"]) * 10_000.0
    else:
        initial_capital = float(portfolio.get("initial_capital") or 100_000_000.0)
    max_positions = int(portfolio.get("max_positions") or 1)
    sizing = str(portfolio.get("position_sizing") or "equal_weight")
    allow_dup = bool(portfolio.get("allow_duplicate_symbol", False))
    valid_days = int(universe.get("valid_days_after_reference") or 60)
    ucondition = str(universe.get("condition") or "none")
    pullback_pct = universe.get("pullback_pct")

    same_day_exit = str(execution.get("same_day_exit") or DEFAULT_SAME_DAY_EXIT)
    if same_day_exit not in SAME_DAY_EXIT_MODES:
        same_day_exit = DEFAULT_SAME_DAY_EXIT

    requested_resolution = str(
        market.get("trade_resolution") or execution.get("resolution") or "1d"
    )
    if requested_resolution != "1d":
        warnings.append(
            f"{requested_resolution} 체결을 요청했지만 일봉 데이터만 있어 일봉으로 근사했습니다. "
            "실행 가정은 assumptions 를 확인하세요."
        )
    if same_day_exit == "always":
        warnings.append(
            "same_day_exit=always 는 진입 당일 익절을 허용합니다. "
            "일봉만으로는 저가·고가 순서를 알 수 없어 성과가 실제보다 좋게 나올 수 있습니다."
        )

    stats = {
        "same_day_entry_exit": 0,
        "same_day_entry_exit_pct": 0.0,
        "ambiguous_bars": 0,
        "same_day_profit_exits_blocked": 0,
        "missing_financials": 0,
        "missing_financials_pct": 0.0,
    }

    # 지표 별칭 — strategy["indicators"][].key 를 조건식/수식에서 이름으로 쓴다
    aliases = _indicator_aliases(strategy)

    # DART 재무 데이터 연결 여부
    dart_ok = False
    if dart is not None:
        try:
            dart_ok = bool(dart.available)
        except Exception:  # pragma: no cover - 저장소가 이상해도 백테스트는 돌아야 한다
            dart_ok = False

    on_missing = str((universe.get("filters") or {}).get("on_missing") or DEFAULT_ON_MISSING)
    if on_missing not in ON_MISSING_MODES:
        on_missing = DEFAULT_ON_MISSING

    # universe.filters — 지원하는 키만 적용하고 나머지는 사유와 함께 남긴다
    applied_filters, financial_filters, ignored_filters = _split_filters(strategy, dart_ok)
    for ig in ignored_filters:
        warnings.append(ig["message"])

    dart_last_fetch = None
    if dart_ok and financial_filters:
        try:
            dart_last_fetch = dart.status().get("last_fetch")
        except Exception:  # pragma: no cover
            dart_last_fetch = None

    def make_assumptions() -> dict:
        return {
            "resolution": "1d",
            "requested_resolution": requested_resolution,
            "fill_model": fill_model,
            "same_day_exit": same_day_exit,
            "slippage_pct": _r(slippage * 100.0, 4),
            "fee_pct": _r(fee * 100.0, 4),
            "exit_priority": [r.get("id") for r in ordered_exits],
            "notes": _assumption_notes(
                requested_resolution, fill_model, same_day_exit, slippage, fee,
                ordered_exits, stats, ignored_filters,
            ),
            "stats": dict(stats),
            "ignored_filters": [
                {"key": ig["key"], "reason": ig["reason"]} for ig in ignored_filters
            ],
            "dart": {
                "available": bool(dart_ok),
                "as_of": bool(dart_ok and financial_filters),
                "coverage_pct": (
                    _r(100.0 - float(stats["missing_financials_pct"]))
                    if (dart_ok and financial_filters) else None
                ),
                "on_missing": on_missing,
                "last_fetch": dart_last_fetch,
            },
        }

    # ---------------------------------------------------------------- 기간
    report(2, "기간 확정")
    start = _d(period.get("start"))
    end_raw = period.get("end", "auto")
    if end_raw in (None, "", "auto"):
        end = store.latest_date()
    else:
        end = _d(end_raw)
    if end < start:
        raise DataUnavailable(f"백테스트 구간이 뒤집혔습니다: {start} ~ {end}")

    warm = _warmup_bars(strategy)
    hist_start = start - dt.timedelta(days=int(warm * 1.75) + 30)

    # ---------------------------------------------------------------- 패널 로드
    rep.set_phase(PHASE_LOAD, 3)
    report(3, f"{hist_start.year}~{end.year}년 데이터 읽는 중", PHASE_LOAD)

    def _on_year(idx: float, total: int, year: int) -> None:
        # 연도 파일 하나가 몇 초씩 걸리므로 **파일 내부에서도** 호출된다.
        rep.check(force=True)
        rep.emit(3 + 14.0 * float(idx) / max(total, 1),
                 f"{year}년 데이터 읽는 중 ({min(int(idx) + 1, total)}/{total}년)",
                 PHASE_LOAD)

    want_cols = _needed_columns(strategy)
    try:
        panel = store.panel(hist_start, end, columns=want_cols, on_year=_on_year)
    except TypeError:                       # 구버전 store (columns/on_year 미지원)
        panel = store.panel(hist_start, end)
    rep.check(force=True)
    report(17, "종목 목록 정리 중", PHASE_LOAD)
    # 연도 파일은 (Date, Code) 순으로 저장돼 있고 연도 순으로 이어붙였으므로 이미 Date 오름차순이다.
    # 700만 행 정렬은 몇 초가 걸리는데 중간에 취소 확인을 넣을 수 없어, 필요할 때만 한다.
    # 종목 안에서 날짜 오름차순이기만 하면 groupby.shift / rolling 이 모두 정확하다.
    if not panel["Date"].is_monotonic_increasing:
        panel = panel.sort_values(["Date", "Code"], kind="stable")
    panel = panel.reset_index(drop=True)
    rep.check(force=True)
    rep.emit(18, "종목 선정 조건 확인 중", PHASE_LOAD, force=True)

    calendar = [d.date() for d in pd.DatetimeIndex(sorted(panel["Date"].unique()))]
    calendar = [d for d in calendar if start <= d <= end]
    if not calendar:
        raise DataUnavailable(f"{start} ~ {end} 구간에 거래일이 없습니다")

    def _load_beat(msg: str) -> None:
        rep.check(force=True)
        rep.emit(18, msg, PHASE_LOAD, force=True)

    codes, ustats = _eligible_codes(panel, universe, applied_filters, beat=_load_beat)
    if len(codes) == 0:
        return _empty_result(
            run_id, t0, warnings, sig, calendar, initial_capital, start, end,
            "종목 선정 조건(시장·제외·filters)을 만족하는 종목이 없습니다.", make_assumptions(),
        )
    # 700만 행 패널을 복사하지 않는다 (수 초가 걸리고 중단할 수 없다).
    # 제외 종목은 기준일 이벤트 단계에서 걸러낸다.
    eligible = set(codes) if len(codes) < ustats["total"] else None
    # 종목 선정에만 쓰인 컬럼은 여기서 버린다 (메모리 회수)
    for _c in ("Market", "MarketId", "Dept"):
        if _c in panel.columns:
            del panel[_c]
    rep.check(force=True)

    sig.add(
        calendar[0],
        "SCAN",
        None,
        f"전체 {ustats['eligible']:,}종목 스캔 · {universe.get('reference_day', {}).get('rule', 'amount_spike')} 기준일 탐색",
    )

    # ---------------------------------------------------------------- 1단계 스캔
    rep.set_phase(PHASE_SCAN, 18)
    report(19, "기준일 찾는 중", PHASE_SCAN)
    if str((universe.get("reference_day") or {}).get("rule") or "") == "custom":
        events = _custom_events(panel, universe, start, aliases, rep)
    else:
        events = _reference_events(panel, universe, start, rep)
    rep.check(force=True)
    rep.emit(20, "기준일 정리 중", PHASE_SCAN, force=True)
    if events.empty:
        return _empty_result(
            run_id, t0, warnings, sig, calendar, initial_capital, start, end,
            "기준일 조건을 만족하는 종목이 없습니다.", make_assumptions(),
        )

    if eligible is not None and not events.empty:
        events = events[events["Code"].isin(eligible)]
        rep.check(force=True)
        if events.empty:
            return _empty_result(
                run_id, t0, warnings, sig, calendar, initial_capital, start, end,
                "기준일 조건을 만족하는 종목이 없습니다.", make_assumptions(),
            )
    cand_codes = pd.Index(events["Code"].unique())
    report(20, f"후보 {len(cand_codes):,}종목")

    # ---------------------------------------------------------------- 후보 종목 봉 준비
    # 날짜 → 행 번호를 종목마다 dict 로 들면 수백만 개 엔트리가 되어 메모리를 다 잡아먹는다.
    # 전역 거래일 달력의 인덱스(gi)를 쓰는 int32 배열 두 개로 대체한다.
    cal_ns = np.array([np.datetime64(d) for d in calendar], dtype="datetime64[ns]")
    n_cal = len(cal_ns)

    recs: Dict[str, dict] = {}
    _n_cand = len(cand_codes)
    for _j, (code, gdf) in enumerate(_iter_candidate_bars(panel, cand_codes, rep, _n_cand)):
        gdf = gdf.sort_values("Date", kind="stable")
        df = gdf.set_index("Date")[[c for c in _PRICE_COLS if c in gdf.columns]].copy()
        recs[code] = {
            "df": df,
            "name": str(gdf["Name"].iloc[-1]) if "Name" in gdf.columns else code,
            **_row_maps(df.index, cal_ns, n_cal),
            # dtype 을 바꾸지 않으면 to_numpy() 가 뷰라서 복사본을 만들지 않는다.
            # OHLC 는 float32 지만 KRX 주가 범위에서는 float64 와 값이 완전히 같다.
            "open": df["Open"].to_numpy(),
            "high": df["High"].to_numpy(),
            "low": df["Low"].to_numpy(),
            "close": df["Close"].to_numpy(),
            "volume": df["Volume"].to_numpy() if "Volume" in df else np.zeros(len(df)),
            "amount": df["Amount"].to_numpy() if "Amount" in df else np.zeros(len(df)),
            "ind": {},
            "ctx": None,
        }

    # 패널은 여기까지만 필요하다. 700만 행을 붙들고 있으면 시뮬레이션 내내 메모리를 잡아먹는다.
    panel = None
    del panel

    events_by_day: Dict[dt.date, List[Tuple[str, dict]]] = {}
    for _k, row in enumerate(events.itertuples(index=False)):
        if (_k & 1023) == 0:
            rep.tick(25, f"기준일 정리 중 ({_k:,}/{len(events):,})", PHASE_SCAN)
        d = _d(row.Date)
        if d < start or d > end:
            continue
        events_by_day.setdefault(d, []).append(
            (
                row.Code,
                {
                    "open": float(row.Open),
                    "high": float(row.High),
                    "low": float(row.Low),
                    "close": float(row.Close),
                    "amount": float(getattr(row, "Amount", 0.0) or 0.0),
                },
            )
        )

    # ---------------------------------------------------------------- 2단계 시뮬레이션
    report(25, "체결 시뮬레이션")
    cash = initial_capital
    active: Dict[str, dict] = {}
    trades: List[dict] = []
    eq_values: List[float] = []
    dsl_error_logged = False
    n_days = len(calendar)

    rep.set_phase(PHASE_SIM, 25)
    for gi, day in enumerate(calendar):
        if gi == 0 or gi == n_days - 1:
            rep.check(force=True)
            rep.emit(
                25 + 65.0 * (gi + 1) / n_days,
                f"종목별 매매 계산 중 ({gi + 1:,}/{n_days:,}일 · {day.isoformat()})",
                PHASE_SIM, force=True,
            )
        rep.tick(
            25 + 65.0 * (gi + 1) / n_days,
            f"종목별 매매 계산 중 ({gi + 1:,}/{n_days:,}일 · {day.isoformat()} · "
            f"보유 {sum(1 for x in active.values() if x['qty'] > 0)}종목 · 누적 {len(trades):,}거래)",
            PHASE_SIM,
        )

        # --- (a) 기준일 이벤트 등록 / 갱신 (조건 만족일이 여럿이면 가장 최근을 채택)
        for code, refbar in events_by_day.get(day, ()):
            rec = recs.get(code)
            if rec is None:
                continue
            li = int(rec["row_of_gi"][gi])
            if li < 0:
                continue
            w = active.get(code)
            if w is None:
                active[code] = _new_watch(code, day, li, gi, refbar)
                sig.add(
                    day, "MATCH", code,
                    f"{code} {rec['name']} — 기준일 {day.isoformat()} 거래대금 "
                    f"{refbar['amount'] / EOK:,.0f}억 → 후보 등록",
                )
                sig.add(
                    day, "WATCH", code,
                    f"{code} 목표 진입가 = 기준일 시가 {_fmt(refbar['open'])} · 감시 시작",
                )
            elif not w["fills"]:
                w.update(ref_date=day, ref_li=li, ref_gi=gi, ref=refbar)
                sig.add(
                    day, "MATCH", code,
                    f"{code} 기준일 갱신 → {day.isoformat()} "
                    f"(거래대금 {refbar['amount'] / EOK:,.0f}억, 더 최근 날짜 채택)",
                )

        # --- (b) 활성 후보 평가: 진입 → 청산 (같은 날 진입+청산 허용, 진입 먼저)
        for code in sorted(active):
            w = active.get(code)
            if w is None:
                continue
            rec = recs[code]
            li = int(rec["row_of_gi"][gi])
            if li < 0:
                continue  # 거래정지 등 — 해당일 봉 없음

            bar = _bar(rec, li)
            nxt = _bar(rec, li + 1) if li + 1 < len(rec["close"]) else None
            ctx = _ctx(rec, w, li=li, aliases=aliases)

            # 유효기간 만료
            if not w["fills"] and (gi - w["ref_gi"]) > valid_days:
                sig.add(day, "WATCH", code,
                        f"{code} 기준일 {w['ref_date'].isoformat()} 이후 {valid_days}영업일 내 미체결 → 후보 폐기")
                active.pop(code, None)
                continue

            # ---- 진입 (기준일 **다음** 거래일부터 감시한다)
            for rule in entries if gi > w["ref_gi"] else ():
                rid = rule.get("id")
                if rid in w["fills"]:
                    continue
                if any(r not in w["fills"] for r in (rule.get("requires") or [])):
                    continue
                first = not w["fills"]
                if first:
                    if len([1 for x in active.values() if x["qty"] > 0]) >= max_positions:
                        continue
                    if not allow_dup and w["qty"] <= 0 and _holding(active, code):
                        continue
                    if not _universe_gate(ucondition, pullback_pct, bar, w["ref"], fill_model):
                        continue

                ctx.params = dict(rule)
                try:
                    hit = evaluate_condition(rule.get("when"), ctx, li)
                except DSLError as e:
                    if not dsl_error_logged:
                        warnings.append(f"조건식 평가 실패(무시): {e}")
                        dsl_error_logged = True
                    continue
                if not hit:
                    continue

                px = _fill_price(rule, ctx, li, bar, nxt, "buy", fill_model)
                if px is None or px <= 0:
                    continue
                fill_date = day if fill_model != "next_open" else _next_day(rec, li)
                buy_px = px * (1.0 + slippage)

                equity_now = cash + _mtm(active, recs, gi)
                if w["planned"] is None:
                    w["planned"] = _planned_position(sizing, portfolio, equity_now, max_positions)
                qty = _entry_qty(rule, sizing, portfolio, w["planned"], equity_now, buy_px, cash)
                if qty <= 0:
                    sig.add(day, "WATCH", code,
                            f"{code} {rule.get('label') or rid} 조건 성립했으나 현금 부족으로 미체결", "09:05:00")
                    continue

                cost = qty * buy_px
                cash -= cost
                w["qty"] += qty
                w["cost"] += cost
                w["fills"][rid] = {"price": float(px), "qty": int(qty), "date": fill_date}
                w["fill_log"].append(
                    {"rule": rid, "date": fill_date.isoformat(), "price": _r(buy_px, 2), "qty": int(qty)}
                )
                w["last_entry_date"] = fill_date
                w["last_entry_at_close"] = _fills_at_close(rule, fill_model)
                if w["entry_date"] is None:
                    w["entry_date"] = fill_date
                    w["entry_li"] = li
                    w["peak"] = bar["high"]
                    w["group_id"] = f"{code}-{w['ref_date'].isoformat()}-{fill_date.isoformat()}"
                    w["entry_bar"] = dict(bar, price=float(px), qty=int(qty),
                                          date=fill_date)

                sig.add(day, "FILL", code,
                        f"BUY {code} @ {_fmt(buy_px)} × {qty}주 (비중 {rule.get('size_pct', 100)}%) "
                        f"— {rule.get('label') or rid}", "15:30:00")
                sig.add(day, "POS", code,
                        f"{code} 평단 {_fmt(w['cost'] / w['qty'])} · 보유 {w['qty']}주", "15:30:00")

            # ---- 청산
            if w["qty"] > 0:
                w["peak"] = max(w["peak"] or bar["high"], bar["high"])
                ctx = _ctx(rec, w, bar=bar, day=day, li=li, aliases=aliases)
                entry_today = w["last_entry_date"] == day
                # 종가에 진입한 봉에서는 더 이상 팔 수 없다 (장 종료). 순서 가정 이전의 문제다.
                if entry_today and w["last_entry_at_close"]:
                    sig.add(day, "POS", code,
                            f"{code} 종가 진입 봉이라 당일 청산은 평가하지 않음 (다음 거래일부터)",
                            "15:31:00")
                    continue
                ambiguous_here = False
                blocked_here = False
                for rule in ordered_exits:
                    if w["qty"] <= 0:
                        break
                    if rule.get("id") in w["exits_done"]:
                        continue      # 부분 청산 규칙은 포지션당 한 번만 발동한다
                    ctx.params = dict(rule)
                    ctx.position = _position(w, bar, day, li)
                    hit, forced_price = _exit_hit(rule, ctx, li, bar, w, day)
                    if not hit:
                        continue
                    px = forced_price
                    if px is None:
                        px = _fill_price(rule, ctx, li, bar, nxt, "sell", fill_model)
                    if px is None or px <= 0:
                        continue
                    exit_date = day if fill_model != "next_open" else _next_day(rec, li)
                    qty_out = _exit_qty(rule, w["qty"])
                    if qty_out <= 0:
                        continue

                    if entry_today:
                        # 진입과 청산이 같은 봉 안에서 모두 성립 — 일봉으로는 순서를 알 수 없다
                        ambiguous_here = True
                        allowed, net = _same_day_allowed(
                            same_day_exit, w, px, qty_out, slippage, fee
                        )
                        if not allowed:
                            if net > 0 and not blocked_here:
                                stats["same_day_profit_exits_blocked"] += 1
                                blocked_here = True
                            sig.add(
                                day, "POS", code,
                                f"{code} {rule.get('label') or rule.get('id')} 조건이 진입 당일 성립했으나 "
                                f"순손익 {net:+,.0f}원 · same_day_exit={same_day_exit} 규칙에 따라 "
                                "다음 거래일로 이월",
                                "15:31:00",
                            )
                            continue

                    trade, proceeds = _close(
                        w, rec, code, rule, px, qty_out, exit_date, slippage, fee,
                        len(trades) + 1, rule.get("label") or rule.get("id"),
                    )
                    cash += proceeds
                    trades.append(trade)
                    w["exits_done"].add(rule.get("id"))
                    sig.add(day, "FILL", code,
                            f"SELL {code} @ {_fmt(trade['exit_price'])} × {qty_out}주 "
                            f"({'전량' if w['qty'] <= 0 else '일부'}) — {trade['exit_reason']}",
                            "15:30:00")
                    sig.add(day, "PNL", code,
                            f"{code} 실현손익 {trade['pnl']:+,.0f}원 ({trade['return_pct']:+.2f}%) "
                            f"· 보유 {trade['hold_days']}일", "15:30:00")
                    if w["qty"] > 0:
                        sig.add(day, "POS", code,
                                f"{code} 부분 청산 후 잔량 {w['qty']}주 · 평단 "
                                f"{_fmt(w['cost'] / w['qty'])} 유지", "15:30:00")
                if ambiguous_here:
                    stats["ambiguous_bars"] += 1
                if w["qty"] <= 0:
                    active.pop(code, None)

        eq_values.append(cash + _mtm(active, recs, gi))

    # ---------------------------------------------------------------- 미청산 강제 청산
    report(92, "미청산 포지션 정리", PHASE_SIM)
    last_day = calendar[-1]
    for _k, code in enumerate(sorted(active)):
        if (_k & 15) == 0:
            rep.tick(92, f"미청산 포지션 정리 중 ({_k:,}/{len(active):,})", PHASE_SIM)
        w = active[code]
        if w["qty"] <= 0:
            continue
        rec = recs[code]
        li = _last_index_upto(rec, len(calendar) - 1)
        if li is None:
            continue
        px = float(rec["close"][li])
        qty_out = w["qty"]
        trade, proceeds = _close(
            w, rec, code, {"id": "EOD", "label": "기간종료"}, px, qty_out, last_day,
            slippage, fee, len(trades) + 1, "기간종료",
        )
        cash += proceeds
        trades.append(trade)
        sig.add(last_day, "FILL", code,
                f"SELL {code} @ {_fmt(trade['exit_price'])} × {qty_out}주 — 기간종료 강제청산",
                "15:30:00")
        sig.add(last_day, "PNL", code,
                f"{code} 실현손익 {trade['pnl']:+,.0f}원 ({trade['return_pct']:+.2f}%) "
                f"· 보유 {trade['hold_days']}일", "15:30:00")
        active.pop(code, None)
    eq_values[-1] = cash + _mtm(active, recs, len(calendar) - 1)

    # ---------------------------------------------------------------- 성과
    rep.set_phase(PHASE_METRICS, 94)
    rep.check(force=True)
    report(96, "성과 계산 중", PHASE_METRICS)
    metrics = compute_metrics(calendar, eq_values, trades, initial_capital, start, end)
    rep.check(force=True)
    rep.emit(97, "자산 곡선 만드는 중", PHASE_METRICS, force=True)
    equity = build_equity(calendar, eq_values, initial_capital)
    monthly = monthly_returns(calendar, eq_values)
    rep.check(force=True)
    rep.emit(98, "종목별 집계 중", PHASE_METRICS, force=True)

    sig.add(
        last_day, "DONE", None,
        f"누적 {metrics['trades']}거래 · 승률 {metrics['win_rate_pct']}% · "
        f"총수익률 {metrics['total_return_pct']:+}%",
        "15:30:00",
    )
    if sig.truncated:
        warnings.append(
            f"시그널 로그가 {sig.limit:,}건을 넘어 이후 {sig.dropped:,}건은 생략했습니다."
        )

    stats["same_day_entry_exit"] = sum(1 for t in trades if t["hold_days"] == 0)
    stats["same_day_entry_exit_pct"] = (
        _r(stats["same_day_entry_exit"] / len(trades) * 100.0) if trades else 0.0
    )
    if stats["same_day_entry_exit"]:
        warnings.append(
            f"진입일과 청산일이 같은 거래가 {stats['same_day_entry_exit']:,}건"
            f"(전체 {len(trades):,}건 중 {stats['same_day_entry_exit_pct']}%)입니다. "
            "일봉만으로는 하루 안의 체결 순서를 확정할 수 없습니다."
        )

    report(100, "완료", PHASE_METRICS)
    return {
        "run_id": run_id,
        "elapsed_sec": _r(time.perf_counter() - t0, 3),
        "warnings": warnings,
        "assumptions": make_assumptions(),
        "metrics": metrics,
        "equity": equity,
        "monthly": monthly,
        "trades": trades,
        "by_stock": by_stock_summary(trades),
        "signals": sig.items,
    }


# ======================================================================================
# 내부 구현
# ======================================================================================


def _net_exit_pnl(w: Mapping, price: float, qty: int, slippage: float, fee: float) -> float:
    """이 청산이 실현할 **순손익** (슬리피지·수수료 반영). ``_close`` 와 같은 계산식."""
    if not qty or not w["qty"]:
        return 0.0
    proceeds = price * (1.0 - slippage) * qty * (1.0 - fee)
    cost_basis = (w["cost"] / w["qty"]) * qty
    return proceeds - cost_basis


def _same_day_allowed(mode: str, w: Mapping, price: float, qty: int,
                      slippage: float, fee: float) -> Tuple[bool, float]:
    """진입 체결 당일에 이 청산을 실행해도 되는가. ``(허용여부, 순손익)`` 을 돌려준다.

    * ``always``    — 전부 허용 (낙관적)
    * ``never``     — 전부 차단 (가장 보수적)
    * ``loss_only`` — **체결가 기준 순손익이 이익이면 차단**, 손실·본전이면 허용.

    ``loss_only`` 는 규칙의 ``type`` 을 보지 않는다. ``type: "stop_loss"`` 라도
    손절선이 평단가 위에 있으면 실질은 익절이므로 같이 차단된다
    (전략1의 "45일선 도달 손절"이 정확히 그 사례다).
    """
    net = _net_exit_pnl(w, price, qty, slippage, fee)
    if mode == "always":
        return True, net
    if mode == "never":
        return False, net
    return (net <= 0.0), net


_FILL_MODEL_NOTES = {
    "touch": "체결가는 목표가 그대로 잡되, 갭으로 목표가를 지나쳐 시작한 날은 당일 시가로 체결했습니다.",
    "next_open": "조건이 성립한 다음 거래일 시가로 체결했습니다.",
    "close": "조건이 성립한 당일 종가로 체결했습니다.",
}

_SAME_DAY_NOTES = {
    "loss_only": (
        "진입 체결이 있었던 날에는 이익이 나는 청산을 모두 다음 거래일로 미루고, "
        "손실·본전 청산만 당일에 허용했습니다 (same_day_exit=loss_only). "
        "판정은 규칙 이름이 아니라 체결가 기준이며, 슬리피지·수수료까지 뺀 순손익으로 봅니다. "
        "같은 봉에서 매수가와 청산가가 모두 닿았더라도 이익 쪽이 먼저였다고 단정할 수 없기 때문입니다. "
        "손절 규칙이라도 손절선이 평단가 위에 있으면 실질은 익절이므로 함께 미뤘습니다."
    ),
    "never": (
        "진입 체결이 있었던 날에는 어떤 청산도 평가하지 않고 다음 거래일부터 판정했습니다 "
        "(same_day_exit=never). 세 가지 설정 중 가장 보수적입니다."
    ),
    "always": (
        "진입 체결이 있었던 날에도 익절·손절을 모두 평가했습니다 (same_day_exit=always). "
        "같은 봉에서 매수가와 익절가가 모두 닿으면 매수가 먼저였다고 가정하므로 "
        "결과가 실제보다 좋게 나올 수 있습니다."
    ),
}


def _assumption_notes(requested_resolution: str, fill_model: str, same_day_exit: str,
                      slippage: float, fee: float, ordered_exits: Sequence[Mapping],
                      stats: Mapping, ignored_filters: Sequence[Mapping] = ()) -> List[str]:
    """사용자에게 그대로 보여줄 실행 가정 문장들."""
    notes = [
        "일봉 데이터만 사용했습니다. 하루 안에서 저가와 고가 중 무엇이 먼저였는지는 알 수 없습니다.",
    ]
    if requested_resolution != "1d":
        notes.append(
            f"전략은 {requested_resolution} 해상도 체결을 요청했지만 marcap 은 일봉만 제공합니다. "
            "분봉 매매는 추후 지원 예정이며, 그 전까지는 일봉 근사로 동작합니다."
        )
    notes.append(_FILL_MODEL_NOTES.get(fill_model, f"fill_model={fill_model} 로 체결했습니다."))
    notes.append(_SAME_DAY_NOTES.get(same_day_exit, ""))
    notes.append(
        f"매수는 체결가 +{slippage * 100:g}%, 매도는 -{slippage * 100:g}% 슬리피지를 적용하고 "
        f"매도 대금에 {fee * 100:g}% 비용(수수료+거래세)을 부과했습니다."
    )
    ids = [r.get("id") for r in ordered_exits]
    if len(ids) > 1:
        notes.append(
            f"같은 봉에서 여러 청산이 동시에 성립하면 {' → '.join(str(i) for i in ids)} 순서로 평가했습니다."
        )
    amb = int(stats.get("ambiguous_bars") or 0)
    if amb:
        notes.append(
            f"진입과 청산 조건이 같은 봉 안에서 모두 성립한 경우가 {amb:,}건 있었습니다. "
            "이 봉들은 순서를 확정할 수 없어 위 가정에 의존합니다. 건수가 많을수록 결과 신뢰도는 낮습니다."
        )
    blocked = int(stats.get("same_day_profit_exits_blocked") or 0)
    if blocked:
        notes.append(
            f"그중 {blocked:,}건은 진입 당일에 이익으로 청산될 수 있었지만 "
            "이 규칙에 막혀 다음 거래일로 넘어갔습니다. "
            "same_day_exit=always 로 두면 이 {0:,}건이 그대로 수익에 잡힙니다.".format(blocked)
        )
    for ig in ignored_filters:
        notes.append(ig["message"])
    notes.append("미청산 포지션은 백테스트 종료일 종가로 강제 청산했습니다 (exit_reason=기간종료).")
    return [n for n in notes if n]


#: 후보 종목을 한 번에 몇 개씩 잘라 처리할지. groupby 한 번이 1초를 넘지 않도록 잡는다.
CANDIDATE_BATCH = 80


def _iter_candidate_bars(panel: pd.DataFrame, cand_codes, rep, total: int,
                         base: float = 20.0, span: float = 5.0,
                         label: str = "후보 종목 데이터 준비 중"):
    """후보 종목의 일봉을 ``(code, DataFrame)`` 으로 흘려보낸다.

    700만 행짜리 패널에 groupby 를 한 번에 걸면 첫 그룹이 나오기까지 10초 가까이 걸리고
    그 사이 취소를 못 받는다. 후보를 배치로 잘라 각 단계가 1초를 넘지 않게 한다.
    """
    cols = [c for c in (["Date", "Code", "Name"] + _PRICE_COLS) if c in panel.columns]
    rep.tick(base, label, PHASE_SCAN)
    narrow = panel if list(panel.columns) == cols else panel[cols]
    rep.tick(base, f"{label} (0/{total:,})", PHASE_SCAN)

    codes = list(cand_codes)
    done = 0
    for i in range(0, len(codes), CANDIDATE_BATCH):
        batch = set(codes[i:i + CANDIDATE_BATCH])
        rep.tick(base + span * done / max(total, 1),
                 f"{label} ({done:,}/{total:,})", PHASE_SCAN)
        mask = narrow["Code"].isin(batch)
        rep.check(force=True)
        part = narrow[mask]
        rep.check(force=True)
        for code, gdf in part.groupby("Code", sort=False, observed=True):
            done += 1
            if (done & 15) == 0:
                rep.tick(base + span * done / max(total, 1),
                         f"{label} ({done:,}/{total:,})", PHASE_SCAN)
            yield code, gdf


def _order_exits(exits: Sequence[Mapping], priority: Sequence[str]) -> List[Mapping]:
    by_id = {r.get("id"): r for r in exits}
    ordered = [by_id[i] for i in priority if i in by_id]
    ordered += [r for r in exits if r.get("id") not in set(priority)]
    return ordered


def _new_watch(code: str, ref_date: dt.date, ref_li: int, ref_gi: int, refbar: dict) -> dict:
    return {
        "code": code,
        "ref_date": ref_date,
        "ref_li": ref_li,
        "ref_gi": ref_gi,
        "ref": refbar,
        "fills": {},
        "fill_log": [],
        "qty": 0,
        "cost": 0.0,
        "planned": None,
        "entry_date": None,
        "entry_li": None,
        "entry_bar": {},
        "last_entry_date": None,
        "last_entry_at_close": False,
        "peak": None,
        "group_id": None,
        "exits_done": set(),
    }


def _row_maps(index: pd.DatetimeIndex, cal_ns: np.ndarray, n_cal: int) -> dict:
    """거래일 달력 인덱스(gi) ↔ 종목 내 행 번호(li) 매핑을 int32 배열로 만든다.

    * ``row_of_gi[gi]``   그 날의 행 번호 (없으면 -1 — 거래정지 등)
    * ``row_upto_gi[gi]`` 그 날 **이하**의 마지막 행 번호 (없으면 -1)
    """
    row_of = np.full(n_cal, -1, dtype=np.int32)
    if n_cal and len(index):
        vals = index.values.astype("datetime64[ns]")
        pos = np.searchsorted(cal_ns, vals)
        ok = pos < n_cal
        ok[ok] &= cal_ns[pos[ok]] == vals[ok]
        row_of[pos[ok]] = np.flatnonzero(ok).astype(np.int32)
    filled = np.where(row_of >= 0, row_of, -1).astype(np.int32)
    row_upto = np.maximum.accumulate(filled)
    return {"row_of_gi": row_of, "row_upto_gi": row_upto}


def _bar(rec: dict, li: int) -> Dict[str, float]:
    return {
        "open": float(rec["open"][li]),
        "high": float(rec["high"][li]),
        "low": float(rec["low"][li]),
        "close": float(rec["close"][li]),
        "volume": float(rec["volume"][li]),
        "amount": float(rec["amount"][li]),
    }


def _next_day(rec: dict, li: int) -> dt.date:
    idx = rec["df"].index
    j = min(li + 1, len(idx) - 1)
    return idx[j].date()


def _last_index_upto(rec: dict, gi: int) -> Optional[int]:
    """달력 인덱스 ``gi`` 시점의 (없으면 그 이전 마지막) 행 번호."""
    if gi < 0 or gi >= rec["row_upto_gi"].shape[0]:
        return None
    li = int(rec["row_upto_gi"][gi])
    return li if li >= 0 else None


def _ctx(rec: dict, w: dict, bar: Optional[dict] = None, day: Optional[dt.date] = None,
         li: Optional[int] = None, aliases: Optional[Mapping] = None) -> EvalContext:
    """종목별로 하나의 EvalContext 를 재사용한다 (봉/지표 캐시 유지)."""
    ctx = rec["ctx"]
    if ctx is None:
        ctx = EvalContext(rec["df"], indicator_cache=rec["ind"], aliases=aliases)
        rec["ctx"] = ctx
    ctx.ref = w["ref"]
    ctx.fills = w["fills"]
    ctx.entry = w["entry_bar"]
    ctx.position = _position(w, bar, day, li)
    return ctx


def _position(w: dict, bar: Optional[dict], day: Optional[dt.date],
              li: Optional[int] = None) -> dict:
    if w["qty"] <= 0:
        return {}
    avg = w["cost"] / w["qty"]
    # position.hold_days 는 **영업일(봉) 수** 다 (ARCHITECTURE-v2 3-2).
    # 결과 페이로드의 trades[].hold_days(달력일)와는 다른 값이다.
    if li is not None and w["entry_li"] is not None:
        hold = max(int(li) - int(w["entry_li"]), 0)
    elif day and w["entry_date"]:
        hold = (day - w["entry_date"]).days
    else:
        hold = 0
    pos = {
        "avg_price": avg,
        "qty": w["qty"],
        "cost": w["cost"],
        "entry_price": next(iter(w["fills"].values()))["price"] if w["fills"] else avg,
        "peak_price": w["peak"] if w["peak"] is not None else avg,
        "hold_days": hold,
    }
    if bar is not None:
        pos["pnl_pct"] = (bar["close"] / avg - 1.0) * 100.0
        pos["pnl"] = (bar["close"] - avg) * w["qty"]
    else:
        pos["pnl_pct"] = 0.0
        pos["pnl"] = 0.0
    return pos


def _holding(active: Mapping[str, dict], code: str) -> bool:
    w = active.get(code)
    return bool(w and w["qty"] > 0)


def _universe_gate(condition: str, pullback_pct, bar: dict, ref: dict, fill_model: str) -> bool:
    """``universe.condition`` — 기준일 이후 감시 조건."""
    if condition in ("none", "", None):
        return True
    probe = bar["low"] if fill_model == "touch" else bar["close"]
    if condition == "close_below_reference_open":
        return probe <= ref.get("open", math.inf)
    if condition == "close_below_reference_low":
        return probe <= ref.get("low", math.inf)
    if condition == "pullback_pct":
        if pullback_pct is None:
            return True
        return probe <= ref.get("close", math.inf) * (1.0 + float(pullback_pct) / 100.0)
    return True


def _planned_position(sizing: str, portfolio: Mapping, equity: float, max_positions: int) -> float:
    """종목당 배정 자본 (ARCHITECTURE 2장 '분할 매수')."""
    if sizing == "fixed_amount":
        return float(portfolio.get("amount_manwon") or 0) * 10_000.0
    if sizing == "fixed_qty":
        return 0.0  # 수량 고정 — 금액 배정 사용 안 함
    return equity / max(int(max_positions), 1)


def _entry_qty(rule: Mapping, sizing: str, portfolio: Mapping, planned: float,
               equity: float, price: float, cash: float) -> int:
    size_pct = float(rule.get("size_pct", 100.0)) / 100.0
    if sizing == "fixed_qty":
        qty = int(float(portfolio.get("qty") or 0) * size_pct)
    else:
        size_of = str(rule.get("size_of") or "planned_position")
        base = equity if size_of == "equity" else planned
        qty = int((base * size_pct) // price)
    affordable = int(cash // price)
    return max(min(qty, affordable), 0)


def _exit_qty(rule: Mapping, held: int) -> int:
    pct = float(rule.get("size_pct", 100.0)) / 100.0
    if pct >= 1.0:
        return held
    return min(held, int(held * pct))


def _exit_hit(rule: Mapping, ctx: EvalContext, li: int, bar: dict, w: dict,
              day: dt.date) -> Tuple[bool, Optional[float]]:
    """청산 조건 판정. ``when`` 이 없으면 type 별 기본 동작으로 대체한다."""
    when = rule.get("when")
    t = str(rule.get("type") or "")
    avg = w["cost"] / w["qty"] if w["qty"] else 0.0

    if when is None:
        if t == "trailing_stop":
            trail = float(rule.get("trail_pct") or 0.0)
            target = (w["peak"] or bar["high"]) * (1.0 - trail / 100.0)
            if bar["low"] <= target:
                return True, min(max(target, bar["low"]), bar["high"]) if bar["open"] > target else bar["open"]
            return False, None
        if t == "time_exit":
            hold = int(ctx.position.get("hold_days") or 0)
            limit = rule.get("max_hold_days", rule.get("hold_days"))
            if limit is not None and hold >= int(limit):
                return True, bar["close"]
            return False, None
        return False, None

    try:
        return bool(evaluate_condition(when, ctx, li)), None
    except DSLError:
        return False, None


def _close(w: dict, rec: dict, code: str, rule: Mapping, price: float, qty: int,
           exit_date: dt.date, slippage: float, fee: float, no: int,
           reason: str) -> Tuple[dict, float]:
    """포지션(또는 일부)을 청산하고 trade 레코드를 만든다."""
    sell_px = price * (1.0 - slippage)
    gross = sell_px * qty
    fees = gross * fee
    proceeds = gross - fees

    avg_paid = w["cost"] / w["qty"]
    cost_basis = avg_paid * qty
    pnl = proceeds - cost_basis
    ret_pct = (pnl / cost_basis * 100.0) if cost_basis else 0.0
    hold_days = (exit_date - w["entry_date"]).days if w["entry_date"] else 0

    w["qty"] -= qty
    w["cost"] -= cost_basis

    trade = {
        "no": no,
        "group_id": w.get("group_id") or f"{code}-{w['ref_date'].isoformat()}",
        "code": code,
        "name": rec["name"],
        "ref_date": w["ref_date"].isoformat(),
        "ref_amount_eok": _i(w["ref"].get("amount", 0.0) / EOK),
        "ref_open": _r(w["ref"].get("open"), 2),
        "fills": list(w["fill_log"]),
        "avg_price": _r(avg_paid, 2),
        "exit_date": exit_date.isoformat(),
        "exit_price": _r(sell_px, 2),
        "exit_rule": rule.get("id"),
        "exit_reason": reason,
        "hold_days": int(hold_days),
        "return_pct": _r(ret_pct),
        "pnl": _i(pnl),
    }
    return trade, proceeds


def _mtm(active: Mapping[str, dict], recs: Mapping[str, dict], gi: int) -> float:
    """보유 포지션 평가액."""
    total = 0.0
    for code, w in active.items():
        if w["qty"] <= 0:
            continue
        rec = recs[code]
        li = _last_index_upto(rec, gi)
        if li is None:
            total += w["cost"]
        else:
            total += w["qty"] * float(rec["close"][li])
    return total


def _empty_result(run_id, t0, warnings, sig, calendar, initial_capital, start, end,
                  note, assumptions) -> dict:
    warnings = list(warnings) + [note]
    values = [initial_capital] * len(calendar)
    sig.add(calendar[-1] if calendar else None, "DONE", None, note, "15:30:00")
    return {
        "run_id": run_id,
        "elapsed_sec": _r(time.perf_counter() - t0, 3),
        "warnings": warnings,
        "assumptions": assumptions,
        "metrics": compute_metrics(calendar, values, [], initial_capital, start, end),
        "equity": build_equity(calendar, values, initial_capital),
        "monthly": monthly_returns(calendar, values),
        "trades": [],
        "by_stock": [],
        "signals": sig.items,
    }
