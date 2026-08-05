"""LAG 지표 레지스트리.

- ``REGISTRY``  : ``{"SMA": spec, ...}``  — spec 은 ARCHITECTURE 4-4 의 응답 형태와 동일한 키를 갖는다.
- ``compute(name, params, df)`` : ``np.ndarray`` (서브필드가 있으면 ``dict[str, np.ndarray]``)

모든 계산은 pandas / numpy 벡터 연산이다. 파이썬 for 루프를 쓰지 않는다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

import numpy as np
import pandas as pd

from .errors import IndicatorError

__all__ = ["REGISTRY", "compute", "spec_list", "source_series", "indicator_key", "raw_column"]


# --------------------------------------------------------------------------------------
# 입력 시리즈 추출
# --------------------------------------------------------------------------------------

_BASE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_DERIVED_FIELDS = ("hl2", "hlc3", "ohlc4", "typical")

SOURCE_OPTIONS = list(_BASE_FIELDS) + list(_DERIVED_FIELDS)


def _raw(df: pd.DataFrame, field: str) -> np.ndarray:
    """대소문자 상관없이 봉 컬럼을 float64 배열로 뽑는다."""
    for cand in (field, field.capitalize(), field.upper(), field.lower()):
        if cand in df.columns:
            return np.asarray(df[cand], dtype="float64")
    raise IndicatorError(f"필요한 컬럼이 없습니다: {field} (있는 컬럼: {list(df.columns)})")


def raw_column(df: pd.DataFrame, name: str) -> np.ndarray:
    """대소문자 무관하게 원본 컬럼을 float64 배열로 뽑는다 (Marcap 등)."""
    return _raw(df, name)


def source_series(df: pd.DataFrame, source: str | None) -> np.ndarray:
    """``source`` 이름에 해당하는 배열을 돌려준다."""
    src = (source or "close").lower()
    if src in _BASE_FIELDS:
        return _raw(df, src)
    if src == "hl2":
        return (_raw(df, "high") + _raw(df, "low")) / 2.0
    if src in ("hlc3", "typical"):
        return (_raw(df, "high") + _raw(df, "low") + _raw(df, "close")) / 3.0
    if src == "ohlc4":
        return (
            _raw(df, "open") + _raw(df, "high") + _raw(df, "low") + _raw(df, "close")
        ) / 4.0
    raise IndicatorError(f"알 수 없는 source: {source}")


# --------------------------------------------------------------------------------------
# 벡터 헬퍼
# --------------------------------------------------------------------------------------


def _s(x: np.ndarray) -> pd.Series:
    return pd.Series(x, dtype="float64")


def _period(params: Dict[str, Any], key: str, default: int) -> int:
    v = params.get(key, default)
    if v is None:
        v = default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise IndicatorError(f"{key} 는 정수여야 합니다: {v!r}") from None
    if n < 1:
        raise IndicatorError(f"{key} 는 1 이상이어야 합니다: {n}")
    return n


def _fnum(params: Dict[str, Any], key: str, default: float) -> float:
    v = params.get(key, default)
    if v is None:
        v = default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise IndicatorError(f"{key} 는 숫자여야 합니다: {v!r}") from None


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    return _s(x).rolling(n, min_periods=n).mean().to_numpy()


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = _s(x).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()
    return out


def _wma(x: np.ndarray, n: int) -> np.ndarray:
    """선형 가중 이동평균 — convolution 으로 O(N·n) 벡터 연산."""
    out = np.full(x.shape[0], np.nan, dtype="float64")
    if x.shape[0] >= n:
        w = np.arange(1, n + 1, dtype="float64")
        w /= w.sum()
        conv = np.convolve(np.nan_to_num(x, nan=0.0), w[::-1], mode="valid")
        out[n - 1:] = conv
        # 원본에 NaN 이 섞여 있으면 해당 창은 무효 처리
        if np.isnan(x).any():
            bad = _s(np.isnan(x).astype("float64")).rolling(n, min_periods=n).sum().to_numpy()
            out[bad > 0] = np.nan
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder 평활 (alpha = 1/n)."""
    return _s(x).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def _rolling_mad(x: np.ndarray, n: int) -> np.ndarray:
    """이동 평균절대편차 — sliding_window_view 로 완전 벡터화."""
    out = np.full(x.shape[0], np.nan, dtype="float64")
    if x.shape[0] >= n:
        win = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = np.abs(win - win.mean(axis=1, keepdims=True)).mean(axis=1)
    return out


def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.true_divide(a, b)
    r[~np.isfinite(r)] = np.nan
    return r


def _true_range(df: pd.DataFrame) -> np.ndarray:
    h = _raw(df, "high")
    l = _raw(df, "low")
    c = _raw(df, "close")
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.nanmax(
        np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0
    )
    tr[0] = h[0] - l[0]
    return tr


# --------------------------------------------------------------------------------------
# 지표 구현
# --------------------------------------------------------------------------------------


def _f_sma(p, df):
    return _sma(source_series(df, p.get("source")), _period(p, "period", 20))


def _f_ema(p, df):
    return _ema(source_series(df, p.get("source")), _period(p, "period", 20))


def _f_wma(p, df):
    return _wma(source_series(df, p.get("source")), _period(p, "period", 20))


def _f_bbands(p, df):
    n = _period(p, "period", 20)
    k = _fnum(p, "stddev", 2.0)
    x = source_series(df, p.get("source"))
    mid = _sma(x, n)
    sd = _s(x).rolling(n, min_periods=n).std(ddof=0).to_numpy()
    return {"middle": mid, "upper": mid + k * sd, "lower": mid - k * sd, "width": 2 * k * sd}


def _f_rsi(p, df):
    n = _period(p, "period", 14)
    x = _s(source_series(df, p.get("source")))
    d = x.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    rd = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    rsi = np.where(rd == 0.0, 100.0, 100.0 - 100.0 / (1.0 + _safe_div(ru, rd)))
    rsi = np.asarray(rsi, dtype="float64")
    rsi[np.isnan(ru) | np.isnan(rd)] = np.nan
    return rsi


def _f_macd(p, df):
    fast = _period(p, "fast", 12)
    slow = _period(p, "slow", 26)
    sig = _period(p, "signal", 9)
    x = source_series(df, p.get("source"))
    ef = _s(x).ewm(span=fast, adjust=False).mean().to_numpy()
    es = _s(x).ewm(span=slow, adjust=False).mean().to_numpy()
    macd = ef - es
    macd[: slow - 1] = np.nan
    signal = _s(macd).ewm(span=sig, adjust=False, min_periods=sig).mean().to_numpy()
    return {"macd": macd, "signal": signal, "hist": macd - signal}


def _f_stoch(p, df):
    k = _period(p, "k", 14)
    d = _period(p, "d", 3)
    smooth = _period(p, "smooth", 3)
    h = _s(_raw(df, "high"))
    l = _s(_raw(df, "low"))
    c = _raw(df, "close")
    hh = h.rolling(k, min_periods=k).max().to_numpy()
    ll = l.rolling(k, min_periods=k).min().to_numpy()
    raw = 100.0 * _safe_div(c - ll, hh - ll)
    kk = _sma(raw, smooth) if smooth > 1 else raw
    dd = _sma(kk, d)
    return {"k": kk, "d": dd, "raw": raw}


def _f_atr(p, df):
    return _wilder(_true_range(df), _period(p, "period", 14))


def _f_adx(p, df):
    n = _period(p, "period", 14)
    h = _raw(df, "high")
    l = _raw(df, "low")
    up = np.concatenate(([np.nan], np.diff(h)))
    dn = np.concatenate(([np.nan], -np.diff(l)))
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pdm[0] = np.nan
    mdm[0] = np.nan
    atr = _wilder(_true_range(df), n)
    pdi = 100.0 * _safe_div(_wilder(pdm, n), atr)
    mdi = 100.0 * _safe_div(_wilder(mdm, n), atr)
    dx = 100.0 * _safe_div(np.abs(pdi - mdi), pdi + mdi)
    adx = _wilder(dx, n)
    return {"adx": adx, "pdi": pdi, "mdi": mdi}


def _f_cci(p, df):
    n = _period(p, "period", 20)
    tp = source_series(df, "hlc3")
    ma = _sma(tp, n)
    mad = _rolling_mad(tp, n)
    return _safe_div(tp - ma, 0.015 * mad)


def _f_obv(p, df):
    c = _raw(df, "close")
    v = _raw(df, "volume")
    step = np.zeros(c.shape[0], dtype="float64")
    if c.shape[0] > 1:
        step[1:] = np.sign(np.diff(c)) * v[1:]
    return np.nancumsum(step)


_VWAP_ANCHOR = {
    "session": "D",
    "day": "D",
    "daily": "D",
    "week": "W",
    "month": "M",
    "quarter": "Q",
    "year": "Y",
}


def _f_vwap(p, df):
    anchor = str(p.get("anchor") or "all").lower()
    tp = source_series(df, "hlc3")
    v = _raw(df, "volume")
    pv = tp * v
    freq = _VWAP_ANCHOR.get(anchor)
    if freq is None or not isinstance(df.index, pd.DatetimeIndex):
        return _safe_div(np.nancumsum(pv), np.nancumsum(v))
    key = df.index.to_period(freq)
    g = pd.DataFrame({"pv": pv, "v": v}).groupby(np.asarray(key), sort=False)
    return _safe_div(g["pv"].cumsum().to_numpy(), g["v"].cumsum().to_numpy())


def _f_envelope(p, df):
    n = _period(p, "period", 20)
    pct = _fnum(p, "pct", 5.0)
    mid = _sma(source_series(df, p.get("source")), n)
    return {"middle": mid, "upper": mid * (1 + pct / 100.0), "lower": mid * (1 - pct / 100.0)}


def _f_highest(p, df):
    n = _period(p, "period", 252)
    return _s(source_series(df, p.get("source"))).rolling(n, min_periods=n).max().to_numpy()


def _f_lowest(p, df):
    n = _period(p, "period", 252)
    return _s(source_series(df, p.get("source"))).rolling(n, min_periods=n).min().to_numpy()


def _f_donchian(p, df):
    n = _period(p, "period", 20)
    up = _s(_raw(df, "high")).rolling(n, min_periods=n).max().to_numpy()
    lo = _s(_raw(df, "low")).rolling(n, min_periods=n).min().to_numpy()
    return {"upper": up, "lower": lo, "middle": (up + lo) / 2.0}


# --------------------------------------------------------------------------------------
# 레지스트리
# --------------------------------------------------------------------------------------


def _P(name, type_, default, **extra):
    d = {"name": name, "type": type_, "default": default}
    d.update(extra)
    return d


_SRC = _P("source", "enum", "close", options=SOURCE_OPTIONS)


def _spec(key, label, params, fields, overlay, pane, fn):
    return {
        "key": key,
        "label": label,
        "params": params,
        "fields": fields,
        "overlay": overlay,
        "pane": pane,
        "fn": fn,
    }


REGISTRY: Dict[str, Dict[str, Any]] = {
    s["key"]: s
    for s in [
        _spec("SMA", "단순이동평균", [_P("period", "int", 20), _SRC], None, True, "price", _f_sma),
        _spec("EMA", "지수이동평균", [_P("period", "int", 20), _SRC], None, True, "price", _f_ema),
        _spec("WMA", "가중이동평균", [_P("period", "int", 20), _SRC], None, True, "price", _f_wma),
        _spec(
            "BBANDS",
            "볼린저밴드",
            [_P("period", "int", 20), _P("stddev", "float", 2.0), _SRC],
            ["upper", "middle", "lower", "width"],
            True,
            "price",
            _f_bbands,
        ),
        _spec("RSI", "상대강도지수", [_P("period", "int", 14), _SRC], None, False, "oscillator", _f_rsi),
        _spec(
            "MACD",
            "MACD",
            [_P("fast", "int", 12), _P("slow", "int", 26), _P("signal", "int", 9), _SRC],
            ["macd", "signal", "hist"],
            False,
            "oscillator",
            _f_macd,
        ),
        _spec(
            "STOCH",
            "스토캐스틱",
            [_P("k", "int", 14), _P("d", "int", 3), _P("smooth", "int", 3)],
            ["k", "d", "raw"],
            False,
            "oscillator",
            _f_stoch,
        ),
        _spec("ATR", "평균실체범위", [_P("period", "int", 14)], None, False, "oscillator", _f_atr),
        _spec(
            "ADX",
            "추세강도",
            [_P("period", "int", 14)],
            ["adx", "pdi", "mdi"],
            False,
            "oscillator",
            _f_adx,
        ),
        _spec("CCI", "상품채널지수", [_P("period", "int", 20)], None, False, "oscillator", _f_cci),
        _spec("OBV", "누적거래량", [], None, False, "volume", _f_obv),
        _spec(
            "VWAP",
            "거래량가중평균",
            [_P("anchor", "enum", "all", options=["all", "session", "week", "month", "quarter", "year"])],
            None,
            True,
            "price",
            _f_vwap,
        ),
        _spec(
            "ENVELOPE",
            "이격도밴드",
            [_P("period", "int", 20), _P("pct", "float", 5.0), _SRC],
            ["upper", "middle", "lower"],
            True,
            "price",
            _f_envelope,
        ),
        _spec(
            "HIGHEST",
            "N봉 최고값",
            [_P("period", "int", 252), _SRC],
            None,
            True,
            "price",
            _f_highest,
        ),
        _spec(
            "LOWEST",
            "N봉 최저값",
            [_P("period", "int", 252), _SRC],
            None,
            True,
            "price",
            _f_lowest,
        ),
        _spec(
            "DONCHIAN",
            "돈치안채널",
            [_P("period", "int", 20)],
            ["upper", "lower", "middle"],
            True,
            "price",
            _f_donchian,
        ),
    ]
}


def spec_list() -> list:
    """``GET /api/indicators`` 응답용 — 내부 전용 키(``fn``)를 제거한 사본."""
    out = []
    for key, s in REGISTRY.items():
        out.append(
            {
                "key": key,
                "label": s["label"],
                "params": [dict(p) for p in s["params"]],
                "fields": list(s["fields"]) if s["fields"] else None,
                "overlay": s["overlay"],
                "pane": s["pane"],
            }
        )
    return out


def indicator_key(name: str, params: Dict[str, Any]) -> str:
    """캐시 키. ``field`` 는 계산 결과 선택일 뿐이라 키에서 제외한다."""
    spec = REGISTRY.get(str(name).upper())
    if spec is None:
        raise IndicatorError(f"알 수 없는 지표: {name}")
    parts = []
    for p in spec["params"]:
        parts.append(f"{p['name']}={params.get(p['name'], p['default'])}")
    return f"{spec['key']}({','.join(parts)})"


def compute(name: str, params: Dict[str, Any] | None, df: pd.DataFrame):
    """지표를 계산한다.

    반환값은 ``np.ndarray`` (len == len(df), 앞부분 NaN).
    서브필드가 있는 지표는 ``dict[str, np.ndarray]``,
    단 ``params["field"]`` 가 주어지면 해당 필드 배열만 돌려준다.
    """
    key = str(name).upper()
    spec = REGISTRY.get(key)
    if spec is None:
        raise IndicatorError(f"알 수 없는 지표: {name}")
    p = dict(params or {})
    if df is None or len(df) == 0:
        empty = np.zeros(0, dtype="float64")
        if spec["fields"]:
            return empty if p.get("field") else {f: empty for f in spec["fields"]}
        return empty

    out = spec["fn"](p, df)

    field = p.get("field")
    if isinstance(out, dict):
        if field:
            f = str(field).lower()
            if f not in out:
                raise IndicatorError(
                    f"{key} 에 없는 서브필드: {field} (가능: {list(out)})"
                )
            return out[f]
        return out
    if field:
        raise IndicatorError(f"{key} 는 서브필드가 없습니다 (field={field!r})")
    return out
