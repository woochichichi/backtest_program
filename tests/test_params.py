"""engine/params.py — ``params`` 선언 처리 (ARCHITECTURE-v2 §1)."""

from __future__ import annotations

import copy
import json

import pytest

from conftest import ROOT
from engine.errors import PathError
from engine.params import (
    collect_params,
    format_path,
    get_by_path,
    params_snapshot,
    parse_path,
    path_exists,
    reset_to_defaults,
    set_by_path,
)
from engine.validate import validate_strategy

ALL_STRATEGIES = sorted((ROOT / "strategies").glob("*.json"))


# ---------------------------------------------------------------- 경로 파싱


@pytest.mark.parametrize("path,parts", [
    ("entries[0].size_pct", ["entries", 0, "size_pct"]),
    ("universe.filters.market_cap_min_eok", ["universe", "filters", "market_cap_min_eok"]),
    ("indicators[4].fast", ["indicators", 4, "fast"]),
    ("a[0][1].b", ["a", 0, 1, "b"]),
    ("top", ["top"]),
])
def test_parse_and_format_roundtrip(path, parts):
    assert parse_path(path) == parts
    assert format_path(parts) == path


@pytest.mark.parametrize("bad", ["", "   ", "a..b", "a[x]", "[0]", "a.", ".a", "a b", None, 3])
def test_parse_path_rejects_garbage(bad):
    with pytest.raises(PathError):
        parse_path(bad)


# ---------------------------------------------------------------- get / set


@pytest.fixture
def doc():
    return {
        "universe": {"filters": {"market_cap_min_eok": 2000}, "valid_days_after_reference": 15},
        "entries": [{"id": "B1", "size_pct": 100}],
        "indicators": [{"key": "MA20", "period": 20}],
    }


def test_get_by_path(doc):
    assert get_by_path(doc, "entries[0].size_pct") == 100
    assert get_by_path(doc, "universe.filters.market_cap_min_eok") == 2000
    assert get_by_path(doc, "indicators[0].key") == "MA20"


def test_get_by_path_missing(doc):
    for p in ("universe.foo", "entries[5].size_pct", "entries[0].nope", "universe.filters.x.y"):
        assert path_exists(doc, p) is False
        with pytest.raises(PathError):
            get_by_path(doc, p)
        assert get_by_path(doc, p, default="D") == "D"


def test_missing_path_message_is_actionable(doc):
    with pytest.raises(PathError) as ei:
        get_by_path(doc, "universe.foo")
    assert "가리키는 위치가 없습니다" in str(ei.value)
    assert "universe.foo" in str(ei.value)


def test_set_by_path(doc):
    set_by_path(doc, "entries[0].size_pct", 50)
    assert doc["entries"][0]["size_pct"] == 50
    set_by_path(doc, "universe.filters.market_cap_min_eok", 5000)
    assert doc["universe"]["filters"]["market_cap_min_eok"] == 5000


def test_set_by_path_creates_missing_dicts(doc):
    set_by_path(doc, "execution.slippage_pct", 0.1)
    assert doc["execution"]["slippage_pct"] == 0.1


def test_set_by_path_does_not_grow_lists(doc):
    with pytest.raises(PathError):
        set_by_path(doc, "entries[3].size_pct", 1)
    with pytest.raises(PathError):
        set_by_path(doc, "nope[0].x", 1)


# ---------------------------------------------------------------- reset_to_defaults


def test_reset_to_defaults_restores_every_param():
    src = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    edited = copy.deepcopy(src)
    for p in collect_params(edited):
        cur = get_by_path(edited, p["path"])
        set_by_path(edited, p["path"], (cur or 0) + 999 if isinstance(cur, (int, float)) else "XXX")
    assert edited != src

    back = reset_to_defaults(edited)
    for p in collect_params(back):
        assert get_by_path(back, p["path"]) == p["default"], p["key"]


def test_reset_to_defaults_is_pure():
    src = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    edited = copy.deepcopy(src)
    set_by_path(edited, "entries[0].size_pct", 13)
    snapshot = copy.deepcopy(edited)
    out = reset_to_defaults(edited)
    assert edited == snapshot, "원본 문서를 건드리면 안 된다"
    assert out is not edited
    assert get_by_path(out, "entries[0].size_pct") == 100


def test_reset_to_defaults_never_touches_params_array():
    src = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    out = reset_to_defaults(src)
    assert out["params"] == src["params"]


def test_reset_includes_unavailable_params():
    src = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    edited = copy.deepcopy(src)
    set_by_path(edited, "universe.filters.debt_ratio_max_pct", 9999)
    out = reset_to_defaults(edited)
    assert get_by_path(out, "universe.filters.debt_ratio_max_pct") == 200


def test_reset_skips_unknown_paths_quietly():
    doc = {"params": [{"key": "x", "path": "nope.here", "default": 1}], "a": 1}
    assert reset_to_defaults(doc)["a"] == 1


# ---------------------------------------------------------------- 등록된 전략


@pytest.mark.parametrize("path", ALL_STRATEGIES, ids=lambda p: p.stem)
def test_registered_strategy_params_resolve(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    ok, errors = validate_strategy(doc)
    assert ok, errors
    snap = params_snapshot(doc)
    assert snap, f"{path.stem} 에 params 선언이 없습니다"
    for row in snap:
        assert row["resolved"], f"{row['key']} → {row['path']} 가 문서에 없습니다"
        assert row["is_default"], (
            f"{row['key']}: 파일의 현재 값 {row['value']!r} 과 default {row['default']!r} 이 다릅니다"
        )


@pytest.mark.parametrize("path", ALL_STRATEGIES, ids=lambda p: p.stem)
def test_registered_strategy_param_groups_and_keys(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    params = collect_params(doc)
    keys = [p["key"] for p in params]
    assert len(keys) == len(set(keys)), "key 중복"
    assert all(p.get("group") for p in params)
    for p in params:
        if p.get("available") is False:
            assert p.get("unavailable_reason")


# ---------------------------------------------------------------- validate 연동


def test_validate_reports_missing_param_path():
    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    doc["params"][3]["path"] = "universe.foo"
    ok, errors = validate_strategy(doc)
    assert not ok
    hit = [e for e in errors if e["path"] == "params[3].path"]
    assert hit and "가리키는 위치가 없습니다: universe.foo" in hit[0]["message"]


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["params"][0].pop("key"), "params[0].key"),
    (lambda d: d["params"][0].pop("group"), "params[0].group"),
    (lambda d: d["params"][0].pop("default"), "params[0].default"),
    (lambda d: d["params"][0].update(type="colour"), "params[0].type"),
    (lambda d: d["params"][1].update(available=False, unavailable_reason=None),
     "params[1].unavailable_reason"),
    (lambda d: d["params"][0].update(key=d["params"][1]["key"]), "params[1].key"),
    (lambda d: d.update(params={"not": "a list"}), "params"),
])
def test_validate_param_shape(mutate, expect):
    doc = json.loads((ROOT / "strategies" / "strategy3.json").read_text(encoding="utf-8"))
    mutate(doc)
    ok, errors = validate_strategy(doc)
    assert not ok
    assert any(e["path"] == expect for e in errors), (expect, errors[:5])


def test_params_is_optional():
    doc = json.loads((ROOT / "strategies" / "strategy1.json").read_text(encoding="utf-8"))
    doc.pop("params")
    ok, errors = validate_strategy(doc)
    assert ok, errors


def test_engine_ignores_params(strategy1, tp_store):
    """params 는 UI 메타데이터다 — 있으나 없으나 백테스트 결과가 같아야 한다."""
    from engine.backtest import run_backtest

    with_params = run_backtest(strategy1, tp_store)
    bare = copy.deepcopy(strategy1)
    bare.pop("params", None)
    without = run_backtest(bare, tp_store)
    assert with_params["trades"] == without["trades"]
    assert with_params["metrics"] == without["metrics"]
