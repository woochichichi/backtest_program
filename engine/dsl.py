"""DSL v1 조건식 / 수식 평가기.

SCHEMA.md "``when`` 조건식" 절의 구현.

* op            : ``> >= < <= == != and or not cross_above cross_below``
* 피연산자      : 봉 필드 / ``ref.*`` / ``fill.<id>.price`` / ``position.*`` /
                  ``{"indicator": ...}`` / ``{"expr": "..."}`` / 리터럴
* 평가 모드     : 벡터(``i=None``, 시계열 전체 bool 배열) 와 스칼라(``i=<int>``) 둘 다 지원

``expr`` 은 ``eval()`` 을 쓰지 않는다. ``ast`` 로 파싱하고 화이트리스트 노드만 통과시킨다.
함수 호출(``Call``)·람다·컴프리헨션·문자열 포매팅 등은 모두 거부한다.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Dict, Iterable, Mapping

import numpy as np
import pandas as pd

from .errors import DSLError
from .indicators import compute, indicator_key, raw_column as _raw_column, source_series

__all__ = [
    "EvalContext",
    "evaluate_condition",
    "evaluate_operand",
    "eval_expr",
    "safe_eval_expr",
    "BAR_FIELDS",
    "COMPARISON_OPS",
    "LOGICAL_OPS",
]

BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount", "marcap")
COMPARISON_OPS = (">", ">=", "<", "<=", "==", "!=", "cross_above", "cross_below")
LOGICAL_OPS = ("and", "or", "not")

#: ``ref.*`` / ``entry.*`` 로 참조 가능한 봉 필드 (ARCHITECTURE-v2 §3-2)
BAR_REF_FIELDS = ("open", "high", "low", "close", "volume", "amount", "marcap", "date")

_REF_FIELDS = BAR_REF_FIELDS
_ENTRY_FIELDS = BAR_REF_FIELDS + ("price", "qty")
_POSITION_FIELDS = (
    "avg_price",
    "qty",
    "pnl_pct",
    "pnl",
    "hold_days",
    "cost",
    "peak_price",
    "entry_price",
)


# ======================================================================================
# 평가 컨텍스트
# ======================================================================================


class EvalContext:
    """조건식 평가에 필요한 모든 값을 들고 있는 객체.

    Parameters
    ----------
    df : pd.DataFrame
        종목의 일봉 (index=Date). 컬럼은 대/소문자 아무거나 (Open/open 둘 다 인식).
    ref : dict | None
        기준일 봉 값. ``{"open":..., "high":..., ...}``
    fills : dict | None
        ``{"B1": {"price": 4000.0, "qty": 100, "date": date(...)}, ...}``
    position : dict | None
        ``{"avg_price":..., "qty":..., "pnl_pct":..., "hold_days":...}``
    params : dict | None
        규칙 레벨 스칼라 (``trigger_pct``, ``target_pct``, ``ma_period`` 등).
    indicator_cache : dict | None
        같은 종목 안에서 지표 계산 결과를 재사용하기 위한 캐시. 종목별로 하나 만들어 넘긴다.
    """

    __slots__ = (
        "df", "ref", "fills", "position", "params", "_ind", "_bars", "n",
        "extra", "aliases", "entry",
    )

    def __init__(
        self,
        df: pd.DataFrame,
        ref: Mapping[str, Any] | None = None,
        fills: Mapping[str, Any] | None = None,
        position: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        indicator_cache: Dict[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        aliases: Mapping[str, Any] | None = None,
        entry: Mapping[str, Any] | None = None,
    ):
        self.df = df
        self.ref = dict(ref or {})
        self.fills = dict(fills or {})
        self.position = dict(position or {})
        self.params = dict(params or {})
        self._ind = indicator_cache if indicator_cache is not None else {}
        self._bars: Dict[str, np.ndarray] = {}
        self.extra = dict(extra or {})
        #: strategy["indicators"] 의 ``key`` → 지표 스펙. 조건식/수식에서 이름으로 쓴다.
        self.aliases = dict(aliases or {})
        #: 진입이 체결된 봉의 값 (``entry.*``). 청산 규칙에서만 채워진다.
        self.entry = dict(entry or {})
        self.n = int(len(df)) if df is not None else 0

    # -- 봉 필드 -----------------------------------------------------------------
    def bar(self, field: str) -> np.ndarray:
        f = field.lower()
        arr = self._bars.get(f)
        if arr is None:
            if f == "marcap":
                # marcap parquet 의 Marcap 은 이미 '원' 단위다 (= Close × Stocks).
                arr = _raw_column(self.df, "Marcap")
            else:
                arr = source_series(self.df, f)
            self._bars[f] = arr
        return arr

    # -- 지표 --------------------------------------------------------------------
    def indicator(self, spec: Mapping[str, Any]) -> np.ndarray:
        name = spec.get("indicator") or spec.get("type") or spec.get("name")
        if not name:
            raise DSLError(f"indicator 이름이 없습니다: {dict(spec)!r}")
        params = {k: v for k, v in spec.items() if k not in ("indicator", "type", "name")}
        field = params.pop("field", None)
        ck = indicator_key(name, params)
        val = self._ind.get(ck)
        if val is None:
            val = compute(name, params, self.df)
            self._ind[ck] = val
        if isinstance(val, dict):
            if not field:
                raise DSLError(
                    f"{name} 은(는) 서브필드가 있는 지표입니다. field 를 지정하세요 (가능: {list(val)})"
                )
            f = str(field).lower()
            if f not in val:
                raise DSLError(f"{name} 에 없는 서브필드: {field} (가능: {list(val)})")
            return val[f]
        if field:
            raise DSLError(f"{name} 은(는) 서브필드가 없습니다 (field={field!r})")
        return val

    # -- 이름 조회 ---------------------------------------------------------------
    def lookup(self, dotted: str):
        """``"close"``, ``"marcap"``, ``"prev.close"``, ``"ref.open"``, ``"entry.low"``,
        ``"fill.B1.price"``, ``"position.avg_price"``, 지표 별칭(``"MA20"``),
        규칙 파라미터(``"trigger_pct"``) 를 값으로 바꾼다.

        봉 필드·지표·``prev.*`` 는 배열, 나머지는 스칼라다."""
        name = dotted.strip()
        low = name.lower()

        if low in BAR_FIELDS:
            return self.bar(low)

        if name in self.aliases:
            return self.indicator(self.aliases[name])

        if "." in name:
            head, rest = name.split(".", 1)
            head_l = head.lower()
            if head_l == "prev":
                return _shift1(_as_f64(self.lookup(rest)))
            if head_l == "entry":
                key = rest.lower()
                if key not in _ENTRY_FIELDS:
                    raise DSLError(f"알 수 없는 진입봉 필드: {name}")
                if not self.entry:
                    raise DSLError(
                        f"진입 전에는 {name} 을(를) 참조할 수 없습니다 (entry.* 는 청산 규칙 전용)"
                    )
                if key not in self.entry:
                    raise DSLError(f"진입봉에 {key} 값이 없습니다: {name}")
                return _num(self.entry[key], name)
            if head_l == "ref":
                key = rest.lower()
                if key not in _REF_FIELDS:
                    raise DSLError(f"알 수 없는 기준일 필드: {name}")
                if key == "date":
                    raise DSLError("ref.date 는 수식에 쓸 수 없습니다")
                if not self.ref:
                    raise DSLError(f"기준일이 없는데 {name} 을(를) 참조했습니다")
                if key not in self.ref:
                    raise DSLError(f"기준일에 {key} 값이 없습니다")
                return _num(self.ref[key], name)
            if head_l == "fill":
                parts = rest.split(".")
                rid = parts[0]
                attr = parts[1].lower() if len(parts) > 1 else "price"
                f = self.fills.get(rid)
                if f is None:
                    raise DSLError(f"아직 체결되지 않은 규칙을 참조했습니다: {name}")
                if attr not in f:
                    raise DSLError(f"체결 정보에 {attr} 가 없습니다: {name}")
                return _num(f[attr], name)
            if head_l == "position":
                key = rest.lower()
                if key not in _POSITION_FIELDS:
                    raise DSLError(f"알 수 없는 포지션 필드: {name}")
                if key not in self.position:
                    raise DSLError(f"보유 포지션이 없는데 {name} 을(를) 참조했습니다")
                return _num(self.position[key], name)
            if head_l in ("param", "params"):
                return _num(self.params.get(rest), name)
            raise DSLError(f"알 수 없는 참조: {name}")

        # 규칙 레벨 파라미터 (trigger_pct, target_pct, ...)
        if name in self.params:
            return _num(self.params[name], name)
        if name in self.extra:
            return _num(self.extra[name], name)
        if low in ("pi",):
            return math.pi
        raise DSLError(f"알 수 없는 이름: {name}")


def _as_f64(v) -> np.ndarray:
    """``prev.*`` 용 — 시계열만 허용한다."""
    if isinstance(v, np.ndarray) and v.ndim == 1:
        return v.astype("float64", copy=False)
    raise DSLError("prev.* 는 시계열(봉 필드·지표)에만 쓸 수 있습니다")


def _num(v, where: str) -> float:
    if v is None:
        raise DSLError(f"{where} 값이 없습니다(None)")
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    if isinstance(v, (pd.Timestamp,)):
        return float(v.value)
    raise DSLError(f"{where} 값을 숫자로 쓸 수 없습니다: {v!r}")


# ======================================================================================
# 안전한 산술식 파서 (eval 금지)
# ======================================================================================

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)

_FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "open", "compile", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir", "input", "exit", "quit",
}

_AST_CACHE: Dict[str, ast.Expression] = {}


def _parse(expr: str) -> ast.Expression:
    tree = _AST_CACHE.get(expr)
    if tree is not None:
        return tree
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise DSLError(f"수식 구문 오류: {expr!r} ({e.msg})") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            raise DSLError(f"수식에서 함수 호출은 금지됩니다: {expr!r}")
        if not isinstance(node, _ALLOWED_NODES):
            raise DSLError(
                f"수식에 허용되지 않은 표현이 있습니다: {type(node).__name__} in {expr!r}"
            )
        if isinstance(node, ast.Name) and (
            node.id in _FORBIDDEN_NAMES or node.id.startswith("__")
        ):
            raise DSLError(f"수식에서 쓸 수 없는 이름입니다: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise DSLError(f"수식에서 쓸 수 없는 속성입니다: {node.attr}")
    _AST_CACHE[expr] = tree
    return tree


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return f"{_dotted(node.value)}.{sl.value}"
        raise DSLError("인덱싱은 문자열 키만 허용됩니다")
    raise DSLError(f"참조로 해석할 수 없습니다: {type(node).__name__}")


def _binop(node: ast.BinOp, a, b):
    op = node.op
    with np.errstate(divide="ignore", invalid="ignore"):
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            return _div(a, b)
        if isinstance(op, ast.FloorDiv):
            return np.floor(_div(a, b))
        if isinstance(op, ast.Mod):
            return np.mod(a, b)
        if isinstance(op, ast.Pow):
            return a ** b
    raise DSLError(f"허용되지 않은 연산자: {type(op).__name__}")


def _div(a, b):
    if np.isscalar(a) and np.isscalar(b):
        return float("nan") if b == 0 else a / b
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.true_divide(a, b)
    r = np.asarray(r, dtype="float64")
    r[~np.isfinite(r)] = np.nan
    return r


def _eval_node(node: ast.AST, ctx: EvalContext):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        raise DSLError(f"수식에 쓸 수 없는 상수: {v!r}")
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return ctx.lookup(_dotted(node))
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand, ctx)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise DSLError(f"허용되지 않은 단항 연산자: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        return _binop(node, _eval_node(node.left, ctx), _eval_node(node.right, ctx))
    raise DSLError(f"허용되지 않은 표현: {type(node).__name__}")


def safe_eval_expr(expr: str, ctx: EvalContext):
    """산술식을 평가한다. 결과는 float 스칼라 또는 np.ndarray."""
    if not isinstance(expr, str):
        raise DSLError(f"expr 은 문자열이어야 합니다: {expr!r}")
    return _eval_node(_parse(expr), ctx)


def eval_expr(expr: str, ctx: EvalContext, i: int | None = None):
    """``safe_eval_expr`` 결과를 (필요하면) ``i`` 시점 스칼라로 좁힌다."""
    return at(safe_eval_expr(expr, ctx), i)


# ======================================================================================
# 피연산자 / 조건식
# ======================================================================================


def at(value, i: int | None):
    """배열이면 ``i`` 번째 값을, 스칼라면 그대로 돌려준다."""
    if i is None:
        return value
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return float(value)
        if i < 0 or i >= value.shape[0]:
            return float("nan")
        return float(value[i])
    return value


def evaluate_operand(operand: Any, ctx: EvalContext, i: int | None = None):
    """DSL 피연산자를 값으로 바꾼다.

    ``i`` 를 주면 그 시점 스칼라, 주지 않으면 배열 또는 스칼라 그대로.
    """
    return at(_operand_raw(operand, ctx), i)


def _operand_raw(operand: Any, ctx: EvalContext):
    if operand is None:
        raise DSLError("피연산자가 None 입니다")
    if isinstance(operand, bool):
        return float(operand)
    if isinstance(operand, (int, float, np.integer, np.floating)):
        return float(operand)
    if isinstance(operand, np.ndarray):
        return operand
    if isinstance(operand, str):
        s = operand.strip()
        # 숫자 문자열 리터럴 허용
        try:
            return float(s)
        except ValueError:
            pass
        return ctx.lookup(s)
    if isinstance(operand, Mapping):
        if "indicator" in operand:
            return ctx.indicator(operand)
        if "expr" in operand:
            return safe_eval_expr(operand["expr"], ctx)
        for k in ("value", "const", "literal"):
            if k in operand:
                return _operand_raw(operand[k], ctx)
        if "ref" in operand:
            return ctx.lookup(f"ref.{operand['ref']}")
        if "field" in operand and len(operand) == 1:
            return ctx.lookup(str(operand["field"]))
        raise DSLError(f"알 수 없는 피연산자: {dict(operand)!r}")
    raise DSLError(f"알 수 없는 피연산자 타입: {type(operand).__name__}")


def _as_array(v, n: int) -> np.ndarray:
    if isinstance(v, np.ndarray):
        if v.shape[0] == n:
            return v.astype("float64", copy=False)
        out = np.full(n, np.nan, dtype="float64")
        m = min(n, v.shape[0])
        out[:m] = v[:m]
        return out
    return np.full(n, float(v), dtype="float64")


def _shift1(a: np.ndarray) -> np.ndarray:
    out = np.empty_like(a)
    out[0] = np.nan
    out[1:] = a[:-1]
    return out


_CMP = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def evaluate_condition(node: Any, ctx: EvalContext, i: int | None = None):
    """``when`` 조건식을 평가한다.

    ``i=None`` → ``np.ndarray[bool]`` (길이 = len(df))
    ``i=<int>`` → ``bool``
    """
    if node is None:
        # 조건이 없으면 항상 참
        return True if i is not None else np.ones(ctx.n, dtype=bool)
    if isinstance(node, bool):
        return node if i is not None else np.full(ctx.n, node, dtype=bool)
    if not isinstance(node, Mapping):
        raise DSLError(f"조건식은 객체여야 합니다: {node!r}")

    op = node.get("op")
    if not op:
        raise DSLError(f"조건식에 op 가 없습니다: {dict(node)!r}")
    op = str(op).lower()

    if op in ("and", "or"):
        conds = node.get("conditions") or node.get("all") or node.get("any") or []
        if not conds:
            raise DSLError(f"{op} 조건에 conditions 가 비어 있습니다")
        vals = [evaluate_condition(c, ctx, i) for c in conds]
        if i is not None:
            return all(vals) if op == "and" else any(vals)
        acc = vals[0]
        for v in vals[1:]:
            acc = np.logical_and(acc, v) if op == "and" else np.logical_or(acc, v)
        return acc

    if op == "not":
        inner = node.get("condition") or node.get("conditions")
        if isinstance(inner, list):
            inner = inner[0] if inner else None
        v = evaluate_condition(inner, ctx, i)
        return (not v) if i is not None else np.logical_not(v)

    if op not in COMPARISON_OPS:
        raise DSLError(f"알 수 없는 op: {op}")

    left_raw = _operand_raw(node.get("left"), ctx)
    right_raw = _operand_raw(node.get("right"), ctx)

    if op in ("cross_above", "cross_below"):
        return _cross(op, left_raw, right_raw, ctx, i)

    if i is not None:
        a = at(left_raw, i)
        b = at(right_raw, i)
        if _isnan(a) or _isnan(b):
            return False
        return bool(_CMP[op](a, b))

    n = ctx.n
    a = _as_array(left_raw, n)
    b = _as_array(right_raw, n)
    with np.errstate(invalid="ignore"):
        res = _CMP[op](a, b)
    res = np.asarray(res, dtype=bool)
    res[np.isnan(a) | np.isnan(b)] = False
    return res


def _isnan(x) -> bool:
    try:
        return bool(np.isnan(x))
    except (TypeError, ValueError):
        return False


def _cross(op: str, left_raw, right_raw, ctx: EvalContext, i: int | None):
    n = ctx.n
    a = _as_array(left_raw, n)
    b = _as_array(right_raw, n)
    pa = _shift1(a)
    pb = _shift1(b)
    if op == "cross_above":
        cur = a > b
        prev = pa <= pb
    else:
        cur = a < b
        prev = pa >= pb
    with np.errstate(invalid="ignore"):
        res = np.asarray(cur & prev, dtype=bool)
    res[np.isnan(a) | np.isnan(b) | np.isnan(pa) | np.isnan(pb)] = False
    if i is not None:
        if i < 0 or i >= n:
            return False
        return bool(res[i])
    return res
