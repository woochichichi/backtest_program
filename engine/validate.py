"""전략 DSL v1 스키마 검증.

    ok, errors = validate_strategy(obj)
    # errors: [{"path": "entries[1].size_pct", "message": "..."}]

외부 의존성(jsonschema) 없이 직접 구현한다 — 오류 경로를 DSL 구조에 맞게
정확히 찍어주기 위해서다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, List, Mapping, Sequence, Tuple

from .errors import PathError
from .indicators import REGISTRY, SOURCE_OPTIONS
from .params import PARAM_TYPES, collect_params, get_by_path

__all__ = ["validate_strategy", "SCHEMA_ID"]

SCHEMA_ID = "krx-backtest-strategy/v1"

_MARKETS = {"KOSPI", "KOSDAQ", "KONEX"}
_EXCLUDES = {"ETF", "ETN", "SPAC", "PREFERRED", "ADMIN_ISSUE", "TRADE_HALT", "REIT", "WARRANT"}
_REF_RULES = {"amount_spike", "volume_spike", "range_breakout", "custom", "none"}
_CONDITIONS = {
    "close_below_reference_open",
    "close_below_reference_low",
    "pullback_pct",
    "none",
}
_SIZE_OF = {"planned_position", "equity", "position"}
_EXIT_TYPES = {"take_profit", "stop_loss", "trailing_stop", "time_exit", "signal"}
_SIZING = {"equal_weight", "fixed_amount", "fixed_qty"}
_FILL_MODELS = {"touch", "next_open", "close"}
_SAME_DAY_EXIT = {"loss_only", "never", "always"}
_RESOLUTIONS = {"1m", "1d"}
_BARS = {"1d"}

_CMP_OPS = {">", ">=", "<", "<=", "==", "!=", "cross_above", "cross_below"}
_LOGIC_OPS = {"and", "or", "not"}

_BAR_FIELDS = {"open", "high", "low", "close", "volume", "amount", "marcap"}
_REF_FIELDS = {"open", "high", "low", "close", "volume", "amount", "marcap"}
_ENTRY_FIELDS = _REF_FIELDS | {"price", "qty"}

#: universe.filters 중 엔진이 실제로 적용할 수 있는 키
SUPPORTED_FILTERS = {
    "market_cap_min_eok", "market_cap_max_eok",
    "price_min", "price_max",
    "amount_min_eok", "volume_min",
}
_POS_FIELDS = {
    "avg_price", "qty", "pnl_pct", "pnl", "hold_days", "cost", "peak_price", "entry_price",
}


class _Errors:
    def __init__(self):
        self.items: List[dict] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"path": path, "message": message})

    def __len__(self):
        return len(self.items)


# --------------------------------------------------------------------------------------
# 원시 검사 헬퍼
# --------------------------------------------------------------------------------------


def _req(obj: Mapping, key: str, path: str, err: _Errors, types=None, allow_none=False):
    if not isinstance(obj, Mapping) or key not in obj:
        err.add(f"{path}.{key}" if path else key, "필수 항목이 없습니다")
        return None
    v = obj[key]
    if v is None and not allow_none:
        err.add(f"{path}.{key}" if path else key, "값이 null 입니다")
        return None
    if types is not None and v is not None and not isinstance(v, types):
        err.add(
            f"{path}.{key}" if path else key,
            f"{_tname(types)} 이어야 합니다 (현재 {type(v).__name__})",
        )
        return None
    return v


def _tname(types) -> str:
    if isinstance(types, tuple):
        return " 또는 ".join(t.__name__ for t in types)
    return types.__name__


def _num(obj: Mapping, key: str, path: str, err: _Errors, *, required=False,
         minimum=None, maximum=None, default=None):
    if key not in obj or obj[key] is None:
        if required:
            err.add(f"{path}.{key}", "필수 항목이 없습니다")
        return default
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        err.add(f"{path}.{key}", f"숫자여야 합니다 (현재 {type(v).__name__})")
        return default
    if minimum is not None and v < minimum:
        err.add(f"{path}.{key}", f"{minimum} 이상이어야 합니다 (현재 {v})")
    if maximum is not None and v > maximum:
        err.add(f"{path}.{key}", f"{maximum} 이하여야 합니다 (현재 {v})")
    return v


def _enum(value, allowed, path: str, err: _Errors, label="값"):
    if value is None:
        return
    if value not in allowed:
        err.add(path, f"허용되지 않은 {label}: {value!r} (가능: {sorted(allowed)})")


def _isdate(v) -> bool:
    if isinstance(v, (dt.date, dt.datetime)):
        return True
    if not isinstance(v, str):
        return False
    try:
        dt.date.fromisoformat(v)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------------------
# 조건식 / 피연산자
# --------------------------------------------------------------------------------------


def _check_operand(op: Any, path: str, err: _Errors, known_ids: set, *,
                   allow_position=True, allow_entry=False):
    if op is None:
        err.add(path, "피연산자가 없습니다")
        return
    if isinstance(op, bool):
        err.add(path, "불리언은 피연산자로 쓸 수 없습니다")
        return
    if isinstance(op, (int, float)):
        return
    if isinstance(op, str):
        _check_reference(op, path, err, known_ids,
                        allow_position=allow_position, allow_entry=allow_entry)
        return
    if isinstance(op, Mapping):
        if "indicator" in op:
            _check_indicator(op, path, err)
            return
        if "expr" in op:
            if not isinstance(op["expr"], str) or not op["expr"].strip():
                err.add(f"{path}.expr", "비어 있지 않은 문자열이어야 합니다")
            return
        if any(k in op for k in ("value", "const", "literal")):
            return
        err.add(path, f"알 수 없는 피연산자 형태입니다: {sorted(op)}")
        return
    err.add(path, f"알 수 없는 피연산자 타입: {type(op).__name__}")


def _check_reference(s: str, path: str, err: _Errors, known_ids: set, *,
                     allow_position=True, allow_entry=False):
    name = s.strip()
    try:
        float(name)
        return
    except ValueError:
        pass
    low = name.lower()
    if low in _BAR_FIELDS:
        return
    if "." not in name:
        # 규칙 레벨 파라미터로 간주 (trigger_pct 등). 존재 여부는 런타임에 확인.
        return
    head, rest = name.split(".", 1)
    head = head.lower()
    if head == "prev":
        _check_reference(rest, path, err, known_ids,
                        allow_position=False, allow_entry=False)
        return
    if head == "entry":
        if not allow_entry:
            err.add(path, "entry.* 는 청산 규칙에서만 쓸 수 있습니다 (진입 전에는 값이 없습니다)")
            return
        if rest.lower() not in _ENTRY_FIELDS:
            err.add(path, f"알 수 없는 진입봉 필드: {name}")
        return
    if head == "ref":
        if rest.lower() not in _REF_FIELDS:
            err.add(path, f"알 수 없는 기준일 필드: {name}")
        return
    if head == "fill":
        rid = rest.split(".")[0]
        if known_ids and rid not in known_ids:
            err.add(path, f"정의되지 않은 규칙 id 를 참조합니다: {rid}")
        return
    if head == "position":
        if not allow_position:
            err.add(path, "진입 규칙에서는 position.* 을 쓸 수 없습니다")
            return
        if rest.lower() not in _POS_FIELDS:
            err.add(path, f"알 수 없는 포지션 필드: {name}")
        return
    err.add(path, f"알 수 없는 참조: {name}")


def _check_indicator(spec: Mapping, path: str, err: _Errors):
    name = spec.get("indicator") or spec.get("type")
    if not isinstance(name, str):
        err.add(f"{path}.indicator", "지표 이름이 문자열이어야 합니다")
        return
    key = name.upper()
    if key not in REGISTRY:
        err.add(f"{path}.indicator", f"알 수 없는 지표: {name} (가능: {sorted(REGISTRY)})")
        return
    reg = REGISTRY[key]
    if reg["fields"]:
        f = spec.get("field")
        if f is None:
            err.add(f"{path}.field", f"{key} 는 field 가 필요합니다 (가능: {reg['fields']})")
        elif str(f).lower() not in reg["fields"]:
            err.add(f"{path}.field", f"{key} 에 없는 서브필드: {f} (가능: {reg['fields']})")
    elif spec.get("field"):
        err.add(f"{path}.field", f"{key} 는 서브필드가 없습니다")
    src = spec.get("source")
    if src is not None and str(src).lower() not in SOURCE_OPTIONS:
        err.add(f"{path}.source", f"알 수 없는 source: {src} (가능: {SOURCE_OPTIONS})")
    for p in reg["params"]:
        if p["name"] in spec and p["type"] == "int":
            v = spec[p["name"]]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                err.add(f"{path}.{p['name']}", f"1 이상의 정수여야 합니다 (현재 {v!r})")


def _check_condition(node: Any, path: str, err: _Errors, known_ids: set,
                     *, allow_position=True, allow_entry=False, depth=0):
    if node is None:
        return
    if depth > 12:
        err.add(path, "조건식 중첩이 너무 깊습니다 (최대 12)")
        return
    if not isinstance(node, Mapping):
        err.add(path, f"조건식은 객체여야 합니다 (현재 {type(node).__name__})")
        return
    op = node.get("op")
    if not isinstance(op, str):
        err.add(f"{path}.op", "op 가 없거나 문자열이 아닙니다")
        return
    o = op.lower()
    if o in ("and", "or"):
        conds = node.get("conditions")
        if not isinstance(conds, Sequence) or isinstance(conds, str) or len(conds) < 1:
            err.add(f"{path}.conditions", f"{o} 는 conditions 배열(1개 이상)이 필요합니다")
            return
        for i, c in enumerate(conds):
            _check_condition(c, f"{path}.conditions[{i}]", err, known_ids,
                             allow_position=allow_position, allow_entry=allow_entry,
                             depth=depth + 1)
        return
    if o == "not":
        inner = node.get("condition")
        if inner is None:
            err.add(f"{path}.condition", "not 은 condition 이 필요합니다")
            return
        _check_condition(inner, f"{path}.condition", err, known_ids,
                         allow_position=allow_position, allow_entry=allow_entry,
                         depth=depth + 1)
        return
    if o not in _CMP_OPS:
        err.add(f"{path}.op", f"알 수 없는 op: {op} (가능: {sorted(_CMP_OPS | _LOGIC_OPS)})")
        return
    if "left" not in node:
        err.add(f"{path}.left", "필수 항목이 없습니다")
    else:
        _check_operand(node["left"], f"{path}.left", err, known_ids,
                      allow_position=allow_position, allow_entry=allow_entry)
    if "right" not in node:
        err.add(f"{path}.right", "필수 항목이 없습니다")
    else:
        _check_operand(node["right"], f"{path}.right", err, known_ids,
                      allow_position=allow_position, allow_entry=allow_entry)


# --------------------------------------------------------------------------------------
# 섹션별 검증
# --------------------------------------------------------------------------------------


def _check_market(obj: Mapping, err: _Errors):
    m = _req(obj, "market", "", err, Mapping)
    if not isinstance(m, Mapping):
        return
    _enum(m.get("bar", "1d"), _BARS, "market.bar", err, "bar")
    _enum(m.get("trade_resolution", "1d"), _RESOLUTIONS, "market.trade_resolution", err, "해상도")
    if "country" in m and not isinstance(m["country"], str):
        err.add("market.country", "문자열이어야 합니다")
    if "asset" in m and not isinstance(m["asset"], str):
        err.add("market.asset", "문자열이어야 합니다")


def _check_universe(obj: Mapping, err: _Errors):
    u = _req(obj, "universe", "", err, Mapping)
    if not isinstance(u, Mapping):
        return
    mk = u.get("markets")
    if mk is None:
        err.add("universe.markets", "필수 항목이 없습니다")
    elif not isinstance(mk, list) or not mk:
        err.add("universe.markets", "1개 이상의 배열이어야 합니다")
    else:
        for i, v in enumerate(mk):
            _enum(v, _MARKETS, f"universe.markets[{i}]", err, "시장")

    ex = u.get("exclude", [])
    if ex is not None:
        if not isinstance(ex, list):
            err.add("universe.exclude", "배열이어야 합니다")
        else:
            for i, v in enumerate(ex):
                _enum(v, _EXCLUDES, f"universe.exclude[{i}]", err, "제외 조건")

    rd = u.get("reference_day")
    if rd is None:
        err.add("universe.reference_day", "필수 항목이 없습니다")
    elif not isinstance(rd, Mapping):
        err.add("universe.reference_day", "객체여야 합니다")
    else:
        rule = rd.get("rule")
        if rule is None:
            err.add("universe.reference_day.rule", "필수 항목이 없습니다")
        else:
            _enum(rule, _REF_RULES, "universe.reference_day.rule", err, "rule")
        _num(rd, "lookback_days", "universe.reference_day", err, minimum=1)
        if rule == "amount_spike":
            _num(rd, "spike_amount_krw_eok", "universe.reference_day", err,
                 required=True, minimum=0)
            _num(rd, "prev_day_amount_max_eok", "universe.reference_day", err, minimum=0)
        elif rule == "volume_spike":
            _num(rd, "volume_mult", "universe.reference_day", err, required=True, minimum=0)
            _num(rd, "volume_ma_period", "universe.reference_day", err, minimum=1)
        elif rule == "range_breakout":
            _num(rd, "breakout_period", "universe.reference_day", err, minimum=1)
        elif rule == "custom":
            if rd.get("when") is None:
                err.add("universe.reference_day.when",
                        'rule="custom" 은 when 조건식이 필요합니다')
            else:
                _check_condition(rd["when"], "universe.reference_day.when", err, set(),
                                 allow_position=False, allow_entry=False)

    filters = u.get("filters")
    if filters is not None and not isinstance(filters, Mapping):
        err.add("universe.filters", "객체여야 합니다")

    cond = u.get("condition", "none")
    _enum(cond, _CONDITIONS, "universe.condition", err, "condition")
    if cond == "pullback_pct":
        _num(u, "pullback_pct", "universe", err, required=True)
    _num(u, "valid_days_after_reference", "universe", err, minimum=1)


def _check_rules(obj: Mapping, err: _Errors) -> set:
    entries = obj.get("entries")
    exits = obj.get("exits")
    if entries is None:
        err.add("entries", "필수 항목이 없습니다")
        entries = []
    elif not isinstance(entries, list) or not entries:
        err.add("entries", "1개 이상의 배열이어야 합니다")
        entries = entries if isinstance(entries, list) else []
    if exits is None:
        err.add("exits", "필수 항목이 없습니다")
        exits = []
    elif not isinstance(exits, list) or not exits:
        err.add("exits", "1개 이상의 배열이어야 합니다")
        exits = exits if isinstance(exits, list) else []

    ids: set = set()
    for kind, arr in (("entries", entries), ("exits", exits)):
        for i, r in enumerate(arr):
            if isinstance(r, Mapping) and isinstance(r.get("id"), str):
                if r["id"] in ids:
                    err.add(f"{kind}[{i}].id", f"중복된 규칙 id: {r['id']}")
                ids.add(r["id"])

    for i, r in enumerate(entries):
        _check_rule(r, f"entries[{i}]", err, ids, is_exit=False)
    for i, r in enumerate(exits):
        _check_rule(r, f"exits[{i}]", err, ids, is_exit=True)

    prio = obj.get("exit_priority")
    if prio is not None:
        if not isinstance(prio, list):
            err.add("exit_priority", "배열이어야 합니다")
        else:
            exit_ids = {r.get("id") for r in exits if isinstance(r, Mapping)}
            for i, v in enumerate(prio):
                if v not in exit_ids:
                    err.add(f"exit_priority[{i}]", f"exits 에 없는 id: {v!r}")
    return ids


def _check_rule(r: Any, path: str, err: _Errors, ids: set, *, is_exit: bool):
    if not isinstance(r, Mapping):
        err.add(path, f"규칙은 객체여야 합니다 (현재 {type(r).__name__})")
        return
    rid = r.get("id")
    if not isinstance(rid, str) or not rid.strip():
        err.add(f"{path}.id", "비어 있지 않은 문자열이어야 합니다")

    req = r.get("requires")
    if req is not None:
        if not isinstance(req, list):
            err.add(f"{path}.requires", "배열이어야 합니다")
        else:
            for i, v in enumerate(req):
                if v not in ids:
                    err.add(f"{path}.requires[{i}]", f"정의되지 않은 규칙 id: {v!r}")
                if v == rid:
                    err.add(f"{path}.requires[{i}]", "자기 자신을 requires 할 수 없습니다")

    if is_exit:
        t = r.get("type")
        if t is not None:
            _enum(t, _EXIT_TYPES, f"{path}.type", err, "청산 타입")
        if t == "trailing_stop" and "trail_pct" not in r and r.get("when") is None:
            err.add(f"{path}.trail_pct", "trailing_stop 은 trail_pct 또는 when 이 필요합니다")
        if (t == "time_exit" and r.get("when") is None
                and "max_hold_days" not in r and "hold_days" not in r):
            err.add(f"{path}.max_hold_days",
                    "time_exit 은 max_hold_days(또는 hold_days) 또는 when 이 필요합니다")

    when = r.get("when")
    if when is None:
        if not is_exit:
            err.add(f"{path}.when", "진입 규칙에는 when 이 필요합니다")
        elif r.get("type") not in ("trailing_stop", "time_exit"):
            err.add(f"{path}.when", "청산 규칙에는 when 또는 type(trailing_stop/time_exit)이 필요합니다")
    else:
        _check_condition(when, f"{path}.when", err, ids,
                         allow_position=is_exit, allow_entry=is_exit)

    if "price" in r and r["price"] is not None:
        _check_operand(r["price"], f"{path}.price", err, ids,
                      allow_position=is_exit, allow_entry=is_exit)

    _num(r, "size_pct", path, err, minimum=0, maximum=100)
    so = r.get("size_of")
    if so is not None:
        _enum(so, _SIZE_OF, f"{path}.size_of", err, "size_of")
        if not is_exit and so == "position":
            err.add(f"{path}.size_of", "진입 규칙에는 size_of=position 을 쓸 수 없습니다")


def _check_portfolio(obj: Mapping, err: _Errors):
    p = _req(obj, "portfolio", "", err, Mapping)
    if not isinstance(p, Mapping):
        return
    if "initial_capital_manwon" not in p and "initial_capital" not in p:
        err.add("portfolio.initial_capital_manwon", "필수 항목이 없습니다")
    else:
        key = "initial_capital_manwon" if "initial_capital_manwon" in p else "initial_capital"
        _num(p, key, "portfolio", err, minimum=1)
    _num(p, "max_positions", "portfolio", err, minimum=1)
    sizing = p.get("position_sizing", "equal_weight")
    _enum(sizing, _SIZING, "portfolio.position_sizing", err, "position_sizing")
    if sizing == "fixed_amount":
        _num(p, "amount_manwon", "portfolio", err, required=True, minimum=1)
    if sizing == "fixed_qty":
        _num(p, "qty", "portfolio", err, required=True, minimum=1)
    if "allow_duplicate_symbol" in p and not isinstance(p["allow_duplicate_symbol"], bool):
        err.add("portfolio.allow_duplicate_symbol", "불리언이어야 합니다")


def _check_execution(obj: Mapping, err: _Errors):
    e = _req(obj, "execution", "", err, Mapping)
    if not isinstance(e, Mapping):
        return
    _enum(e.get("resolution", "1d"), _RESOLUTIONS, "execution.resolution", err, "해상도")
    _enum(e.get("fill_model", "touch"), _FILL_MODELS, "execution.fill_model", err, "fill_model")
    _enum(e.get("same_day_exit", "loss_only"), _SAME_DAY_EXIT,
          "execution.same_day_exit", err, "same_day_exit")
    _num(e, "slippage_pct", "execution", err, minimum=0, maximum=100)
    _num(e, "fee_pct", "execution", err, minimum=0, maximum=100)


def _check_period(obj: Mapping, err: _Errors):
    p = _req(obj, "period", "", err, Mapping)
    if not isinstance(p, Mapping):
        return
    s = p.get("start")
    if s is None:
        err.add("period.start", "필수 항목이 없습니다")
    elif not _isdate(s):
        err.add("period.start", f"YYYY-MM-DD 형식이어야 합니다 (현재 {s!r})")
    e = p.get("end", "auto")
    if e not in (None, "auto", "") and not _isdate(e):
        err.add("period.end", f"YYYY-MM-DD 또는 'auto' 여야 합니다 (현재 {e!r})")
    if _isdate(s) and _isdate(e):
        if dt.date.fromisoformat(str(e)) < dt.date.fromisoformat(str(s)):
            err.add("period.end", "start 보다 빠를 수 없습니다")


def _check_indicators(obj: Mapping, err: _Errors):
    inds = obj.get("indicators")
    if inds is None:
        return
    if not isinstance(inds, list):
        err.add("indicators", "배열이어야 합니다")
        return
    for i, spec in enumerate(inds):
        path = f"indicators[{i}]"
        if not isinstance(spec, Mapping):
            err.add(path, "객체여야 합니다")
            continue
        t = spec.get("type") or spec.get("indicator")
        if not isinstance(t, str):
            err.add(f"{path}.type", "지표 type 이 필요합니다")
            continue
        if t.upper() not in REGISTRY:
            err.add(f"{path}.type", f"알 수 없는 지표: {t} (가능: {sorted(REGISTRY)})")
            continue
        reg = REGISTRY[t.upper()]
        if spec.get("source") is not None and str(spec["source"]).lower() not in SOURCE_OPTIONS:
            err.add(f"{path}.source", f"알 수 없는 source: {spec['source']}")
        for p in reg["params"]:
            if p["name"] in spec and p["type"] == "int":
                v = spec[p["name"]]
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    err.add(f"{path}.{p['name']}", f"1 이상의 정수여야 합니다 (현재 {v!r})")
        if "plot" in spec and not isinstance(spec["plot"], bool):
            err.add(f"{path}.plot", "불리언이어야 합니다")


# --------------------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------------------


def _check_params(obj: Mapping, err: _Errors):
    """``params`` 는 UI 메타데이터다. 여기서는 형식과 **path 존재 여부**만 본다."""
    raw = obj.get("params")
    if raw is None:
        return
    if not isinstance(raw, list):
        err.add("params", "배열이어야 합니다")
        return
    seen: set = set()
    for i, p in enumerate(raw):
        path = f"params[{i}]"
        if not isinstance(p, Mapping):
            err.add(path, f"객체여야 합니다 (현재 {type(p).__name__})")
            continue
        for key in ("key", "label", "group", "path", "type"):
            v = p.get(key)
            if not isinstance(v, str) or not v.strip():
                err.add(f"{path}.{key}", "비어 있지 않은 문자열이어야 합니다")
        if isinstance(p.get("key"), str):
            if p["key"] in seen:
                err.add(f"{path}.key", f"중복된 key: {p['key']}")
            seen.add(p["key"])
        if isinstance(p.get("type"), str):
            _enum(p["type"], set(PARAM_TYPES), f"{path}.type", err, "type")
        if "default" not in p:
            err.add(f"{path}.default", "필수 항목이 없습니다 (초기화 버튼이 되돌릴 값)")
        if "available" in p and not isinstance(p["available"], bool):
            err.add(f"{path}.available", "불리언이어야 합니다")
        if p.get("available") is False and not p.get("unavailable_reason"):
            err.add(f"{path}.unavailable_reason",
                    "available:false 항목은 사용자에게 보여줄 이유가 필요합니다")
        if p.get("type") == "select" and not isinstance(p.get("options"), list):
            err.add(f"{path}.options", "type=select 는 options 배열이 필요합니다")
        for k in ("min", "max", "step"):
            if k in p and p[k] is not None and (
                isinstance(p[k], bool) or not isinstance(p[k], (int, float))
            ):
                err.add(f"{path}.{k}", "숫자여야 합니다")

        raw_path = p.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            try:
                get_by_path(obj, raw_path)
            except PathError as e:
                err.add(f"{path}.path", str(e))


def validate_strategy(obj: Any) -> Tuple[bool, List[dict]]:
    """전략 DSL 을 검증한다.

    Returns
    -------
    (ok, errors) : (bool, list[dict])
        ``errors`` 는 ``[{"path": "entries[1].size_pct", "message": "..."}]``.
    """
    err = _Errors()
    if not isinstance(obj, Mapping):
        err.add("", f"전략은 JSON 객체여야 합니다 (현재 {type(obj).__name__})")
        return False, err.items

    schema = obj.get("schema")
    if schema is None:
        err.add("schema", "필수 항목이 없습니다")
    elif schema != SCHEMA_ID:
        err.add("schema", f"고정값 {SCHEMA_ID!r} 이어야 합니다 (현재 {schema!r})")

    sid = obj.get("id")
    if not isinstance(sid, str) or not sid.strip():
        err.add("id", "비어 있지 않은 슬러그 문자열이어야 합니다")
    elif not all(c.isalnum() or c in "-_" for c in sid):
        err.add("id", "영문/숫자/- /_ 만 쓸 수 있습니다")

    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        err.add("name", "비어 있지 않은 문자열이어야 합니다")

    if "description" in obj and obj["description"] is not None and not isinstance(obj["description"], str):
        err.add("description", "문자열이어야 합니다")
    if "enabled" in obj and not isinstance(obj["enabled"], bool):
        err.add("enabled", "불리언이어야 합니다")

    _check_market(obj, err)
    _check_universe(obj, err)
    _check_params(obj, err)
    _check_rules(obj, err)
    _check_indicators(obj, err)
    _check_portfolio(obj, err)
    _check_execution(obj, err)
    _check_period(obj, err)

    return (len(err) == 0), err.items
