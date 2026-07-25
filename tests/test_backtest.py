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
}
ASSUMPTION_STAT_KEYS = {
    "same_day_entry_exit", "same_day_entry_exit_pct", "ambiguous_bars",
}
METRIC_KEYS = {
    "total_return_pct", "cagr_pct", "mdd_pct", "sharpe", "win_rate_pct",
    "profit_factor", "trades", "wins", "losses", "avg_win_pct", "avg_loss_pct",
    "avg_hold_days", "initial_capital", "final_capital", "period",
}
TRADE_KEYS = {
    "no", "code", "name", "ref_date", "ref_amount_eok", "ref_open", "fills",
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
        ("loss_only", "손절 계열만 평가"),
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


def test_untyped_exit_rule_is_treated_as_loss_side(strategy1, dates):
    """type 이 없는 커스텀 청산은 loss_only 에서 당일 평가를 허용한다 (보수적)."""
    s = _mode(strategy1, "loss_only")
    for r in s["exits"]:
        if r["id"] == "TP":
            r.pop("type")
    res = run_backtest(s, _same_day_tp_store(dates))
    assert res["trades"][0]["exit_date"] == dates[B2_BAR].isoformat()


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
