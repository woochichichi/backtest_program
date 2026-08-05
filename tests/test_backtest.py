"""engine/backtest.py + validate.py + data.py + metrics.py 통합 검증.

가장 중요한 테스트는 ``test_strategy1_take_profit_scenario`` / ``test_strategy1_stop_loss_scenario``
— 합성 데이터로 전략1 전체 시나리오(기준일 → B1 → B2 → TP / SL)가 의도대로 체결되는지 본다.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import B1_BAR, B2_BAR, REF_BAR, ROOT, SL_BAR, TP_BAR
from engine.backtest import run_backtest
from engine.data import MarcapStore
from engine.errors import DataUnavailable, StrategyError
from engine.validate import validate_strategy

RESULT_KEYS = {
    "run_id", "elapsed_sec", "warnings", "assumptions", "metrics", "equity",
    "monthly", "trades", "by_stock", "signals",
}
ASSUMPTION_KEYS = {
    "resolution", "requested_resolution", "fill_model", "same_day_exit",
    "slippage_pct", "fee_pct", "exit_priority", "notes", "stats",
    "ignored_filters", "dart",
}
DART_KEYS = {"available", "as_of", "coverage_pct", "on_missing", "last_fetch"}
ASSUMPTION_STAT_KEYS = {
    "same_day_entry_exit", "same_day_entry_exit_pct", "ambiguous_bars",
    "same_day_profit_exits_blocked", "missing_financials", "missing_financials_pct",
}
METRIC_KEYS = {
    "total_return_pct", "cagr_pct", "mdd_pct", "sharpe", "win_rate_pct",
    "profit_factor", "trades", "wins", "losses", "avg_win_pct", "avg_loss_pct",
    "avg_hold_days", "initial_capital", "final_capital", "period",
}
TRADE_KEYS = {
    "no", "group_id", "code", "name", "ref_date", "ref_amount_eok", "ref_open", "fills",
    "avg_price", "exit_date", "exit_price", "exit_rule", "exit_reason",
    "hold_days", "return_pct", "pnl",
}


# ======================================================================================
# import / 데이터 없음
# ======================================================================================


def test_import_without_marcap_data():
    """marcap 폴더가 없어도 import 는 성공해야 한다."""
    out = subprocess.run(
        [sys.executable, "-c",
         "from engine.backtest import run_backtest; "
         "from engine.data import MarcapStore; "
         "s = MarcapStore(root='__no_such_dir__'); "
         "print('OK', s.available)"],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "OK False" in out.stdout


def test_store_unavailable_raises(tmp_path):
    store = MarcapStore(root=tmp_path / "nope", cache_dir=tmp_path / "cache")
    assert store.available is False
    with pytest.raises(DataUnavailable):
        store.latest_date()
    with pytest.raises(DataUnavailable):
        store.panel("2025-01-01", "2025-12-31")
    with pytest.raises(DataUnavailable):
        store.bars("005930")


def test_store_status_when_unavailable(tmp_path):
    st = MarcapStore(root=tmp_path / "nope", cache_dir=tmp_path / "cache").status()
    assert st["available"] is False
    assert st["file_count"] == 0
    assert set(st) >= {
        "available", "last_sync", "result", "latest_trade_date", "first_trade_date",
        "file_count", "git_rev", "row_count", "auto_sync",
    }
    assert set(st["auto_sync"]) == {"registered", "time", "task"}


def test_store_roundtrip_with_parquet(tmp_path, dates):
    """실제 parquet 파일을 만들어 lazy 로드 · panel 캐시 · bars 를 검증한다."""
    from conftest import make_frame

    data_dir = tmp_path / "marcap" / "data"
    data_dir.mkdir(parents=True)
    df = make_frame("100010", "테스트에이", "tp", dates)
    for year, g in df.groupby(df["Date"].dt.year):
        g.to_parquet(data_dir / f"marcap-{year}.parquet", index=False)

    cache = tmp_path / "cache"
    store = MarcapStore(root=tmp_path / "marcap", cache_dir=cache)
    assert store.available is True
    assert store.latest_date() == dates[-1]
    assert store.first_date() == dates[0]

    p = store.panel(dates[0], dates[10])
    assert len(p) == 11 and "Code" in p.columns and "Date" in p.columns
    assert list(cache.glob("panel-*.feather"))          # feather 캐시 생성
    assert len(store.panel(dates[0], dates[10])) == 11  # 캐시 재사용 경로

    b = store.bars("100010", dates[0], dates[5])
    assert b.index.name == "Date" and len(b) == 6
    assert store.names()["100010"] == "테스트에이"

    st = store.status()
    assert st["available"] is True and st["row_count"] == len(df)


# ======================================================================================
# validate
# ======================================================================================


def test_strategy1_passes_validation(strategy1_json):
    ok, errors = validate_strategy(strategy1_json)
    assert ok, errors
    assert errors == []


def test_all_registered_strategies_validate():
    for path in sorted((ROOT / "strategies").glob("*.json")):
        obj = json.loads(path.read_text(encoding="utf-8"))
        ok, errors = validate_strategy(obj)
        assert ok, f"{path.name}: {errors}"


@pytest.mark.parametrize("mutate,path_fragment", [
    (lambda s: s.pop("schema"), "schema"),
    (lambda s: s.update(schema="wrong/v9"), "schema"),
    (lambda s: s.pop("entries"), "entries"),
    (lambda s: s["entries"][1].update(size_pct=180), "entries[1].size_pct"),
    (lambda s: s["entries"][1].update(requires=["ZZ"]), "entries[1].requires[0]"),
    (lambda s: s["exits"][1]["when"].update(right={"indicator": "NOPE"}),
     "exits[1].when.right.indicator"),
    (lambda s: s["exits"][0]["when"].update(op="~~"), "exits[0].when.op"),
    (lambda s: s["universe"].update(markets=["NASDAQ"]), "universe.markets[0]"),
    (lambda s: s["universe"]["reference_day"].pop("spike_amount_krw_eok"),
     "universe.reference_day.spike_amount_krw_eok"),
    (lambda s: s["portfolio"].update(max_positions=0), "portfolio.max_positions"),
    (lambda s: s["execution"].update(fill_model="magic"), "execution.fill_model"),
    (lambda s: s["period"].update(start="어제"), "period.start"),
    (lambda s: s.update(exit_priority=["NOPE"]), "exit_priority[0]"),
    (lambda s: s["indicators"][0].update(period=0), "indicators[0].period"),
    (lambda s: s["entries"][0]["when"].update(left={"expr": ""}), "entries[0].when.left.expr"),
])
def test_validation_catches_errors(strategy1_json, mutate, path_fragment):
    s = json.loads(json.dumps(strategy1_json))
    mutate(s)
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == path_fragment for e in errors), (path_fragment, errors)


def test_validate_rejects_non_object():
    ok, errors = validate_strategy(["not", "a", "dict"])
    assert not ok and errors


def test_bbands_without_field_is_error(strategy1_json):
    s = json.loads(json.dumps(strategy1_json))
    s["exits"][1]["when"]["right"] = {"indicator": "BBANDS", "period": 20}
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "exits[1].when.right.field" for e in errors)


def test_run_backtest_rejects_invalid_strategy(strategy1, tp_store):
    strategy1["portfolio"]["max_positions"] = -1
    with pytest.raises(StrategyError) as ei:
        run_backtest(strategy1, tp_store)
    assert ei.value.errors


# ======================================================================================
# 응답 스키마 (ARCHITECTURE 4-5)
# ======================================================================================


def test_result_matches_api_schema(strategy1, tp_store):
    res = run_backtest(strategy1, tp_store)
    assert set(res) == RESULT_KEYS
    assert res["run_id"].startswith("r_")
    assert isinstance(res["elapsed_sec"], float)
    a = res["assumptions"]
    assert set(a) == ASSUMPTION_KEYS
    assert a["resolution"] == "1d"
    assert a["requested_resolution"] == "1m"
    assert a["fill_model"] == "touch"
    assert a["same_day_exit"] == "loss_only"
    assert a["slippage_pct"] == pytest.approx(0.1)
    assert a["fee_pct"] == pytest.approx(0.23)
    assert a["exit_priority"] == ["SL", "TP"]
    assert isinstance(a["notes"], list) and all(isinstance(n, str) and n for n in a["notes"])
    assert set(a["stats"]) == ASSUMPTION_STAT_KEYS
    assert set(a["dart"]) == DART_KEYS
    assert a["dart"]["available"] is False and a["dart"]["as_of"] is False
    assert a["dart"]["coverage_pct"] is None
    assert a["dart"]["on_missing"] == "include"
    assert all(isinstance(v, (int, float)) for v in a["stats"].values())
    assert set(res["metrics"]) == METRIC_KEYS
    assert set(res["metrics"]["period"]) == {"start", "end", "years"}
    assert set(res["equity"]) == {"dates", "values", "drawdown"}
    n = len(res["equity"]["dates"])
    assert len(res["equity"]["values"]) == n == len(res["equity"]["drawdown"])
    assert res["equity"]["values"][0] == pytest.approx(100.0)
    for m in res["monthly"]:
        assert set(m) == {"month", "return_pct"}
    for t in res["trades"]:
        assert set(t) == TRADE_KEYS
        for f in t["fills"]:
            assert set(f) == {"rule", "date", "price", "qty"}
    for b in res["by_stock"]:
        assert set(b) == {"code", "name", "trades", "win_rate", "pnl"}
    for s in res["signals"]:
        assert set(s) == {"ts", "level", "code", "message"}
    json.dumps(res)  # 서버는 그대로 직렬화만 한다


def test_warns_about_1m_resolution(strategy1, tp_store):
    res = run_backtest(strategy1, tp_store)
    assert any("일봉으로 근사" in w for w in res["warnings"])
    assert any("일봉 데이터만 사용했습니다" in n for n in res["assumptions"]["notes"])
    assert any("분봉 매매는 추후 지원 예정" in n for n in res["assumptions"]["notes"])


def test_assumption_notes_change_with_mode(strategy1, tp_store):
    for mode, needle in [
        ("loss_only", "이익이 나는 청산을 모두 다음 거래일로 미루고"),
        ("never", "어떤 청산도 평가하지 않고"),
        ("always", "실제보다 좋게 나올 수 있습니다"),
    ]:
        s = json.loads(json.dumps(strategy1))
        s["execution"]["same_day_exit"] = mode
        res = run_backtest(s, tp_store)
        assert res["assumptions"]["same_day_exit"] == mode
        assert any(needle in n for n in res["assumptions"]["notes"]), (mode, res["assumptions"]["notes"])


def test_always_mode_emits_optimism_warning(strategy1, tp_store):
    s = json.loads(json.dumps(strategy1))
    s["execution"]["same_day_exit"] = "always"
    res = run_backtest(s, tp_store)
    assert any("좋게 나올 수 있습니다" in w for w in res["warnings"])
    # 보수적 기본값에서는 그 경고가 없어야 한다
    assert not any("좋게 나올 수 있습니다" in w for w in run_backtest(strategy1, tp_store)["warnings"])


def test_same_day_exit_default_is_loss_only(strategy1_json, tp_store, dates):
    """execution 에서 키를 빼도 기본값은 loss_only 다."""
    s = json.loads(json.dumps(strategy1_json))
    s["period"] = {"start": dates[90].isoformat(), "end": "auto"}
    s["execution"].pop("same_day_exit", None)
    s["params"] = [p for p in s["params"] if p["path"] != "execution.same_day_exit"]
    assert run_backtest(s, tp_store)["assumptions"]["same_day_exit"] == "loss_only"


def test_strategy1_declares_same_day_exit(strategy1_json):
    assert strategy1_json["execution"]["same_day_exit"] == "loss_only"


def test_validation_rejects_bad_same_day_exit(strategy1_json):
    s = json.loads(json.dumps(strategy1_json))
    s["execution"]["same_day_exit"] = "sometimes"
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "execution.same_day_exit" for e in errors)


def test_progress_callback(strategy1, tp_store):
    seen = []
    run_backtest(strategy1, tp_store, progress=lambda d, t, m: seen.append((d, t, m)))
    assert seen
    assert all(t == 100 for _, t, _ in seen)
    assert seen[-1][0] == 100
    assert all(isinstance(m, str) for _, _, m in seen)


def test_progress_callback_errors_are_swallowed(strategy1, tp_store):
    def boom(done, total, message):
        raise RuntimeError("nope")

    res = run_backtest(strategy1, tp_store, progress=boom)
    assert res["trades"]


def test_signal_levels(strategy1, tp_store):
    res = run_backtest(strategy1, tp_store)
    levels = {s["level"] for s in res["signals"]}
    assert {"SCAN", "MATCH", "WATCH", "FILL", "POS", "PNL", "DONE"} <= levels


def test_warmup_history_is_requested(strategy1, tp_store, dates):
    """MA45 를 쓰므로 period.start 보다 훨씬 앞에서부터 패널을 읽어야 한다."""
    run_backtest(strategy1, tp_store)
    req_start, _ = tp_store.panel_calls[0]
    assert req_start < dt.date.fromisoformat(strategy1["period"]["start"])
    assert (dt.date.fromisoformat(strategy1["period"]["start"]) - req_start).days >= 60


def test_no_candidate_returns_empty_result(strategy1, flat_store):
    res = run_backtest(strategy1, flat_store)
    assert res["trades"] == []
    assert res["metrics"]["trades"] == 0
    assert res["metrics"]["final_capital"] == pytest.approx(100_000_000, abs=1)
    assert any("기준일" in w for w in res["warnings"])


# ======================================================================================
# ★ 핵심: 전략1 전체 시나리오
# ======================================================================================


def test_strategy1_take_profit_scenario(strategy1, tp_store, dates):
    """기준일 → B1(기준일 시가) → B2(-10%) → TP(평단 +10%) 가 의도대로 체결된다."""
    res = run_backtest(strategy1, tp_store)

    assert len(res["trades"]) == 1, res["trades"]
    t = res["trades"][0]

    # -- 기준일
    assert t["code"] == "100010"
    assert t["name"] == "테스트에이"
    assert t["ref_date"] == dates[REF_BAR].isoformat()
    assert t["ref_open"] == pytest.approx(4000.0)
    assert t["ref_amount_eok"] == pytest.approx(1200.0)

    # -- 분할 매수 2회
    assert [f["rule"] for f in t["fills"]] == ["B1", "B2"]
    b1, b2 = t["fills"]
    assert b1["date"] == dates[B1_BAR].isoformat()
    assert b1["price"] == pytest.approx(4000.0 * 1.001, rel=1e-9)   # 기준일 시가 + 슬리피지
    assert b1["qty"] == 1248                                        # 1,000만 × 50% ÷ 4,004
    assert b2["date"] == dates[B2_BAR].isoformat()
    assert b2["price"] == pytest.approx(3600.0 * 1.001, rel=1e-9)   # B1 -10% + 슬리피지
    assert b2["qty"] == 1387

    # -- 평단 = 실제 지불가 가중평균
    expect_avg = (b1["qty"] * b1["price"] + b2["qty"] * b2["price"]) / (b1["qty"] + b2["qty"])
    assert t["avg_price"] == pytest.approx(expect_avg, rel=1e-6)

    # -- 익절
    assert t["exit_rule"] == "TP"
    assert t["exit_date"] == dates[TP_BAR].isoformat()
    assert t["exit_price"] == pytest.approx(expect_avg * 1.10 * 0.999, rel=1e-6)
    assert t["hold_days"] == (dates[TP_BAR] - dates[B1_BAR]).days
    assert 9.0 < t["return_pct"] < 10.0          # 총 +10% 에서 수수료·슬리피지 차감
    assert t["pnl"] > 900_000

    # -- 집계
    m = res["metrics"]
    assert m["trades"] == 1 and m["wins"] == 1 and m["losses"] == 0
    assert m["win_rate_pct"] == 100.0
    assert m["final_capital"] > m["initial_capital"]
    assert res["by_stock"][0]["code"] == "100010"
    assert res["by_stock"][0]["pnl"] == t["pnl"]


def test_strategy1_stop_loss_scenario(strategy1, sl_store, dates):
    """같은 진입 후 45일선까지 무너지면 SL 로 청산된다 (exit_priority 로 SL 우선)."""
    res = run_backtest(strategy1, sl_store)

    assert len(res["trades"]) == 1, res["trades"]
    t = res["trades"][0]

    assert t["code"] == "100020"
    assert t["ref_date"] == dates[REF_BAR].isoformat()
    assert [f["rule"] for f in t["fills"]] == ["B1", "B2"]
    assert t["fills"][0]["price"] == pytest.approx(4000.0 * 1.001, rel=1e-9)
    assert t["fills"][1]["price"] == pytest.approx(3600.0 * 1.001, rel=1e-9)

    assert t["exit_rule"] == "SL"
    assert t["exit_date"] == dates[SL_BAR].isoformat()
    # 체결가는 그날 봉 [저가, 고가] 안 (MA45 터치가)
    assert 1900.0 * 0.999 <= t["exit_price"] <= 2550.0
    assert t["return_pct"] < -30.0
    assert t["pnl"] < 0

    m = res["metrics"]
    assert m["trades"] == 1 and m["wins"] == 0 and m["losses"] == 1
    assert m["win_rate_pct"] == 0.0
    assert m["mdd_pct"] < 0
    assert m["final_capital"] < m["initial_capital"]


def test_exit_priority_stop_loss_first(strategy1, sl_store):
    """exit_priority 를 뒤집으면 평가 순서가 실제로 바뀐다 (같은 봉에서 둘 다 성립할 때)."""
    from engine.backtest import _order_exits

    ordered = _order_exits(strategy1["exits"], ["SL", "TP"])
    assert [r["id"] for r in ordered] == ["SL", "TP"]
    ordered = _order_exits(strategy1["exits"], ["TP", "SL"])
    assert [r["id"] for r in ordered] == ["TP", "SL"]


def test_gap_through_target_fills_at_open(strategy1, dates):
    """갭으로 목표가를 지나쳐 시작하면 목표가가 아니라 당일 시가로 체결된다."""
    import numpy as np

    from conftest import FakeStore, make_frame

    f = make_frame("100010", "테스트에이", "tp", dates)
    # B1 봉을 갭하락으로 바꾼다: 시가 3,500 (< 기준일 시가 4,000)
    f.loc[B1_BAR, ["Open", "High", "Low", "Close"]] = [3500.0, 3550.0, 3300.0, 3400.0]
    store = FakeStore([f])

    res = run_backtest(strategy1, store)
    b1 = res["trades"][0]["fills"][0]
    assert b1["price"] == pytest.approx(3500.0 * 1.001, rel=1e-9)   # 목표가 4,000 이 아니라 시가


def test_forced_close_at_period_end(strategy1, dates):
    """청산 조건이 안 걸리면 마지막 거래일 종가로 강제 청산되고 사유는 '기간종료'."""
    from conftest import FakeStore, make_frame

    s = json.loads(json.dumps(strategy1))
    s["period"]["end"] = dates[B2_BAR + 3].isoformat()   # B2 직후에 구간을 끊는다

    f = make_frame("100010", "테스트에이", "tp", dates)
    # 진입 이후를 완전 횡보로 만들어 TP/SL 어느 쪽도 안 걸리게 한다
    f.loc[B2_BAR + 1:, ["Open", "High", "Low", "Close"]] = [3620.0, 3640.0, 3600.0, 3620.0]
    store = FakeStore([f])

    res = run_backtest(s, store)
    t = res["trades"][0]
    assert t["exit_reason"] == "기간종료"
    assert t["exit_rule"] == "EOD"
    assert t["exit_date"] == dates[B2_BAR + 3].isoformat()
    assert t["exit_price"] == pytest.approx(3620.0 * 0.999, rel=1e-9)


def test_valid_days_after_reference_expiry(strategy1, dates):
    """기준일 이후 유효기간 내에 B1 이 안 걸리면 후보를 폐기한다."""
    from conftest import FakeStore, make_frame

    s = json.loads(json.dumps(strategy1))
    s["universe"]["valid_days_after_reference"] = 2   # bar 100 → bar 102 까지

    f = make_frame("100010", "테스트에이", "tp", dates)
    store = FakeStore([f])
    res = run_backtest(s, store)

    assert res["trades"] == []
    assert any("후보 폐기" in sig["message"] for sig in res["signals"])


def test_max_positions_limits_concurrent_entries(strategy1, dates):
    from conftest import FakeStore, make_frame

    s = json.loads(json.dumps(strategy1))
    s["portfolio"]["max_positions"] = 1

    frames = [
        make_frame("100010", "에이", "tp", dates),
        make_frame("100050", "비", "tp", dates),
    ]
    res = run_backtest(s, FakeStore(frames))
    # 같은 날 둘 다 조건이 성립하지만 한 종목만 진입한다
    assert len({t["code"] for t in res["trades"]}) == 1


def test_equity_curve_and_monthly(strategy1, tp_store, dates):
    res = run_backtest(strategy1, tp_store)
    eq = res["equity"]
    assert eq["dates"][0] == dates[90].isoformat()
    assert eq["dates"][-1] == dates[-1].isoformat()
    assert all(d <= 0.0 for d in eq["drawdown"])
    assert res["monthly"]
    assert all(len(m["month"]) == 7 for m in res["monthly"])


# ======================================================================================
# ★ execution.same_day_exit — 진입 당일 청산 정책
# ======================================================================================


def _mode(strategy1, mode):
    s = json.loads(json.dumps(strategy1))
    s["execution"]["same_day_exit"] = mode
    return s


def _same_day_tp_store(dates):
    """B2 체결 봉(107)에서 익절가까지 함께 닿는 프레임 — 순서를 알 수 없는 애매한 봉."""
    from conftest import FakeStore, make_frame

    f = make_frame("100010", "테스트에이", "tp", dates)
    f.loc[B2_BAR, ["Open", "High", "Low", "Close"]] = [3710.0, 4300.0, 3550.0, 4200.0]
    return FakeStore([f])


def _same_day_sl_store(dates):
    """B2 체결 봉(107)에서 45일선까지 함께 무너지는 프레임."""
    from conftest import FakeStore, make_frame

    f = make_frame("100020", "테스트비", "sl", dates)
    f.loc[B2_BAR, ["Open", "High", "Low", "Close"]] = [3710.0, 3730.0, 2000.0, 2100.0]
    return FakeStore([f])


def test_same_day_take_profit_only_with_always(strategy1, dates):
    """같은 봉에서 진입+익절 — always 만 당일 체결, loss_only/never 는 다음 거래일로 이월."""
    store_of = {m: _same_day_tp_store(dates) for m in ("always", "loss_only", "never")}

    res = run_backtest(_mode(strategy1, "always"), store_of["always"])
    t = res["trades"][0]
    assert t["exit_rule"] == "TP"
    assert t["exit_date"] == dates[B2_BAR].isoformat()      # B2 체결 당일 익절

    for mode in ("loss_only", "never"):
        res = run_backtest(_mode(strategy1, mode), store_of[mode])
        t = res["trades"][0]
        assert t["exit_rule"] == "TP"
        assert t["exit_date"] > dates[B2_BAR].isoformat(), mode
        assert t["exit_date"] == dates[TP_BAR].isoformat(), mode

    # 세 경우 모두 "순서를 알 수 없는 봉" 으로 집계된다
    for mode in ("always", "loss_only", "never"):
        res = run_backtest(_mode(strategy1, mode), _same_day_tp_store(dates))
        assert res["assumptions"]["stats"]["ambiguous_bars"] >= 1, mode


def test_same_day_stop_loss_fires_under_loss_only(strategy1, dates):
    """loss_only 는 진입 당일에도 손절을 발동시킨다. never 만 다음 거래일로 미룬다."""
    for mode in ("always", "loss_only"):
        res = run_backtest(_mode(strategy1, mode), _same_day_sl_store(dates))
        t = res["trades"][0]
        assert t["exit_rule"] == "SL", mode
        assert t["exit_date"] == dates[B2_BAR].isoformat(), mode

    res = run_backtest(_mode(strategy1, "never"), _same_day_sl_store(dates))
    t = res["trades"][0]
    assert t["exit_rule"] == "SL"
    assert t["exit_date"] > dates[B2_BAR].isoformat()


def test_loss_only_blocks_tp_but_not_sl(strategy1, dates):
    """같은 설정(loss_only)에서 TP 는 막히고 SL 은 통과한다 — 손실 방향만 허용."""
    tp = run_backtest(_mode(strategy1, "loss_only"), _same_day_tp_store(dates))["trades"][0]
    sl = run_backtest(_mode(strategy1, "loss_only"), _same_day_sl_store(dates))["trades"][0]
    assert tp["exit_date"] > dates[B2_BAR].isoformat()      # 익절은 이월
    assert sl["exit_date"] == dates[B2_BAR].isoformat()     # 손절은 당일


def test_deferred_exit_is_logged(strategy1, dates):
    res = run_backtest(_mode(strategy1, "loss_only"), _same_day_tp_store(dates))
    assert any("이월" in s["message"] and "same_day_exit=loss_only" in s["message"]
               for s in res["signals"])


def test_loss_only_ignores_rule_type(strategy1, dates):
    """판정은 type 이 아니라 체결가 기준이다 — type 을 떼도 결과가 같아야 한다."""
    base = run_backtest(_mode(strategy1, "loss_only"), _same_day_tp_store(dates))
    s = _mode(strategy1, "loss_only")
    for r in s["exits"]:
        r.pop("type", None)
    untyped = run_backtest(s, _same_day_tp_store(dates))
    assert untyped["trades"][0]["exit_date"] == base["trades"][0]["exit_date"]
    assert untyped["trades"][0]["exit_date"] > dates[B2_BAR].isoformat()   # 이익이므로 이월


# --------------------------------------------------------------------------------------
# ★ 라벨은 손절인데 체결가가 평단 위인 경우 (전략1의 45일선 손절이 실제로 이렇다)
# --------------------------------------------------------------------------------------


def _sl_above_avg_store(dates):
    """45일선을 진입가 **위**에 두어, SL 규칙이 사실상 익절로 동작하게 만든 프레임."""
    from conftest import FakeStore, make_frame

    f = make_frame("100010", "테스트에이", "tp", dates)   # Amount 스케줄(기준일 1,200억) 재사용
    # 워밍업 100봉을 6,000 평보합으로 덮어써 MA45 를 6,000 근처로 올린다
    f.loc[: REF_BAR - 1, ["Open", "High", "Low", "Close"]] = [6000.0, 6050.0, 5950.0, 6000.0]
    f.loc[REF_BAR, ["Open", "High", "Low", "Close"]] = [4000.0, 4100.0, 3900.0, 3950.0]
    # 기준일 다음 봉: 저가 3,800 ≤ 기준일 시가 4,000 → B1 체결. 동시에 저가 ≤ MA45 → SL 성립
    f.loc[REF_BAR + 1, ["Open", "High", "Low", "Close"]] = [4050.0, 4200.0, 3800.0, 3900.0]
    f.loc[REF_BAR + 2, ["Open", "High", "Low", "Close"]] = [3900.0, 3950.0, 3700.0, 3750.0]
    f.loc[REF_BAR + 3:, ["Open", "High", "Low", "Close"]] = [3750.0, 3800.0, 3700.0, 3750.0]
    return FakeStore([f])


def test_profitable_stop_loss_is_blocked_on_entry_day(strategy1, dates):
    """type 은 stop_loss 지만 체결가가 평단 위 → loss_only 는 당일 차단하고 이월한다."""
    entry_day = dates[REF_BAR + 1].isoformat()

    # always: 진입 당일에 그대로 체결되고, 손절 규칙인데 **이익**으로 끝난다
    a = run_backtest(_mode(strategy1, "always"), _sl_above_avg_store(dates))["trades"][0]
    assert a["exit_rule"] == "SL"
    assert a["exit_date"] == entry_day
    assert a["pnl"] > 0, "이 픽스처는 '이익 나는 손절'이어야 한다"
    assert a["exit_price"] > a["avg_price"]

    # loss_only: 같은 봉인데 차단 → 다음 거래일에 손실로 청산
    res = run_backtest(_mode(strategy1, "loss_only"), _sl_above_avg_store(dates))
    t = res["trades"][0]
    assert t["exit_rule"] == "SL"
    assert t["exit_date"] == dates[REF_BAR + 2].isoformat()
    assert t["pnl"] < 0
    assert res["assumptions"]["stats"]["same_day_profit_exits_blocked"] == 1
    assert res["assumptions"]["stats"]["ambiguous_bars"] >= 1
    assert any("이월" in s["message"] for s in res["signals"])

    # never 도 같은 결과 (모두 차단)
    n = run_backtest(_mode(strategy1, "never"), _sl_above_avg_store(dates))["trades"][0]
    assert n["exit_date"] == t["exit_date"]


def test_loss_side_exit_still_allowed_same_day(strategy1, dates):
    """순손익이 손실이면 진입 당일에도 통과한다 (차단 카운터도 안 올라간다)."""
    res = run_backtest(_mode(strategy1, "loss_only"), _same_day_sl_store(dates))
    t = res["trades"][0]
    assert t["exit_date"] == dates[B2_BAR].isoformat()
    assert t["pnl"] < 0
    assert res["assumptions"]["stats"]["same_day_profit_exits_blocked"] == 0


def test_always_never_blocks_nothing(strategy1, dates):
    for store in (_same_day_tp_store(dates), _sl_above_avg_store(dates)):
        res = run_backtest(_mode(strategy1, "always"), store)
        assert res["assumptions"]["stats"]["same_day_profit_exits_blocked"] == 0


def test_blocked_count_appears_in_notes(strategy1, dates):
    res = run_backtest(_mode(strategy1, "loss_only"), _sl_above_avg_store(dates))
    assert any("이익으로 청산될 수 있었지만" in n for n in res["assumptions"]["notes"])
    assert any("체결가 기준" in n for n in res["assumptions"]["notes"])


def test_same_day_stats(strategy1, dates):
    res = run_backtest(_mode(strategy1, "always"), _same_day_tp_store(dates))
    st = res["assumptions"]["stats"]
    assert st["ambiguous_bars"] >= 1
    assert st["same_day_entry_exit"] == sum(1 for t in res["trades"] if t["hold_days"] == 0)
    assert 0.0 <= st["same_day_entry_exit_pct"] <= 100.0


def test_signal_truncation_reports_dropped_count(strategy1, dates, monkeypatch):
    import engine.backtest as bt

    monkeypatch.setattr(bt, "MAX_SIGNALS", 3)
    res = run_backtest(strategy1, _same_day_tp_store(dates))
    assert len(res["signals"]) == 3
    dropped = [w for w in res["warnings"] if "생략했습니다" in w]
    assert dropped and "3건을 넘어" in dropped[0]
    assert "0건은 생략" not in dropped[0]


def test_performance_budget(strategy1, dates):
    """후보가 아닌 종목은 파이썬 루프를 타지 않는다 — 300종목 스캔이 3초 이내."""
    import time

    from conftest import FakeStore, make_frame

    frames = [make_frame(f"1{i:04d}0", f"필러{i}", "flat", dates) for i in range(300)]
    frames.append(make_frame("100010", "테스트에이", "tp", dates))
    store = FakeStore(frames)

    t = time.perf_counter()
    res = run_backtest(strategy1, store)
    elapsed = time.perf_counter() - t
    assert elapsed < 3.0, f"{elapsed:.2f}s"
    assert len(res["trades"]) == 1


# ======================================================================================
# ★ ARCHITECTURE-v2 — 취소 / 진행률
# ======================================================================================


def test_should_cancel_raises(strategy1, tp_store):
    from engine.errors import BacktestCancelled

    with pytest.raises(BacktestCancelled) as ei:
        run_backtest(strategy1, tp_store, should_cancel=lambda: True)
    assert "취소" in str(ei.value)
    assert ei.value.phase


def test_should_cancel_false_runs_normally(strategy1, tp_store):
    res = run_backtest(strategy1, tp_store, should_cancel=lambda: False)
    assert res["trades"]


def test_cancel_midway(strategy1, tp_store):
    """N 번째 확인부터 True 를 돌려주면 그 지점에서 멈춘다."""
    from engine.errors import BacktestCancelled

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(BacktestCancelled):
        run_backtest(strategy1, tp_store, should_cancel=cancel)
    assert calls["n"] >= 4


def test_cancel_callback_errors_are_swallowed(strategy1, tp_store):
    def boom():
        raise RuntimeError("nope")

    assert run_backtest(strategy1, tp_store, should_cancel=boom)["trades"]


def test_progress_v1_three_arg_callback_still_works(strategy1, tp_store):
    seen = []
    run_backtest(strategy1, tp_store, progress=lambda d, t, m: seen.append((d, t, m)))
    assert seen and all(t == 100 for _, t, _ in seen)
    assert seen[-1][0] == 100


def test_progress_v2_five_arg_callback(strategy1, tp_store):
    seen = []

    def prog(done, total, message, phase=None, eta_sec=None):
        seen.append((done, total, message, phase, eta_sec))

    run_backtest(strategy1, tp_store, progress=prog)
    assert seen
    phases = {p for _, _, _, p, _ in seen}
    assert {"데이터 읽는 중", "기준일 찾는 중", "종목별 매매 계산 중", "성과 계산 중"} & phases
    assert all(e is None or isinstance(e, (int, float)) for *_, e in seen)
    assert all(isinstance(m, str) and m for _, _, m, _, _ in seen)
    assert seen[-1][0] == 100


def test_progress_messages_show_concrete_progress(strategy1, tp_store):
    seen = []
    run_backtest(strategy1, tp_store,
                 progress=lambda d, t, m, phase=None, eta=None: seen.append(m))
    assert any("/" in m for m in seen), "진척이 보이는 (n/m) 형태 메시지가 있어야 한다"


def test_progress_never_goes_backwards(strategy1, tp_store):
    seen = []
    run_backtest(strategy1, tp_store,
                 progress=lambda d, t, m, phase=None, eta=None: seen.append(d))
    assert seen == sorted(seen)


# ======================================================================================
# ★ ARCHITECTURE-v2 — DSL 확장
# ======================================================================================


def _v2_strategy(strategy1, **over):
    s = json.loads(json.dumps(strategy1))
    s.update(over)
    return s


def test_custom_reference_day_matches_amount_spike(strategy1, tp_store, dates):
    """rule="custom" 으로 amount_spike 와 동등한 조건을 쓰면 같은 결과가 나와야 한다."""
    base = run_backtest(strategy1, tp_store)

    s = json.loads(json.dumps(strategy1))
    s["universe"]["reference_day"] = {
        "rule": "custom",
        "lookback_days": 20,
        "spike_amount_krw_eok": 1000,
        "prev_day_amount_max_eok": 200,
        "when": {"op": "and", "conditions": [
            {"op": ">=", "left": "amount", "right": {"expr": "spike_amount_krw_eok * 100000000"}},
            {"op": "<=", "left": "prev.amount", "right": {"expr": "prev_day_amount_max_eok * 100000000"}},
        ]},
    }
    s["params"] = [p for p in s["params"]
                   if not p["path"].startswith("universe.reference_day")]
    ok, errors = validate_strategy(s)
    assert ok, errors

    got = run_backtest(s, tp_store)
    assert [t["ref_date"] for t in got["trades"]] == [t["ref_date"] for t in base["trades"]]
    assert [t["exit_rule"] for t in got["trades"]] == [t["exit_rule"] for t in base["trades"]]


def test_custom_rule_requires_when(strategy1):
    s = json.loads(json.dumps(strategy1))
    s["universe"]["reference_day"] = {"rule": "custom", "lookback_days": 20}
    s["params"] = [p for p in s["params"] if not p["path"].startswith("universe.reference_day")]
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "universe.reference_day.when" for e in errors)


def test_highest_operand_in_reference_day(strategy1, tp_store, dates):
    """HIGHEST 로 신고가 조건을 걸면 기준일이 실제로 걸러진다."""
    s = json.loads(json.dumps(strategy1))
    s["indicators"].append({"key": "HIGH252", "type": "HIGHEST", "period": 252,
                            "source": "close", "plot": False})
    s["universe"]["reference_day"] = {
        "rule": "custom", "lookback_days": 20, "spike_amount_krw_eok": 1000,
        "when": {"op": "and", "conditions": [
            {"op": ">=", "left": "amount", "right": {"expr": "spike_amount_krw_eok * 100000000"}},
            {"op": ">=", "left": "close", "right": "HIGH252"},
        ]},
    }
    s["params"] = [p for p in s["params"] if not p["path"].startswith("universe.reference_day")]
    ok, errors = validate_strategy(s)
    assert ok, errors
    # 200봉짜리 합성 데이터라 252봉 HIGHEST 는 전부 NaN → 조건이 성립하지 않는다
    assert run_backtest(s, tp_store)["trades"] == []

    s["indicators"][-1]["period"] = 20
    got = run_backtest(s, tp_store)
    assert got["trades"], "20봉 신고가로 낮추면 기준일이 잡혀야 한다"


def test_entry_operand_only_in_exits(strategy1):
    s = json.loads(json.dumps(strategy1))
    s["entries"][0]["when"] = {"op": "<=", "left": "low", "right": "entry.low"}
    ok, errors = validate_strategy(s)
    assert not ok
    assert any("entry.*" in e["message"] for e in errors)


def test_entry_operand_in_exit_rule(strategy1, tp_store, dates):
    """entry.low 이탈 손절 — 진입 봉의 저가를 참조한다."""
    s = json.loads(json.dumps(strategy1))
    s["exits"] = [{
        "id": "SL", "label": "손절 (진입일 저가 이탈)", "type": "stop_loss",
        "when": {"op": "<", "left": "close", "right": "entry.low"},
        "price": "close", "size_pct": 100,
    }]
    s["exit_priority"] = ["SL"]
    s["params"] = [p for p in s["params"]
                   if not p["path"].startswith("exits[1]")
                   and not p["path"].startswith("exits[0].target")]
    ok, errors = validate_strategy(s)
    assert ok, errors
    res = run_backtest(s, tp_store)
    t = res["trades"][0]
    assert t["exit_rule"] == "SL"
    # 진입봉(B1, bar 104)의 저가 3,900 아래로 종가(3,820)가 내려간 바로 다음 봉에서 청산
    assert t["exit_date"] == dates[B1_BAR + 1].isoformat()


def test_marcap_operand(strategy1, tp_store):
    """시가총액 조건 — marcap 은 원 단위 그대로 쓴다."""
    s = json.loads(json.dumps(strategy1))
    s["universe"]["reference_day"] = {
        "rule": "custom", "lookback_days": 20, "spike_amount_krw_eok": 1000,
        "market_cap_min_eok": 100000000,
        "when": {"op": "and", "conditions": [
            {"op": ">=", "left": "amount", "right": {"expr": "spike_amount_krw_eok * 100000000"}},
            {"op": ">=", "left": "marcap", "right": {"expr": "market_cap_min_eok * 100000000"}},
        ]},
    }
    s["params"] = [p for p in s["params"] if not p["path"].startswith("universe.reference_day")]
    assert run_backtest(s, tp_store)["trades"] == []   # 합성 종목은 시총이 훨씬 작다


def test_prev_operand(ohlcv):
    from engine.dsl import EvalContext, evaluate_operand

    ctx = EvalContext(ohlcv)
    close = evaluate_operand("close", ctx)
    prev = evaluate_operand("prev.close", ctx)
    assert np_isnan(prev[0])
    assert list(prev[1:]) == list(close[:-1])


def np_isnan(x):
    import math

    return math.isnan(float(x))


# --------------------------------------------------------------------------------------
# 부분 청산 + group_id
# --------------------------------------------------------------------------------------


def test_partial_exit_keeps_position(strategy1, tp_store, dates):
    """size_pct 50 이면 절반만 팔고 잔량이 남아 나머지 청산 규칙이 계속 평가된다."""
    s = json.loads(json.dumps(strategy1))
    s["exits"][0]["size_pct"] = 50            # TP 를 1차 익절로
    res = run_backtest(s, tp_store)

    assert len(res["trades"]) == 2, res["trades"]
    first, second = res["trades"]
    assert first["exit_rule"] == "TP"
    assert first["exit_date"] == dates[TP_BAR].isoformat()
    # 잔량은 계속 살아남아 나머지 청산 규칙(SL)이 이어서 평가된다
    assert second["exit_rule"] == "SL"
    assert second["exit_date"] > first["exit_date"]

    # 같은 진입에서 나온 청산이므로 group_id 를 공유한다
    assert first["group_id"] == second["group_id"]
    assert isinstance(first["group_id"], str) and first["group_id"]

    # 부분 청산 후에도 평단은 그대로 유지된다
    assert first["avg_price"] == second["avg_price"]
    assert first["fills"] == second["fills"]

    # 매도 수량은 원래 수량의 절반씩
    total_qty = sum(f["qty"] for f in first["fills"])
    sells = [sg for sg in res["signals"] if sg["level"] == "FILL" and "SELL" in sg["message"]]
    assert len(sells) == 2
    assert f"{total_qty // 2}주 (일부)" in sells[0]["message"]
    assert "(전량)" in sells[1]["message"]
    assert any("부분 청산 후 잔량" in sg["message"] for sg in res["signals"])


def test_partial_exit_rule_fires_once(strategy1, tp_store):
    """부분 청산 규칙은 한 포지션에서 한 번만 발동한다 (반복 매도 방지)."""
    s = json.loads(json.dumps(strategy1))
    s["exits"][0]["size_pct"] = 50
    res = run_backtest(s, tp_store)
    assert sum(1 for t in res["trades"] if t["exit_rule"] == "TP") == 1


def test_group_id_present_on_every_trade(strategy1, tp_store):
    res = run_backtest(strategy1, tp_store)
    assert all(isinstance(t["group_id"], str) and t["group_id"] for t in res["trades"])


# --------------------------------------------------------------------------------------
# time_exit
# --------------------------------------------------------------------------------------


def test_time_exit_by_when(strategy1, tp_store, dates):
    """position.hold_days 는 **영업일(봉) 수** 다."""
    s = json.loads(json.dumps(strategy1))
    s["exits"] = [{
        "id": "TIME", "label": "시간 청산", "type": "time_exit",
        "hold_days": 3,
        "when": {"op": ">=", "left": "position.hold_days", "right": "hold_days"},
        "price": "close", "size_pct": 100,
    }]
    s["exit_priority"] = ["TIME"]
    s["params"] = [p for p in s["params"] if not p["path"].startswith("exits[")]
    ok, errors = validate_strategy(s)
    assert ok, errors
    t = run_backtest(s, tp_store)["trades"][0]
    assert t["exit_rule"] == "TIME"
    assert t["exit_date"] == dates[B1_BAR + 3].isoformat()   # 진입봉 + 3봉


def test_time_exit_without_when(strategy1, tp_store, dates):
    s = json.loads(json.dumps(strategy1))
    s["exits"] = [{"id": "TIME", "label": "시간 청산", "type": "time_exit",
                   "max_hold_days": 2, "size_pct": 100}]
    s["exit_priority"] = ["TIME"]
    s["params"] = [p for p in s["params"] if not p["path"].startswith("exits[")]
    ok, errors = validate_strategy(s)
    assert ok, errors
    t = run_backtest(s, tp_store)["trades"][0]
    assert t["exit_rule"] == "TIME"
    assert t["exit_date"] == dates[B1_BAR + 2].isoformat()


def test_time_exit_needs_limit_or_when(strategy1):
    s = json.loads(json.dumps(strategy1))
    s["exits"] = [{"id": "TIME", "type": "time_exit", "size_pct": 100}]
    s["exit_priority"] = ["TIME"]
    s["params"] = [p for p in s["params"] if not p["path"].startswith("exits[")]
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "exits[0].max_hold_days" for e in errors)


# --------------------------------------------------------------------------------------
# universe.filters / ignored_filters
# --------------------------------------------------------------------------------------


def test_ignored_filters_are_reported(strategy1, tp_store):
    s = json.loads(json.dumps(strategy1))
    s["universe"]["filters"] = {"debt_ratio_max_pct": 200, "market_cap_min_eok": 1}
    s["params"] = list(s["params"]) + [{
        "key": "debt_ratio_max_pct", "label": "부채비율 상한", "group": "1. 종목 선정",
        "path": "universe.filters.debt_ratio_max_pct", "type": "number", "unit": "%",
        "default": 200, "available": False,
        "unavailable_reason": "marcap 에 재무 데이터가 없어 이 조건은 적용되지 않습니다.",
    }]
    ok, errors = validate_strategy(s)
    assert ok, errors

    res = run_backtest(s, tp_store)
    ig = res["assumptions"]["ignored_filters"]
    assert [x["key"] for x in ig] == ["debt_ratio_max_pct"]
    assert "재무 데이터" in ig[0]["reason"]
    assert any("부채비율 상한" in w and "적용하지 않았습니다" in w for w in res["warnings"])
    assert any("부채비율 상한" in n for n in res["assumptions"]["notes"])
    # 지원되는 필터는 무시 목록에 없어야 한다
    assert "market_cap_min_eok" not in [x["key"] for x in ig]


def test_market_cap_filter_applies(strategy1, tp_store):
    s = json.loads(json.dumps(strategy1))
    s["universe"]["filters"] = {"market_cap_min_eok": 100000000}   # 1경원 — 아무것도 안 남는다
    res = run_backtest(s, tp_store)
    assert res["trades"] == []
    assert res["assumptions"]["ignored_filters"] == []


def test_ignored_filters_empty_by_default(strategy1, tp_store):
    assert run_backtest(strategy1, tp_store)["assumptions"]["ignored_filters"] == []


# --------------------------------------------------------------------------------------
# 전략3
# --------------------------------------------------------------------------------------


def test_strategy3_is_registered_and_valid():
    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    ok, errors = validate_strategy(doc)
    assert ok, errors
    assert doc["id"] == "strategy3"
    assert doc["description"] == "52주 신고가 거래대금 폭발 주도주 탐색 및 20일선 지지 반등 매매"
    assert doc["universe"]["reference_day"]["rule"] == "custom"
    assert doc["exit_priority"] == ["SL", "TIME", "TP1", "TP2"]
    assert doc["universe"]["valid_days_after_reference"] == 15


def test_strategy3_defaults_match_spec():
    from engine.params import get_by_path

    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    expect = {
        "universe.filters.market_cap_min_eok": 2000,
        "universe.filters.debt_ratio_max_pct": 200,
        "universe.filters.current_ratio_min_pct": 100,
        "universe.filters.profitable_quarters_min": 4,
        "universe.reference_day.spike_amount_krw_eok": 1000,
        "universe.reference_day.volume_mult": 5,
        "universe.reference_day.close_change_min_pct": 15,
        "universe.reference_day.gap_open_max_pct": 5,
        "indicators[3].period": 252,
        "indicators[0].period": 20,
        "indicators[1].period": 5,
        "indicators[2].period": 20,
        "entries[0].pullback_amount_divisor": 3,
        "universe.valid_days_after_reference": 15,
        "entries[0].size_pct": 100,
        "exits[0].size_pct": 100,
        "exits[1].target_pct": 7,
        "exits[1].size_pct": 50,
        "exits[2].size_pct": 100,
        "exits[3].hold_days": 7,
        "exits[3].pnl_min_pct": 3,
    }
    for path, want in expect.items():
        assert get_by_path(doc, path) == want, path


def test_strategy3_financial_params_require_dart():
    """전략 파일은 정적이고 DART 유무는 실행 환경에 달렸다.
    그러니 available 을 파일에 박지 않고 requires="dart" 로 선언한다."""
    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    fin = {"debt_ratio_max_pct", "current_ratio_min_pct", "profitable_quarters_min", "on_missing"}
    got = {p["key"] for p in doc["params"] if p.get("requires") == "dart"}
    assert got == fin
    for p in doc["params"]:
        if p["key"] in fin - {"on_missing"}:
            assert p.get("available") is not False
            assert "재무" in p["unavailable_reason"]


def test_strategy3_runs_on_synthetic_data(dates):
    """실데이터 없이도 끝까지 돌아가야 한다 (조건이 까다로워 거래는 안 나올 수 있다)."""
    from conftest import FakeStore, make_frame

    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    doc["period"] = {"start": dates[90].isoformat(), "end": "auto"}
    store = FakeStore([make_frame("100010", "테스트에이", "tp", dates),
                       make_frame("100030", "필러", "flat", dates)])
    res = run_backtest(doc, store)
    assert set(res) == RESULT_KEYS
    assert [x["key"] for x in res["assumptions"]["ignored_filters"]] == [
        "debt_ratio_max_pct", "current_ratio_min_pct", "profitable_quarters_min",
    ]
    assert len(res["warnings"]) >= 3


def test_close_entry_cannot_exit_same_bar(strategy1, tp_store, dates):
    """종가에 진입한 봉에서는 청산을 평가하지 않는다 (장이 이미 끝났다)."""
    s = json.loads(json.dumps(strategy1))
    s["entries"] = [{
        "id": "B1", "label": "종가 진입",
        "when": {"op": "<=", "left": "low", "right": "ref.open"},
        "price": "close", "size_pct": 100, "size_of": "planned_position",
    }]
    # 진입한 봉에서 바로 성립하는 청산 규칙
    s["exits"] = [{
        "id": "OUT", "label": "즉시 청산", "type": "signal",
        "when": {"op": ">", "left": "close", "right": 0},
        "price": "close", "size_pct": 100,
    }]
    s["exit_priority"] = ["OUT"]
    s["params"] = [p for p in s["params"]
                   if not p["path"].startswith("exits[") and not p["path"].startswith("entries[1]")]
    ok, errors = validate_strategy(s)
    assert ok, errors

    res = run_backtest(s, tp_store)
    t = res["trades"][0]
    assert t["exit_date"] > t["fills"][0]["date"], "종가 진입 당일에 팔면 안 된다"
    assert t["hold_days"] >= 1
    assert any("종가 진입 봉이라 당일 청산은 평가하지 않음" in sg["message"] for sg in res["signals"])


def test_close_entry_block_is_independent_of_same_day_exit(strategy1, tp_store):
    """same_day_exit=always 로 둬도 종가 진입 봉의 당일 청산은 막힌다."""
    s = json.loads(json.dumps(strategy1))
    s["execution"]["same_day_exit"] = "always"
    s["entries"] = [{
        "id": "B1", "label": "종가 진입",
        "when": {"op": "<=", "left": "low", "right": "ref.open"},
        "price": "close", "size_pct": 100, "size_of": "planned_position",
    }]
    s["exits"] = [{
        "id": "OUT", "label": "즉시 청산", "type": "signal",
        "when": {"op": ">", "left": "close", "right": 0},
        "price": "close", "size_pct": 100,
    }]
    s["exit_priority"] = ["OUT"]
    s["params"] = [p for p in s["params"]
                   if not p["path"].startswith("exits[") and not p["path"].startswith("entries[1]")]
    t = run_backtest(s, tp_store)["trades"][0]
    assert t["exit_date"] > t["fills"][0]["date"]


# ======================================================================================
# ★ DART 재무 필터 연동
# ======================================================================================

DART_KEYS_SET = {"available", "as_of", "coverage_pct", "on_missing", "last_fetch"}

FIN_FILTERS = {
    "debt_ratio_max_pct": 200,
    "current_ratio_min_pct": 100,
    "profitable_quarters_min": 4,
}


def _with_filters(strategy1, **filters):
    s = json.loads(json.dumps(strategy1))
    s.setdefault("universe", {})["filters"] = dict(filters)
    return s


def _good(code):
    from conftest import dart_history

    return dart_history(code, debt_ratio_pct=50.0, current_ratio_pct=250.0,
                        op_income_quarter=1000)


def _bad_debt(code):
    from conftest import dart_history

    return dart_history(code, debt_ratio_pct=500.0, current_ratio_pct=250.0,
                        op_income_quarter=1000)


# ---------------------------------------------------------------- dart 없음 = 기존 동작


def test_dart_none_preserves_existing_behaviour(strategy1, tp_store):
    """dart 를 안 넘기면 결과가 예전과 완전히 같아야 한다."""
    a = run_backtest(strategy1, tp_store)
    b = run_backtest(strategy1, tp_store, dart=None)
    assert a["trades"] == b["trades"]
    assert a["metrics"] == b["metrics"]
    assert a["assumptions"]["dart"] == {
        "available": False, "as_of": False, "coverage_pct": None,
        "on_missing": "include", "last_fetch": None,
    }


def test_unavailable_dart_is_same_as_none(strategy1, tp_store, make_dart):
    s = _with_filters(strategy1, **FIN_FILTERS)
    off = make_dart([], available=False)
    res = run_backtest(s, tp_store, dart=off)
    base = run_backtest(s, tp_store, dart=None)
    assert res["trades"] == base["trades"]
    assert res["assumptions"]["dart"]["available"] is False
    assert {x["key"] for x in res["assumptions"]["ignored_filters"]} == set(FIN_FILTERS)
    assert any("DART 재무 데이터가 연결되지 않아" in x["reason"]
               for x in res["assumptions"]["ignored_filters"])


def test_old_positional_progress_call_still_works(strategy1, tp_store):
    """예전 시그니처 run_backtest(s, store, progress) 로 부르던 코드가 깨지면 안 된다."""
    seen = []
    res = run_backtest(strategy1, tp_store, lambda d, t, m: seen.append(d))
    assert seen and res["trades"]
    assert res["assumptions"]["dart"]["available"] is False


# ---------------------------------------------------------------- 필터가 실제로 걸러낸다


def test_financial_filter_screens_out(strategy1, tp_store, make_dart):
    """부채비율이 기준을 넘는 종목은 후보에서 빠진다."""
    base = run_backtest(strategy1, tp_store)
    assert {t["code"] for t in base["trades"]} == {"100010"}

    s = _with_filters(strategy1, **FIN_FILTERS)
    good = run_backtest(s, tp_store, dart=make_dart(_good("100010")))
    assert {t["code"] for t in good["trades"]} == {"100010"}
    assert good["trades"] == base["trades"], "조건을 통과하면 결과가 그대로여야 한다"

    bad = run_backtest(s, tp_store, dart=make_dart(_bad_debt("100010")))
    assert bad["trades"] == []
    assert bad["assumptions"]["dart"]["available"] is True
    assert bad["assumptions"]["dart"]["as_of"] is True


def test_each_financial_filter_alone(strategy1, tp_store, make_dart):
    from conftest import dart_history

    cases = [
        ("debt_ratio_max_pct", dict(debt_ratio_pct=500.0, current_ratio_pct=250.0, op_income_quarter=1000)),
        ("current_ratio_min_pct", dict(debt_ratio_pct=50.0, current_ratio_pct=10.0, op_income_quarter=1000)),
        ("profitable_quarters_min", dict(debt_ratio_pct=50.0, current_ratio_pct=250.0, op_income_quarter=-5)),
    ]
    for key, fin in cases:
        s = _with_filters(strategy1, **{key: FIN_FILTERS[key]})
        dart = make_dart(dart_history("100010", **fin))
        assert run_backtest(s, tp_store, dart=dart)["trades"] == [], key


def test_financial_filters_removed_from_ignored_when_dart_on(strategy1, tp_store, make_dart):
    s = _with_filters(strategy1, **FIN_FILTERS)
    res = run_backtest(s, tp_store, dart=make_dart(_good("100010")))
    assert res["assumptions"]["ignored_filters"] == []
    assert not any("부채비율" in w for w in res["warnings"])
    assert any("이미 공시된 재무제표만" in n for n in res["assumptions"]["notes"])


def test_dart_block_contents(strategy1, tp_store, make_dart):
    s = _with_filters(strategy1, **FIN_FILTERS)
    d = run_backtest(s, tp_store, dart=make_dart(_good("100010")))["assumptions"]["dart"]
    assert set(d) == DART_KEYS_SET
    assert d == {"available": True, "as_of": True, "coverage_pct": 100.0,
                 "on_missing": "include", "last_fetch": "2026-08-05 14:20:00"}


# ---------------------------------------------------------------- ★ 룩어헤드 차단


def test_no_lookahead_future_disclosure_is_invisible(strategy1, tp_store, dates, make_dart):
    """공시 전 분기의 재무가 쓰이면 이 테스트가 깨진다.

    기준일은 2024-10-21. 그 시점에 공시된 재무는 전부 '탈락' 값이고,
    '통과' 값은 기준일 **이후**에 공시된다. 룩어헤드가 있으면 종목이 통과해 버린다.
    """
    from conftest import dart_row

    ref = dates[REF_BAR]
    assert ref.isoformat() == "2024-10-21"

    rows = [
        # 기준일 전에 공시된 것 — 부채비율 500% (탈락)
        dart_row("100010", 2024, 2, "2024-08-14",
                 debt_ratio_pct=500.0, current_ratio_pct=250.0, op_income_quarter=1000),
        # 기준일 **다음날** 공시 — 부채비율 50% (통과). 기준일에는 보이면 안 된다
        dart_row("100010", 2024, 3, "2024-10-22",
                 debt_ratio_pct=50.0, current_ratio_pct=250.0, op_income_quarter=1000),
    ]
    s = _with_filters(strategy1, debt_ratio_max_pct=200)
    res = run_backtest(s, tp_store, dart=make_dart(rows))
    assert res["trades"] == [], "기준일 이후에 공시된 재무를 미리 본 것이다 (룩어헤드)"


def test_same_data_disclosed_earlier_does_pass(strategy1, tp_store, dates, make_dart):
    """위와 완전히 같은 값인데 공시일만 기준일 **전**으로 당기면 통과해야 한다.

    (그래야 위 테스트가 '항상 탈락' 때문에 통과하는 게 아님이 보장된다)
    """
    from conftest import dart_row

    rows = [
        dart_row("100010", 2024, 2, "2024-08-14",
                 debt_ratio_pct=500.0, current_ratio_pct=250.0, op_income_quarter=1000),
        dart_row("100010", 2024, 3, "2024-10-20",     # 기준일 하루 전 공시
                 debt_ratio_pct=50.0, current_ratio_pct=250.0, op_income_quarter=1000),
    ]
    s = _with_filters(strategy1, debt_ratio_max_pct=200)
    res = run_backtest(s, tp_store, dart=make_dart(rows))
    assert [t["code"] for t in res["trades"]] == ["100010"]


def test_lookahead_guard_on_profit_quarters(strategy1, tp_store, make_dart):
    """연속 흑자 판정도 공시 시점을 지킨다."""
    from conftest import dart_row

    rows = [
        dart_row("100010", 2024, 1, "2024-05-15", debt_ratio_pct=50.0,
                 current_ratio_pct=250.0, op_income_quarter=100),
        dart_row("100010", 2024, 2, "2024-08-14", debt_ratio_pct=50.0,
                 current_ratio_pct=250.0, op_income_quarter=100),
        # 4분기 연속을 채워주는 값들이 기준일 뒤에 공시된다
        dart_row("100010", 2024, 3, "2024-11-14", debt_ratio_pct=50.0,
                 current_ratio_pct=250.0, op_income_quarter=100),
        dart_row("100010", 2024, 4, "2025-03-20", debt_ratio_pct=50.0,
                 current_ratio_pct=250.0, op_income_quarter=100),
    ]
    s = _with_filters(strategy1, profitable_quarters_min=4)
    # 기준일(2024-10-21)에는 2024Q1·Q2 두 분기만 보인다 → 4분기 미달로 탈락
    assert run_backtest(s, tp_store, dart=make_dart(rows))["trades"] == []


# ---------------------------------------------------------------- on_missing


def test_on_missing_defaults_to_include(strategy1, tp_store, make_dart):
    """재무를 모르는 종목은 기본적으로 통과시킨다 (생존 편향 방지)."""
    s = _with_filters(strategy1, **FIN_FILTERS)
    assert s["universe"]["filters"].get("on_missing") is None
    dart = make_dart(_good("999999"))          # 100010 재무가 아예 없다
    res = run_backtest(s, tp_store, dart=dart)
    assert [t["code"] for t in res["trades"]] == ["100010"]
    assert res["assumptions"]["dart"]["on_missing"] == "include"
    assert res["assumptions"]["stats"]["missing_financials"] == 1
    assert res["assumptions"]["stats"]["missing_financials_pct"] == 100.0
    assert res["assumptions"]["dart"]["coverage_pct"] == 0.0
    assert any("재무를 알 수 없는 종목은" in n and "통과시켰습니다" in n
               for n in res["assumptions"]["notes"])


def test_on_missing_exclude_drops_and_warns(strategy1, tp_store, make_dart):
    s = _with_filters(strategy1, on_missing="exclude", **FIN_FILTERS)
    res = run_backtest(s, tp_store, dart=make_dart(_good("999999")))
    assert res["trades"] == []
    assert res["assumptions"]["dart"]["on_missing"] == "exclude"
    assert res["assumptions"]["stats"]["missing_financials"] == 1
    assert any("재무를 알 수 없는" in w and "생존 편향" not in w and "좋게 나올 수 있습니다" in w
               for w in res["warnings"])
    assert any("생존 편향" in n for n in res["assumptions"]["notes"])


def test_definite_failure_beats_unknown(strategy1, tp_store, make_dart):
    """값을 아는 조건에서 이미 탈락이면 on_missing=include 여도 통과시키지 않는다."""
    from conftest import dart_history

    rows = dart_history("100010", debt_ratio_pct=500.0, current_ratio_pct=None,
                        op_income_quarter=1000)
    s = _with_filters(strategy1, debt_ratio_max_pct=200, current_ratio_min_pct=100)
    res = run_backtest(s, tp_store, dart=make_dart(rows))
    assert res["trades"] == []


def test_partial_unknown_is_missing(strategy1, tp_store, make_dart):
    """일부 지표만 결측이어도 '재무 모름' 으로 집계된다."""
    from conftest import dart_history

    rows = dart_history("100010", debt_ratio_pct=50.0, current_ratio_pct=None,
                        op_income_quarter=1000)
    s = _with_filters(strategy1, debt_ratio_max_pct=200, current_ratio_min_pct=100)
    res = run_backtest(s, tp_store, dart=make_dart(rows))
    assert [t["code"] for t in res["trades"]] == ["100010"]      # include 라 통과
    assert res["assumptions"]["stats"]["missing_financials"] == 1


def test_missing_financials_count_across_codes(strategy1, dates, make_dart):
    """여러 종목 중 몇 개가 '재무 모름' 인지 정확히 센다."""
    from conftest import FakeStore, make_frame

    frames = [make_frame("100010", "에이", "tp", dates), make_frame("100050", "비", "tp", dates)]
    store = FakeStore(frames)
    s = _with_filters(strategy1, debt_ratio_max_pct=200)
    res = run_backtest(s, store, dart=make_dart(_good("100010")))   # 100050 만 모름
    st = res["assumptions"]["stats"]
    assert st["missing_financials"] == 1
    assert st["missing_financials_pct"] == 50.0
    assert res["assumptions"]["dart"]["coverage_pct"] == 50.0


def test_on_missing_validation(strategy1):
    s = _with_filters(strategy1, on_missing="maybe", debt_ratio_max_pct=200)
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "universe.filters.on_missing" for e in errors)


def test_filters_must_be_numeric(strategy1):
    s = _with_filters(strategy1, debt_ratio_max_pct="많이")
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "universe.filters.debt_ratio_max_pct" for e in errors)


def test_params_requires_dart_validation(strategy1):
    s = json.loads(json.dumps(strategy1))
    s["params"][0] = dict(s["params"][0], requires="quandl")
    ok, errors = validate_strategy(s)
    assert not ok
    assert any(e["path"] == "params[0].requires" for e in errors)


def test_strategy3_declares_requires_dart():
    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    fin = {"debt_ratio_max_pct", "current_ratio_min_pct", "profitable_quarters_min"}
    got = {p["key"] for p in doc["params"] if p.get("requires") == "dart"} - {"on_missing"}
    assert got == fin
    for p in doc["params"]:
        if p["key"] in fin:
            assert p.get("available") is not False, "파일에 available:false 를 박아두지 않는다"
            assert p.get("unavailable_reason")
    assert doc["universe"]["filters"]["on_missing"] == "include"


# ======================================================================================
# ★ 차트 조회 성능 경로 — bars() 푸시다운 + 종목 캐시
# ======================================================================================


def _write_marcap_years(tmp_path, dates, codes_by_year):
    """연도별 parquet 를 만든다. ``codes_by_year`` 는 ``{year: [(raw_code, name), ...]}``."""
    from conftest import make_frame

    data_dir = tmp_path / "marcap" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for year, entries in codes_by_year.items():
        rows = []
        yd = [d for d in dates if d.year == year]
        if not yd:
            continue
        for raw, name in entries:
            f = make_frame(str(raw).zfill(6), name, "flat", yd)
            f["Code"] = str(raw)          # 파일에는 원본 표기 그대로 (앞자리 0 없이 저장된 옛 파일 재현)
            rows.append(f)
        pd.concat(rows, ignore_index=True).to_parquet(
            data_dir / f"marcap-{year}.parquet", index=False
        )
    return tmp_path / "marcap"


@pytest.fixture
def multiyear_store(tmp_path):
    """2023~2025년 3개 파일. 2023년만 앞자리 0 을 뗀 옛 표기('5930')."""
    import pandas as pd  # noqa: F811

    dates = [d.date() for d in pd.bdate_range("2023-01-02", "2025-12-31")]
    root = _write_marcap_years(tmp_path, dates, {
        2023: [("5930", "삼성전자"), ("660", "SK하이닉스")],       # 옛 표기
        2024: [("005930", "삼성전자"), ("000660", "SK하이닉스")],
        2025: [("005930", "삼성전자"), ("000660", "SK하이닉스")],
    })
    return MarcapStore(root=root, cache_dir=tmp_path / "cache")


def test_code_variants():
    from engine.data import code_variants

    assert code_variants("005930") == ["005930", "05930", "5930"]
    assert code_variants("000660") == ["000660", "00660", "0660", "660"]
    assert code_variants("373220") == ["373220"]
    assert code_variants("5930") == ["005930", "05930", "5930"]


def test_bars_includes_zero_stripped_old_rows(multiyear_store):
    """1995~2000년 파일은 Code 를 '5930' 처럼 저장한다. 이 행이 누락되면 안 된다."""
    df = multiyear_store.bars("005930")
    years = sorted(set(df.index.year))
    assert years == [2023, 2024, 2025], "옛 표기 연도가 빠졌다"
    assert (df["Code"] == "005930").all(), "Code 가 6자리로 정규화돼야 한다"

    naive = multiyear_store.bars("000660")
    assert sorted(set(naive.index.year)) == [2023, 2024, 2025]


def test_bars_matches_full_scan(multiyear_store):
    """푸시다운 결과가 연도 전체를 읽어 거른 것과 완전히 같아야 한다."""
    got = multiyear_store.bars("005930", use_cache=False)

    ref = []
    for y in (2023, 2024, 2025):
        df = multiyear_store.load_year(y)
        ref.append(df[df["Code"] == "005930"])
    want = pd.concat(ref, ignore_index=True).set_index("Date").sort_index()

    assert len(got) == len(want)
    for col in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        np.testing.assert_allclose(
            got[col].to_numpy("float64"), want[col].to_numpy("float64")
        )


def test_bars_does_not_pollute_year_cache(multiyear_store):
    """차트 조회가 백테스트용 연도 캐시를 밀어내면 안 된다."""
    assert multiyear_store._years == {}
    multiyear_store.bars("005930")
    assert multiyear_store._years == {}, "푸시다운 읽기는 _years 에 넣지 않는다"


def test_bars_uses_year_cache_when_already_loaded(multiyear_store):
    """이미 메모리에 올라온 연도가 있으면 그걸 쓴다 (다시 읽지 않는다)."""
    multiyear_store.load_year(2024)
    assert len(multiyear_store._years) == 1
    df = multiyear_store.bars("005930", "2024-01-01", "2024-12-31")
    assert len(df) > 0
    assert len(multiyear_store._years) == 1


def test_symbol_cache_created_and_reused(multiyear_store):
    multiyear_store.bars("005930")
    cache_files = list(multiyear_store.symbol_cache_dir.glob("005930.parquet"))
    assert cache_files, "종목 캐시 파일이 만들어져야 한다"
    assert (multiyear_store.symbol_cache_dir / "005930.meta.json").is_file()

    # 원본을 못 읽게 만들어도 캐시로 답해야 한다
    hidden = multiyear_store.root / "data"
    moved = multiyear_store.root / "data_hidden"
    df_before = multiyear_store.bars("005930")
    hidden.rename(moved)
    try:
        with pytest.raises(DataUnavailable):
            multiyear_store.bars("005930")     # _require() 가 먼저 막는다
    finally:
        moved.rename(hidden)
    assert len(multiyear_store.bars("005930")) == len(df_before)


def test_symbol_cache_invalidated_on_source_change(multiyear_store):
    import os

    first = multiyear_store.bars("005930")
    meta_path = multiyear_store.symbol_cache_dir / "005930.meta.json"
    meta_before = json.loads(meta_path.read_text(encoding="utf-8"))

    newest = multiyear_store.root / "data" / "marcap-2025.parquet"
    os.utime(newest, (0, 0))                  # 원본이 갱신된 것처럼

    again = multiyear_store.bars("005930")
    assert len(again) == len(first)
    meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_after["files"] != meta_before["files"], "지문이 갱신돼야 한다"


def test_symbol_cache_incremental_growth(multiyear_store):
    """범위 조회 → 더 넓은 범위 조회 시 부족한 연도만 읽어 이어붙인다 (점진 로딩)."""
    part = multiyear_store.bars("005930", "2025-01-01", "2025-12-31")
    meta = json.loads((multiyear_store.symbol_cache_dir / "005930.meta.json").read_text(encoding="utf-8"))
    assert meta["years"] == [2025]

    whole = multiyear_store.bars("005930")
    meta = json.loads((multiyear_store.symbol_cache_dir / "005930.meta.json").read_text(encoding="utf-8"))
    assert meta["years"] == [2023, 2024, 2025]
    assert len(whole) > len(part)
    assert sorted(set(whole.index.year)) == [2023, 2024, 2025]

    # 좁은 구간을 다시 물어도 값이 같아야 한다
    again = multiyear_store.bars("005930", "2025-01-01", "2025-12-31")
    assert len(again) == len(part)
    np.testing.assert_allclose(
        again["Close"].to_numpy("float64"), part["Close"].to_numpy("float64")
    )


def test_bars_columns_option(multiyear_store):
    lean = multiyear_store.bars("005930", columns=["Date", "Code", "Close"])
    assert set(lean.columns) == {"Code", "Close"}

    allc = multiyear_store.bars("005930", columns="all", use_cache=False)
    assert {"Marcap", "Dept", "Market"} <= set(allc.columns)

    default = multiyear_store.bars("005930", use_cache=False)
    assert {"Open", "High", "Low", "Close", "Volume", "Amount", "Name"} <= set(default.columns)


def test_bars_range_and_errors(multiyear_store):
    r = multiyear_store.bars("005930", "2024-01-01", "2024-12-31")
    assert set(r.index.year) == {2024}
    with pytest.raises(DataUnavailable):
        multiyear_store.bars("005930", "2030-01-01", "2030-12-31")
    with pytest.raises(DataUnavailable):
        multiyear_store.bars("999999")
    with pytest.raises(DataUnavailable):
        multiyear_store.bars("005930", "2025-01-01", "2024-01-01")


def test_clear_symbol_cache(multiyear_store):
    multiyear_store.bars("005930")
    assert list(multiyear_store.symbol_cache_dir.glob("*.parquet"))
    assert multiyear_store.clear_symbol_cache() > 0
    assert not list(multiyear_store.symbol_cache_dir.glob("*"))
    assert len(multiyear_store.bars("005930")) > 0      # 다시 만들어진다


def test_symbol_cache_trim(multiyear_store):
    from engine.data import MarcapStore as MS

    multiyear_store.bars("005930")
    multiyear_store.bars("000660")
    assert len(list(multiyear_store.symbol_cache_dir.glob("*.parquet"))) == 2
    multiyear_store._trim_symbol_cache(max_bytes=1)
    left = list(multiyear_store.symbol_cache_dir.glob("*.parquet"))
    assert len(left) <= 1, "상한을 넘으면 오래된 것부터 지운다"
