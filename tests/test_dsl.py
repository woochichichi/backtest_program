"""engine/dsl.py — DSL v1 조건식 / 안전 수식 파서."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from engine.dsl import EvalContext, evaluate_condition, evaluate_operand, safe_eval_expr
from engine.errors import DSLError


@pytest.fixture
def df():
    return pd.DataFrame(
        {
            "Open": [100.0, 102.0, 101.0, 99.0, 105.0],
            "High": [103.0, 104.0, 102.0, 101.0, 108.0],
            "Low": [99.0, 100.0, 97.0, 95.0, 104.0],
            "Close": [102.0, 101.0, 98.0, 100.0, 107.0],
            "Volume": [10.0, 20.0, 30.0, 40.0, 50.0],
            "Amount": [1e9, 2e9, 3e9, 4e9, 5e9],
        },
        index=pd.to_datetime(
            ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
        ),
    )


@pytest.fixture
def ctx(df, strategy1_json):
    from engine.backtest import _indicator_aliases

    return EvalContext(
        df,
        aliases=_indicator_aliases(strategy1_json),
        ref={"open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "amount": 1.2e11},
        fills={"B1": {"price": 100.0, "qty": 10, "date": dt.date(2026, 1, 5)}},
        position={
            "avg_price": 98.0, "qty": 20, "pnl_pct": 5.0, "pnl": 100.0,
            "hold_days": 7, "cost": 1960.0, "peak_price": 108.0, "entry_price": 100.0,
        },
        params={"trigger_pct": -10, "target_pct": 10, "ma_period": 45},
    )


# ---------------------------------------------------------------- 피연산자


def test_bar_fields_are_vectors(ctx):
    np.testing.assert_allclose(evaluate_operand("close", ctx), [102, 101, 98, 100, 107])
    assert evaluate_operand("low", ctx, 2) == 97.0


def test_ref_fill_position_are_scalars(ctx):
    assert evaluate_operand("ref.open", ctx) == 100.0
    assert evaluate_operand("fill.B1.price", ctx) == 100.0
    assert evaluate_operand("position.avg_price", ctx) == 98.0
    assert evaluate_operand("position.hold_days", ctx) == 7.0


def test_literal_operands(ctx):
    assert evaluate_operand(10, ctx) == 10.0
    assert evaluate_operand("10.5", ctx) == 10.5
    assert evaluate_operand({"value": 3}, ctx) == 3.0


def test_indicator_operand(ctx):
    sma = evaluate_operand({"indicator": "SMA", "period": 2, "source": "close"}, ctx)
    assert isinstance(sma, np.ndarray)
    np.testing.assert_allclose(sma[1], 101.5)


def test_indicator_subfield_required(ctx):
    with pytest.raises(DSLError):
        evaluate_operand({"indicator": "BBANDS", "period": 3}, ctx)
    v = evaluate_operand({"indicator": "BBANDS", "period": 3, "field": "lower"}, ctx)
    assert isinstance(v, np.ndarray)


def test_unknown_reference_raises(ctx):
    with pytest.raises(DSLError):
        evaluate_operand("bogus.field", ctx)
    with pytest.raises(DSLError):
        evaluate_operand("ref.nope", ctx)


def test_missing_fill_raises(ctx):
    with pytest.raises(DSLError):
        evaluate_operand("fill.B9.price", ctx)


# ---------------------------------------------------------------- 비교 연산


@pytest.mark.parametrize("op,expected", [
    ("<=", [True, False, True, True, False]),
    ("<", [True, False, True, True, False]),
    (">", [False, True, False, False, True]),
    (">=", [False, True, False, False, True]),
])
def test_comparison_vector(ctx, op, expected):
    node = {"op": op, "left": "low", "right": 99.5}
    got = evaluate_condition(node, ctx)
    assert got.dtype == bool
    assert list(got) == expected


def test_comparison_scalar_matches_vector(ctx):
    node = {"op": "<=", "left": "low", "right": "ref.open"}
    vec = evaluate_condition(node, ctx)
    for i in range(len(vec)):
        assert evaluate_condition(node, ctx, i) == bool(vec[i])


def test_equality_ops(ctx):
    assert evaluate_condition({"op": "==", "left": "close", "right": 98}, ctx, 2) is True
    assert evaluate_condition({"op": "!=", "left": "close", "right": 98}, ctx, 2) is False


def test_nan_is_false(ctx):
    node = {"op": ">", "left": {"indicator": "SMA", "period": 4}, "right": 0}
    vec = evaluate_condition(node, ctx)
    assert list(vec[:3]) == [False, False, False]
    assert vec[3] is np.True_ or bool(vec[3])


# ---------------------------------------------------------------- 논리 / 크로스


def test_and_or_not(ctx):
    a = {"op": ">", "left": "close", "right": 99}
    b = {"op": "<", "left": "close", "right": 103}
    assert list(evaluate_condition({"op": "and", "conditions": [a, b]}, ctx)) == [
        True, True, False, True, False
    ]
    assert list(evaluate_condition({"op": "or", "conditions": [a, b]}, ctx)) == [
        True, True, True, True, True
    ]
    assert list(evaluate_condition({"op": "not", "condition": a}, ctx)) == [
        False, False, True, False, False
    ]


def test_and_scalar_mode(ctx):
    a = {"op": ">", "left": "close", "right": 99}
    b = {"op": "<", "left": "close", "right": 103}
    node = {"op": "and", "conditions": [a, b]}
    assert evaluate_condition(node, ctx, 0) is True
    assert evaluate_condition(node, ctx, 2) is False


def test_cross_above_below(ctx):
    node = {"op": "cross_above", "left": "close", "right": 100.5}
    vec = evaluate_condition(node, ctx)
    # close: 102 101 98 100 107 → 100.5 상향돌파는 index 4
    assert list(vec) == [False, False, False, False, True]
    assert evaluate_condition(node, ctx, 4) is True
    assert evaluate_condition(node, ctx, 0) is False

    down = {"op": "cross_below", "left": "close", "right": 100.5}
    assert list(evaluate_condition(down, ctx)) == [False, False, True, False, False]


def test_missing_op_raises(ctx):
    with pytest.raises(DSLError):
        evaluate_condition({"left": "close", "right": 1}, ctx)
    with pytest.raises(DSLError):
        evaluate_condition({"op": "wat", "left": "close", "right": 1}, ctx)


# ---------------------------------------------------------------- 수식 (expr)


def test_expr_arithmetic(ctx):
    assert safe_eval_expr("fill.B1.price * (1 + trigger_pct / 100)", ctx) == pytest.approx(90.0)
    assert safe_eval_expr("position.avg_price * (1 + target_pct / 100)", ctx) == pytest.approx(107.8)
    assert safe_eval_expr("ref.open + 1 - 2 * 3", ctx) == pytest.approx(95.0)
    assert safe_eval_expr("-ref.open", ctx) == pytest.approx(-100.0)
    assert safe_eval_expr("2 ** 3", ctx) == pytest.approx(8.0)


def test_expr_with_vector_operand(ctx):
    v = safe_eval_expr("close * 2", ctx)
    assert isinstance(v, np.ndarray)
    np.testing.assert_allclose(v, [204, 202, 196, 200, 214])
    assert evaluate_operand({"expr": "close * 2"}, ctx, 1) == 202.0


def test_expr_division_by_zero_is_nan(ctx):
    assert np.isnan(safe_eval_expr("ref.open / 0", ctx))


@pytest.mark.parametrize("bad", [
    "__import__('os').system('ls')",
    "open('/etc/passwd')",
    "close.__class__",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "eval('1+1')",
    "close if close else 1",
    "f'{close}'",
    "close and 1",
])
def test_expr_rejects_unsafe(ctx, bad):
    with pytest.raises(DSLError):
        safe_eval_expr(bad, ctx)


def test_expr_syntax_error(ctx):
    with pytest.raises(DSLError):
        safe_eval_expr("1 +", ctx)


def test_expr_unknown_name(ctx):
    with pytest.raises(DSLError):
        safe_eval_expr("nope * 2", ctx)


def test_no_builtin_leaks(ctx):
    """eval() 을 안 쓰므로 내장 이름은 전부 미정의여야 한다."""
    for name in ("abs", "min", "max", "len", "sum", "print"):
        with pytest.raises(DSLError):
            safe_eval_expr(name, ctx)


# ---------------------------------------------------------------- 전략1 조건식 그대로


def test_strategy1_conditions(ctx, strategy1_json):
    entries = {e["id"]: e for e in strategy1_json["entries"]}
    exits = {e["id"]: e for e in strategy1_json["exits"]}

    # B1 : low <= ref.open (100.0)   — low: 99 100 97 95 104
    assert list(evaluate_condition(entries["B1"]["when"], ctx)) == [
        True, True, True, True, False
    ]

    # B2 : low <= fill.B1.price * (1 + -10/100) = 90
    ctx.params = dict(entries["B2"])
    assert not any(evaluate_condition(entries["B2"]["when"], ctx))

    # TP : high >= position.avg_price * 1.1 = 107.8
    ctx.params = dict(exits["TP"])
    assert list(evaluate_condition(exits["TP"]["when"], ctx)) == [
        False, False, False, False, True
    ]

    # SL : low <= SMA45  (5봉짜리 프레임이라 전부 NaN → False)
    ctx.params = dict(exits["SL"])
    assert not any(evaluate_condition(exits["SL"]["when"], ctx))


def test_price_operands_of_strategy1(ctx, strategy1_json):
    entries = {e["id"]: e for e in strategy1_json["entries"]}
    exits = {e["id"]: e for e in strategy1_json["exits"]}
    assert evaluate_operand(entries["B1"]["price"], ctx) == 100.0
    ctx.params = dict(entries["B2"])
    assert evaluate_operand(entries["B2"]["price"], ctx) == pytest.approx(90.0)
    ctx.params = dict(exits["TP"])
    assert evaluate_operand(exits["TP"]["price"], ctx) == pytest.approx(107.8)


def test_none_condition_is_always_true(ctx):
    assert evaluate_condition(None, ctx, 0) is True
    assert evaluate_condition(None, ctx).all()
