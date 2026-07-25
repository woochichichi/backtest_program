"""자연어 -> 전략 DSL v1 변환 (Anthropic SDK).

ARCHITECTURE.md 4-7 계약:
    요청  {"prompt": "...", "base": <선택, 기존 DSL>}
    응답  {"ok": true,  "strategy": <DSL>, "warnings": []}
          {"ok": false, "error": "...", "how_to": "..."}

설계 원칙 (중요):
  - anthropic 패키지가 없어도, ANTHROPIC_API_KEY 가 없어도 **서버는 정상 기동**해야 한다.
    따라서 이 모듈은 import 시점에 anthropic 을 import 하지 않는다(지연 import).
  - 실패는 예외가 아니라 ok:false 페이로드로 돌려준다. 500 을 내지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 모델은 ARCHITECTURE.md 4-7 에 명시된 값을 그대로 쓴다.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "strategies" / "SCHEMA.md"

HOW_TO_KEY = (
    "명령 프롬프트에서 `setx ANTHROPIC_API_KEY sk-ant-...` 를 실행한 뒤 "
    "run_web.bat 을 다시 실행하세요. 키는 https://console.anthropic.com 에서 발급합니다."
)
HOW_TO_PKG = (
    "가상환경에 anthropic 패키지가 없습니다. install.bat 을 실행하거나 "
    "`.venv\\Scripts\\pip install anthropic` 을 실행하세요."
)

_SYSTEM_TEMPLATE = """당신은 국내 주식(KOSPI/KOSDAQ) 백테스트 전략 DSL 생성기다.
아래는 전략 DSL v1 명세다.

<schema>
{schema}
</schema>

규칙:
1. 출력은 `krx-backtest-strategy/v1` JSON 객체 **하나**뿐이다. 코드펜스(```)와 설명 문장을 절대 넣지 마라.
2. 명세에 없는 값을 지어내지 말고, 모르는 값은 명세의 기본값을 쓴다.
3. 분할 매수/매도는 `entries`/`exits` 배열 원소를 늘려서 표현한다. 새 필드를 만들지 마라.
4. 사용자의 원문 자연어를 `description` 에 그대로 보존한다.
5. `id` 는 소문자 영숫자와 `_`, `-` 만 사용하는 슬러그로 만든다 (정규식 ^[a-z0-9_-]+$).
6. `schema` 필드는 반드시 "krx-backtest-strategy/v1" 이다.
"""

_USER_TEMPLATE = """사용자 전략 설명:
\"\"\"
{prompt}
\"\"\"

위 설명을 DSL v1 JSON 하나로 변환해라. 설명 없이 JSON만 출력한다."""

_USER_TEMPLATE_WITH_BASE = """아래는 사용자가 편집 중인 기존 전략 JSON이다.

<base>
{base}
</base>

사용자 수정 요청:
\"\"\"
{prompt}
\"\"\"

기존 전략을 위 요청대로 수정한 DSL v1 JSON 하나만 출력해라. 요청과 무관한 필드는 그대로 유지한다."""


# ------------------------------------------------------------- 사용 가능 여부
_HAS_SDK: Optional[bool] = None


def has_sdk() -> bool:
    """anthropic 패키지를 import 할 수 있는지. **모듈을 실제로 실행하지 않는다.**"""
    global _HAS_SDK
    if _HAS_SDK is None:
        try:
            _HAS_SDK = importlib.util.find_spec("anthropic") is not None
        except Exception:
            _HAS_SDK = False
    return bool(_HAS_SDK)


def has_key() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def is_available() -> bool:
    """`GET /api/status` 의 features.ai_available.

    키 존재 여부와 패키지 import 가능 여부만 본다. **API 호출은 하지 않는다.**
    """
    return has_key() and has_sdk()


# ----------------------------------------------------------------- 스키마
def load_schema_text() -> str:
    """SCHEMA.md 전문을 읽어 시스템 프롬프트에 넣는다."""
    try:
        return SCHEMA_PATH.read_text(encoding="utf-8")
    except Exception:
        return "(SCHEMA.md 를 읽을 수 없습니다. strategies/SCHEMA.md 를 확인하세요.)"


def build_system_prompt() -> str:
    return _SYSTEM_TEMPLATE.format(schema=load_schema_text())


# --------------------------------------------------------- JSON 추출 유틸
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """모델 응답 문자열에서 JSON 객체 하나를 뽑아낸다.

    1) 통째로 파싱 시도
    2) 코드펜스 안쪽 파싱 시도
    3) 중괄호 균형을 세어 첫 번째 완결 객체를 잘라내 파싱 (문자열/이스케이프 인식)
    """
    if not text:
        return None

    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    for m in _FENCE_RE.finditer(text):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
        start = text.find("{", start + 1)

    return None


def _collect_text(message: Any) -> str:
    """Message 응답에서 text 블록만 이어붙인다 (thinking 블록은 건너뛴다)."""
    parts: List[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts)


# ----------------------------------------------------------------- 검증
def _validate(obj: Dict[str, Any]) -> Tuple[bool, List[Dict[str, str]]]:
    """engine.validate.validate_strategy 로 검증. 엔진이 없으면 최소 검증으로 대체."""
    try:
        from engine.validate import validate_strategy  # type: ignore
    except Exception:
        errors: List[Dict[str, str]] = []
        if obj.get("schema") != "krx-backtest-strategy/v1":
            errors.append(
                {"path": "schema", "message": "schema 는 'krx-backtest-strategy/v1' 이어야 합니다."}
            )
        for key in ("id", "name", "market", "universe", "entries", "exits", "portfolio", "execution", "period"):
            if key not in obj:
                errors.append({"path": key, "message": f"필수 항목 '{key}' 이(가) 없습니다."})
        return (not errors), errors

    ok, errors = validate_strategy(obj)
    return bool(ok), list(errors or [])


# ----------------------------------------------------------------- 엔드포인트 본체
def generate_strategy(prompt: str, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """자연어를 DSL v1 로 변환한다. 어떤 실패도 예외로 던지지 않고 dict 로 돌려준다."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {
            "ok": False,
            "error": "전략 설명(prompt)이 비어 있습니다.",
            "how_to": "만들고 싶은 매매 조건을 한국어 문장으로 적어주세요.",
        }

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {
            "ok": False,
            "error": "ANTHROPIC_API_KEY 가 설정되지 않았습니다.",
            "how_to": HOW_TO_KEY,
        }

    # 지연 import — 패키지가 없어도 서버 기동에는 영향이 없어야 한다.
    try:
        import anthropic  # type: ignore
    except Exception as exc:
        return {
            "ok": False,
            "error": "anthropic 패키지가 설치되어 있지 않습니다.",
            "how_to": HOW_TO_PKG,
            "detail": str(exc),
        }

    if base:
        user_text = _USER_TEMPLATE_WITH_BASE.format(
            base=json.dumps(base, ensure_ascii=False, indent=2), prompt=prompt
        )
    else:
        user_text = _USER_TEMPLATE.format(prompt=prompt)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "Anthropic API 호출에 실패했습니다.",
            "how_to": "네트워크 연결과 ANTHROPIC_API_KEY 값을 확인한 뒤 다시 시도하세요.",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    # 안전 정책상 거절된 경우 content 가 비어 있을 수 있다. 먼저 확인한다.
    if getattr(message, "stop_reason", None) == "refusal":
        return {
            "ok": False,
            "error": "모델이 요청을 거절했습니다.",
            "how_to": "전략 설명을 다시 작성해 주세요.",
        }

    warnings: List[str] = []
    if getattr(message, "stop_reason", None) == "max_tokens":
        warnings.append("응답이 최대 길이에 도달해 잘렸을 수 있습니다.")

    raw = _collect_text(message)
    obj = extract_json(raw)
    if obj is None:
        return {
            "ok": False,
            "error": "모델 응답에서 JSON 을 찾지 못했습니다.",
            "how_to": "전략 설명을 더 구체적으로 적고 다시 시도해 주세요.",
            "detail": raw[:1000],
        }

    obj.setdefault("schema", "krx-backtest-strategy/v1")
    if base and not obj.get("id"):
        obj["id"] = base.get("id")
    if not obj.get("description"):
        obj["description"] = prompt

    ok, errors = _validate(obj)
    if not ok:
        return {
            "ok": False,
            "error": "생성된 전략이 스키마 검증을 통과하지 못했습니다.",
            "how_to": "조건을 더 명확히 적어 다시 생성하거나, JSON 탭에서 직접 수정하세요.",
            "errors": errors,
            "strategy": obj,
        }

    usage = getattr(message, "usage", None)
    if usage is not None:
        warnings.append(
            "토큰 사용: 입력 %s / 출력 %s"
            % (getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))
        )

    return {"ok": True, "strategy": obj, "warnings": warnings}
