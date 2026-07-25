"""성과 지표.

ARCHITECTURE 4-5 의 ``metrics`` / ``equity`` / ``monthly`` 페이로드를 만든다.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Dict, Iterable, List, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "RISK_FREE_RATE",
    "TRADING_DAYS",
    "build_equity",
    "monthly_returns",
    "trade_stats",
    "max_drawdown",
    "sharpe_ratio",
    "cagr",
    "compute_metrics",
]

#: 무위험수익률 (연 3%)
RISK_FREE_RATE = 0.03
#: 연간 거래일 수
TRADING_DAYS = 252


def _i(v):
    """원 단위 정수. None/NaN 안전."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if math.isfinite(f) else None


def _r(v, nd=2):
    """None/NaN 안전 반올림."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


# --------------------------------------------------------------------------------------
# equity / drawdown
# --------------------------------------------------------------------------------------


def drawdown_series(values: np.ndarray) -> np.ndarray:
    """각 시점의 낙폭(%) — 0 이하의 값."""
    v = np.asarray(values, dtype="float64")
    if v.size == 0:
        return v
    peak = np.maximum.accumulate(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = (v / peak - 1.0) * 100.0
    dd[~np.isfinite(dd)] = 0.0
    return dd


def max_drawdown(values: Sequence[float]) -> float:
    dd = drawdown_series(np.asarray(values, dtype="float64"))
    return float(dd.min()) if dd.size else 0.0


def build_equity(dates: Sequence[dt.date], values: Sequence[float], initial: float) -> Dict:
    """``{"dates": [...], "values": [...], "drawdown": [...]}``.

    ``values`` 는 초기 자본을 100 으로 정규화한 지수다 (ARCHITECTURE 4-5 예시와 동일).
    """
    v = np.asarray(values, dtype="float64")
    base = float(initial) if initial else 1.0
    idx = v / base * 100.0 if base else v
    dd = drawdown_series(idx)
    return {
        "dates": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates],
        "values": [_r(x, 4) for x in idx],
        "drawdown": [_r(x, 4) for x in dd],
    }


def monthly_returns(dates: Sequence[dt.date], values: Sequence[float]) -> List[Dict]:
    """월별 수익률 ``[{"month": "2025-01", "return_pct": 3.2}]``."""
    if len(dates) == 0:
        return []
    s = pd.Series(np.asarray(values, dtype="float64"),
                  index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]))
    monthly_last = s.resample("ME").last().dropna()
    if monthly_last.empty:
        return []
    prev = float(s.iloc[0])
    out = []
    for ts, val in monthly_last.items():
        ret = (float(val) / prev - 1.0) * 100.0 if prev else 0.0
        out.append({"month": f"{ts.year:04d}-{ts.month:02d}", "return_pct": _r(ret)})
        prev = float(val)
    return out


# --------------------------------------------------------------------------------------
# 위험조정 수익
# --------------------------------------------------------------------------------------


def sharpe_ratio(values: Sequence[float], rf: float = RISK_FREE_RATE) -> float | None:
    v = np.asarray(values, dtype="float64")
    if v.size < 3:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.diff(v) / v[:-1]
    ret = ret[np.isfinite(ret)]
    if ret.size < 2:
        return None
    sd = float(ret.std(ddof=1))
    if sd == 0.0:
        return None
    excess = float(ret.mean()) - rf / TRADING_DAYS
    return excess / sd * math.sqrt(TRADING_DAYS)


def cagr(initial: float, final: float, years: float) -> float | None:
    if initial <= 0 or years <= 0:
        return None
    if final <= 0:
        return -100.0
    return ((final / initial) ** (1.0 / years) - 1.0) * 100.0


# --------------------------------------------------------------------------------------
# 거래 통계
# --------------------------------------------------------------------------------------


def trade_stats(trades: Iterable[Dict]) -> Dict:
    """승률 / profit_factor / 평균 손익 / 평균 보유일."""
    tl = list(trades)
    n = len(tl)
    pnls = np.asarray([float(t.get("pnl") or 0.0) for t in tl], dtype="float64")
    rets = np.asarray([float(t.get("return_pct") or 0.0) for t in tl], dtype="float64")
    holds = np.asarray([float(t.get("hold_days") or 0.0) for t in tl], dtype="float64")

    win_mask = pnls > 0
    loss_mask = pnls < 0
    wins = int(win_mask.sum())
    losses = int(loss_mask.sum())

    gross_profit = float(pnls[win_mask].sum()) if wins else 0.0
    gross_loss = float(-pnls[loss_mask].sum()) if losses else 0.0
    if gross_loss > 0:
        pf = gross_profit / gross_loss
    elif gross_profit > 0:
        pf = None  # 손실 거래 없음 → 무한대. null 로 내보낸다.
    else:
        pf = 0.0

    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": _r(wins / n * 100.0) if n else 0.0,
        "profit_factor": _r(pf) if pf is not None else None,
        "avg_win_pct": _r(rets[win_mask].mean()) if wins else 0.0,
        "avg_loss_pct": _r(rets[loss_mask].mean()) if losses else 0.0,
        "avg_hold_days": _r(holds.mean(), 1) if n else 0.0,
        "gross_profit": _i(gross_profit),
        "gross_loss": _i(gross_loss),
    }


# --------------------------------------------------------------------------------------
# 종합
# --------------------------------------------------------------------------------------


def compute_metrics(
    dates: Sequence[dt.date],
    equity_values: Sequence[float],
    trades: Iterable[Dict],
    initial_capital: float,
    start: dt.date,
    end: dt.date,
    rf: float = RISK_FREE_RATE,
) -> Dict:
    """ARCHITECTURE 4-5 의 ``metrics`` 오브젝트를 만든다."""
    v = np.asarray(equity_values, dtype="float64")
    final = float(v[-1]) if v.size else float(initial_capital)
    total_return = (final / initial_capital - 1.0) * 100.0 if initial_capital else 0.0
    years = max((end - start).days / 365.25, 1e-9)

    st = trade_stats(trades)
    idx = v / initial_capital * 100.0 if initial_capital else v

    return {
        "total_return_pct": _r(total_return),
        "cagr_pct": _r(cagr(initial_capital, final, years)),
        "mdd_pct": _r(max_drawdown(idx)),
        "sharpe": _r(sharpe_ratio(v, rf)),
        "win_rate_pct": st["win_rate_pct"],
        "profit_factor": st["profit_factor"],
        "trades": st["trades"],
        "wins": st["wins"],
        "losses": st["losses"],
        "avg_win_pct": st["avg_win_pct"],
        "avg_loss_pct": st["avg_loss_pct"],
        "avg_hold_days": st["avg_hold_days"],
        "initial_capital": _i(initial_capital),
        "final_capital": _i(final),
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "years": _r(years),
        },
    }


def by_stock_summary(trades: Iterable[Dict]) -> List[Dict]:
    """``by_stock`` 집계 — 종목별 거래수 / 승률 / 손익."""
    agg: Dict[str, Dict] = {}
    for t in trades:
        code = t.get("code")
        row = agg.setdefault(
            code, {"code": code, "name": t.get("name"), "trades": 0, "wins": 0, "pnl": 0.0}
        )
        row["trades"] += 1
        row["pnl"] += float(t.get("pnl") or 0.0)
        if float(t.get("pnl") or 0.0) > 0:
            row["wins"] += 1
    out = []
    for row in agg.values():
        out.append(
            {
                "code": row["code"],
                "name": row["name"],
                "trades": row["trades"],
                "win_rate": _r(row["wins"] / row["trades"] * 100.0) if row["trades"] else 0.0,
                "pnl": _i(row["pnl"]),
            }
        )
    out.sort(key=lambda r: (r["pnl"] is None, -(r["pnl"] or 0.0)))
    return out
