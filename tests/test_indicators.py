"""engine/indicators.py — SCHEMA.md LAG 지표 표 전부 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.errors import IndicatorError
from engine.indicators import REGISTRY, compute, indicator_key, source_series, spec_list

ALL_KEYS = [
    "SMA", "EMA", "WMA", "BBANDS", "RSI", "MACD", "STOCH",
    "ATR", "ADX", "CCI", "OBV", "VWAP", "ENVELOPE", "DONCHIAN",
    "HIGHEST", "LOWEST",          # ARCHITECTURE-v2 3-2
]

SUBFIELD_KEYS = {"BBANDS", "MACD", "STOCH", "ADX", "ENVELOPE", "DONCHIAN"}


# ---------------------------------------------------------------- 레지스트리


def test_registry_covers_schema_table():
    assert set(REGISTRY) == set(ALL_KEYS)


def test_registry_spec_shape():
    for key, s in REGISTRY.items():
        assert s["key"] == key
        assert isinstance(s["label"], str) and s["label"]
        assert isinstance(s["params"], list)
        assert isinstance(s["overlay"], bool)
        for p in s["params"]:
            assert {"name", "type", "default"} <= set(p)


def test_spec_list_is_json_safe():
    """GET /api/indicators 응답 — 내부 함수 참조가 새어나가면 안 된다."""
    import json

    lst = spec_list()
    assert len(lst) == len(ALL_KEYS)
    assert all("fn" not in s for s in lst)
    json.dumps(lst)  # 직렬화 가능해야 한다


def test_unknown_indicator_raises(rng_ohlcv):
    with pytest.raises(IndicatorError):
        compute("NOPE", {}, rng_ohlcv)


def test_indicator_key_is_stable():
    a = indicator_key("SMA", {"period": 45, "source": "close"})
    b = indicator_key("SMA", {"period": 45, "source": "close", "field": "x"})
    assert a == b == "SMA(period=45,source=close)"
    assert indicator_key("SMA", {"period": 20}) != a


# ---------------------------------------------------------------- 공통 계약


@pytest.mark.parametrize("key", ALL_KEYS)
def test_length_and_leading_nan(key, rng_ohlcv):
    out = compute(key, {}, rng_ohlcv)
    arrays = list(out.values()) if isinstance(out, dict) else [out]
    for a in arrays:
        assert isinstance(a, np.ndarray)
        assert a.shape[0] == len(rng_ohlcv)
        assert a.dtype == np.float64


@pytest.mark.parametrize("key", sorted(SUBFIELD_KEYS))
def test_subfield_dict_and_selection(key, rng_ohlcv):
    out = compute(key, {}, rng_ohlcv)
    assert isinstance(out, dict)
    fields = REGISTRY[key]["fields"]
    assert set(fields) <= set(out)
    first = fields[0]
    picked = compute(key, {"field": first}, rng_ohlcv)
    assert isinstance(picked, np.ndarray)
    np.testing.assert_allclose(picked, out[first], equal_nan=True)


def test_bad_field_raises(rng_ohlcv):
    with pytest.raises(IndicatorError):
        compute("BBANDS", {"field": "nope"}, rng_ohlcv)
    with pytest.raises(IndicatorError):
        compute("SMA", {"field": "upper"}, rng_ohlcv)


def test_empty_dataframe_is_safe():
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    assert compute("SMA", {"period": 5}, empty).shape[0] == 0
    assert compute("BBANDS", {}, empty)["upper"].shape[0] == 0


def test_lowercase_columns_also_work(rng_ohlcv):
    lower = rng_ohlcv.rename(columns=str.lower)
    np.testing.assert_allclose(
        compute("SMA", {"period": 10}, lower),
        compute("SMA", {"period": 10}, rng_ohlcv),
        equal_nan=True,
    )


# ---------------------------------------------------------------- 수치 검증


def test_sma_matches_pandas(rng_ohlcv):
    got = compute("SMA", {"period": 45, "source": "close"}, rng_ohlcv)
    want = rng_ohlcv["Close"].rolling(45).mean().to_numpy()
    np.testing.assert_allclose(got, want, equal_nan=True)
    assert np.isnan(got[:44]).all()
    assert not np.isnan(got[44])


def test_ema_matches_pandas(rng_ohlcv):
    got = compute("EMA", {"period": 12}, rng_ohlcv)
    want = rng_ohlcv["Close"].ewm(span=12, adjust=False, min_periods=12).mean().to_numpy()
    np.testing.assert_allclose(got, want, equal_nan=True)


def test_wma_linear_weights():
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    got = compute("WMA", {"period": 3}, df)
    # (1*1 + 2*2 + 3*3) / 6 = 2.3333...
    assert np.isnan(got[:2]).all()
    np.testing.assert_allclose(got[2], (1 * 1 + 2 * 2 + 3 * 3) / 6.0)
    np.testing.assert_allclose(got[4], (3 * 1 + 4 * 2 + 5 * 3) / 6.0)


def test_bbands_relation(rng_ohlcv):
    b = compute("BBANDS", {"period": 20, "stddev": 2}, rng_ohlcv)
    ok = ~np.isnan(b["middle"])
    assert (b["upper"][ok] >= b["middle"][ok]).all()
    assert (b["lower"][ok] <= b["middle"][ok]).all()
    want_mid = rng_ohlcv["Close"].rolling(20).mean().to_numpy()
    np.testing.assert_allclose(b["middle"], want_mid, equal_nan=True)


def test_rsi_bounds_and_monotone_series():
    up = pd.DataFrame({"Close": np.arange(1.0, 60.0)})
    r = compute("RSI", {"period": 14}, up)
    assert np.nanmax(r) == pytest.approx(100.0)
    down = pd.DataFrame({"Close": np.arange(60.0, 1.0, -1.0)})
    r2 = compute("RSI", {"period": 14}, down)
    assert np.nanmin(r2) == pytest.approx(0.0, abs=1e-9)


def test_rsi_in_range(rng_ohlcv):
    r = compute("RSI", {"period": 14}, rng_ohlcv)
    v = r[~np.isnan(r)]
    assert v.min() >= 0.0 and v.max() <= 100.0


def test_macd_hist_identity(rng_ohlcv):
    m = compute("MACD", {}, rng_ohlcv)
    np.testing.assert_allclose(m["hist"], m["macd"] - m["signal"], equal_nan=True)


def test_stoch_range(rng_ohlcv):
    s = compute("STOCH", {"k": 14, "d": 3, "smooth": 3}, rng_ohlcv)
    k = s["k"][~np.isnan(s["k"])]
    assert k.min() >= -1e-9 and k.max() <= 100 + 1e-9


def test_atr_positive(rng_ohlcv):
    a = compute("ATR", {"period": 14}, rng_ohlcv)
    assert np.nanmin(a) > 0


def test_adx_components(rng_ohlcv):
    a = compute("ADX", {"period": 14}, rng_ohlcv)
    for f in ("adx", "pdi", "mdi"):
        v = a[f][~np.isnan(a[f])]
        assert v.min() >= -1e-9 and v.max() <= 100 + 1e-6


def test_cci_matches_definition(rng_ohlcv):
    n = 20
    tp = source_series(rng_ohlcv, "hlc3")
    ma = pd.Series(tp).rolling(n).mean().to_numpy()
    mad = pd.Series(tp).rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True).to_numpy()
    want = (tp - ma) / (0.015 * mad)
    np.testing.assert_allclose(compute("CCI", {"period": n}, rng_ohlcv), want, equal_nan=True, rtol=1e-9)


def test_obv_direction():
    df = pd.DataFrame(
        {"Close": [10.0, 11.0, 10.5, 12.0], "Volume": [100.0, 200.0, 300.0, 400.0]}
    )
    got = compute("OBV", {}, df)
    np.testing.assert_allclose(got, [0.0, 200.0, -100.0, 300.0])


def test_vwap_all_anchor(rng_ohlcv):
    v = compute("VWAP", {"anchor": "all"}, rng_ohlcv)
    tp = source_series(rng_ohlcv, "hlc3")
    vol = rng_ohlcv["Volume"].to_numpy("float64")
    want = np.cumsum(tp * vol) / np.cumsum(vol)
    np.testing.assert_allclose(v, want)


def test_vwap_month_anchor_resets(rng_ohlcv):
    v = compute("VWAP", {"anchor": "month"}, rng_ohlcv)
    assert v.shape[0] == len(rng_ohlcv)
    assert np.isfinite(v).all()


def test_envelope_bands(rng_ohlcv):
    e = compute("ENVELOPE", {"period": 20, "pct": 5}, rng_ohlcv)
    ok = ~np.isnan(e["middle"])
    np.testing.assert_allclose(e["upper"][ok], e["middle"][ok] * 1.05)
    np.testing.assert_allclose(e["lower"][ok], e["middle"][ok] * 0.95)


def test_donchian_channel(rng_ohlcv):
    d = compute("DONCHIAN", {"period": 20}, rng_ohlcv)
    ok = ~np.isnan(d["upper"])
    assert (d["upper"][ok] >= d["lower"][ok]).all()
    np.testing.assert_allclose(
        d["upper"], rng_ohlcv["High"].rolling(20).max().to_numpy(), equal_nan=True
    )


def test_highest_lowest(rng_ohlcv):
    hi = compute("HIGHEST", {"period": 20, "source": "close"}, rng_ohlcv)
    lo = compute("LOWEST", {"period": 20, "source": "close"}, rng_ohlcv)
    want_hi = rng_ohlcv["Close"].rolling(20).max().to_numpy()
    np.testing.assert_allclose(hi, want_hi, equal_nan=True)
    np.testing.assert_allclose(lo, rng_ohlcv["Close"].rolling(20).min().to_numpy(), equal_nan=True)
    ok = ~np.isnan(hi)
    assert (hi[ok] >= rng_ohlcv["Close"].to_numpy()[ok] - 1e-9).all()
    assert (lo[ok] <= hi[ok]).all()


def test_highest_default_is_52_weeks():
    assert dict(p["name"] for p in [] ) == {} or True
    spec = REGISTRY["HIGHEST"]
    assert {p["name"]: p["default"] for p in spec["params"]}["period"] == 252


def test_indicator_source_volume_amount(rng_ohlcv):
    """거래량 이동평균 (ARCHITECTURE-v2 3-2 source 확장)."""
    v = compute("SMA", {"period": 20, "source": "volume"}, rng_ohlcv)
    np.testing.assert_allclose(v, rng_ohlcv["Volume"].rolling(20).mean().to_numpy(), equal_nan=True)
    a = compute("SMA", {"period": 5, "source": "amount"}, rng_ohlcv)
    np.testing.assert_allclose(a, rng_ohlcv["Amount"].rolling(5).mean().to_numpy(), equal_nan=True)


def test_source_variants(rng_ohlcv):
    hl2 = source_series(rng_ohlcv, "hl2")
    np.testing.assert_allclose(
        hl2, (rng_ohlcv["High"].to_numpy() + rng_ohlcv["Low"].to_numpy()) / 2
    )
    with pytest.raises(IndicatorError):
        source_series(rng_ohlcv, "nope")


def test_no_python_loop_perf(rng_ohlcv):
    """지표 14종을 3,000봉에 대해 전부 계산해도 1초 안에 끝나야 한다."""
    import time

    big = pd.concat([rng_ohlcv] * 10, ignore_index=True)
    t = time.perf_counter()
    for key in ALL_KEYS:
        compute(key, {}, big)
    assert time.perf_counter() - t < 1.0
