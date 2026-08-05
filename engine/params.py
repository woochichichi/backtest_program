"""``params`` 선언 처리 (ARCHITECTURE-v2 §1).

전략 JSON 최상위 ``params`` 는 **UI 전용 메타데이터**다. 엔진 로직은 이 값을 읽지 않고
``path`` 가 가리키는 실제 값만 본다. 이 모듈은 서버·프런트가 공유할 경로 접근기를 제공한다.

    from engine.params import get_by_path, set_by_path, reset_to_defaults

경로 표기는 ``entries[0].size_pct`` 형식 — 점(``.``) 구분, 대괄호 정수 인덱스.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple, Union

from .errors import PathError

__all__ = [
    "PARAM_TYPES",
    "parse_path",
    "format_path",
    "get_by_path",
    "set_by_path",
    "path_exists",
    "reset_to_defaults",
    "collect_params",
    "params_snapshot",
]

#: ``params[].type`` 로 허용되는 값
PARAM_TYPES = ("number", "int", "percent", "select", "bool", "date", "text")

_SENTINEL = object()

_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)((?:\[\d+\])*)$")
_INDEX = re.compile(r"\[(\d+)\]")

PathPart = Union[str, int]


# --------------------------------------------------------------------------------------
# 경로 파싱
# --------------------------------------------------------------------------------------


def parse_path(path: str) -> List[PathPart]:
    """``"entries[0].size_pct"`` → ``["entries", 0, "size_pct"]``."""
    if not isinstance(path, str) or not path.strip():
        raise PathError(f"경로는 비어 있지 않은 문자열이어야 합니다: {path!r}")
    parts: List[PathPart] = []
    for seg in path.strip().split("."):
        m = _SEGMENT.match(seg)
        if not m:
            raise PathError(
                f"경로 표기가 잘못됐습니다: {path!r} — 'entries[0].size_pct' 형식이어야 합니다"
            )
        parts.append(m.group(1))
        for idx in _INDEX.findall(m.group(2) or ""):
            parts.append(int(idx))
    return parts


def format_path(parts: Sequence[PathPart]) -> str:
    """``["entries", 0, "size_pct"]`` → ``"entries[0].size_pct"``."""
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out = f"{out}.{p}" if out else str(p)
    return out


# --------------------------------------------------------------------------------------
# 읽기 / 쓰기
# --------------------------------------------------------------------------------------


def _walk(doc: Any, parts: Sequence[PathPart], path: str, upto: int | None = None):
    cur = doc
    end = len(parts) if upto is None else upto
    for i in range(end):
        p = parts[i]
        if isinstance(p, int):
            if not isinstance(cur, Sequence) or isinstance(cur, (str, bytes)):
                raise PathError(f"가리키는 위치가 없습니다: {path} ('{format_path(parts[:i])}' 가 배열이 아닙니다)")
            if p >= len(cur) or p < -len(cur):
                raise PathError(f"가리키는 위치가 없습니다: {path} (인덱스 {p} 범위 밖)")
            cur = cur[p]
        else:
            if not isinstance(cur, Mapping) or p not in cur:
                raise PathError(f"가리키는 위치가 없습니다: {path}")
            cur = cur[p]
    return cur


def get_by_path(doc: Any, path: str, default: Any = _SENTINEL) -> Any:
    """경로가 가리키는 값. 없으면 ``PathError`` (또는 ``default``)."""
    parts = parse_path(path)
    try:
        return _walk(doc, parts, path)
    except PathError:
        if default is not _SENTINEL:
            return default
        raise


def path_exists(doc: Any, path: str) -> bool:
    try:
        get_by_path(doc, path)
        return True
    except PathError:
        return False


def set_by_path(doc: Any, path: str, value: Any) -> Any:
    """경로에 값을 써넣고 ``doc`` 을 돌려준다 (제자리 수정).

    중간에 없는 **딕셔너리 키**는 만들어 준다. 배열 인덱스는 만들지 않는다
    (몇 개짜리 배열이어야 하는지 알 수 없으므로).
    """
    parts = parse_path(path)
    if not parts:
        raise PathError(f"경로가 비어 있습니다: {path!r}")

    cur = doc
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(p, int):
            if not isinstance(cur, list):
                raise PathError(f"가리키는 위치가 없습니다: {path} ('{format_path(parts[:i])}' 가 배열이 아닙니다)")
            if p >= len(cur):
                raise PathError(f"가리키는 위치가 없습니다: {path} (인덱스 {p} 범위 밖)")
            cur = cur[p]
        else:
            if not isinstance(cur, MutableMapping):
                raise PathError(f"가리키는 위치가 없습니다: {path}")
            if p not in cur or cur[p] is None:
                if isinstance(nxt, int):
                    raise PathError(f"가리키는 위치가 없습니다: {path} ('{p}' 배열이 없습니다)")
                cur[p] = {}
            cur = cur[p]

    last = parts[-1]
    if isinstance(last, int):
        if not isinstance(cur, list):
            raise PathError(f"가리키는 위치가 없습니다: {path}")
        if last >= len(cur):
            raise PathError(f"가리키는 위치가 없습니다: {path} (인덱스 {last} 범위 밖)")
        cur[last] = value
    else:
        if not isinstance(cur, MutableMapping):
            raise PathError(f"가리키는 위치가 없습니다: {path}")
        cur[last] = value
    return doc


# --------------------------------------------------------------------------------------
# params 유틸
# --------------------------------------------------------------------------------------


def collect_params(doc: Mapping) -> List[dict]:
    """``doc["params"]`` 중 dict 인 항목만."""
    raw = doc.get("params") if isinstance(doc, Mapping) else None
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, Mapping)]


def reset_to_defaults(doc: Mapping) -> dict:
    """모든 ``params[].default`` 를 각 ``path`` 에 다시 써넣은 **새 문서**를 돌려준다.

    * ``available: false`` 항목도 되돌린다.
    * ``params`` 배열 자체는 절대 건드리지 않는다.
    * 경로가 없는 항목은 조용히 건너뛴다 (검증은 ``validate_strategy`` 담당).
    """
    out = copy.deepcopy(dict(doc))
    for p in collect_params(out):
        path = p.get("path")
        if not isinstance(path, str) or "default" not in p:
            continue
        try:
            set_by_path(out, path, copy.deepcopy(p["default"]))
        except PathError:
            continue
    return out


def params_snapshot(doc: Mapping) -> List[dict]:
    """현재 문서 기준으로 각 파라미터의 ``value`` 와 ``is_default`` 를 붙여 돌려준다.

    프런트의 파라미터 폼이 "지금 값 / 기본값과 다른가"를 한 번에 그릴 수 있게 하는 편의 함수.
    """
    out = []
    for i, p in enumerate(collect_params(doc)):
        row = dict(p)
        path = p.get("path")
        row["index"] = i
        try:
            row["value"] = get_by_path(doc, path) if isinstance(path, str) else None
            row["resolved"] = True
        except PathError:
            row["value"] = None
            row["resolved"] = False
        row["is_default"] = row["resolved"] and row["value"] == p.get("default")
        out.append(row)
    return out
