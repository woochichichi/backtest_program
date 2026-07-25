"""KRX Backtester — FastAPI 서버.

ARCHITECTURE.md 4장의 HTTP API를 경로·요청·응답 스키마 그대로 구현한다.

설계 원칙
---------
1. **engine 이 없어도 서버는 기동한다.** engine.* 는 전부 지연 import 하고,
   없으면 503 + 한국어 안내로 떨어진다. (`import server.app` 은 항상 성공해야 한다)
2. **NaN 은 절대 JSON 으로 나가지 않는다.** 모든 응답은 SafeJSONResponse 를 통해
   비유한 float / numpy 스칼라 / 날짜를 정규화한 뒤 `allow_nan=False` 로 직렬화한다.
3. **모든 에러 응답은 같은 모양이다.** `{"ok": false, "error": "한국어", "detail": "..."}`
4. `server/web/` 은 다른 계층이 만든다. 없어도 죽지 않고 안내 HTML 을 돌려준다.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import json
import math
import mimetypes
import os
import platform
import queue
import re
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from server import ai as ai_module
from server.runs import RUNS, trades_for_code

try:  # numpy 는 engine 이 쓰지만, 서버 단독 기동 시에는 없어도 된다.
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None  # type: ignore


# ==================================================================== 경로
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "server" / "web"
STRATEGY_DIR = ROOT / "strategies"
BACKUP_DIR = STRATEGY_DIR / ".bak"
MARCAP_DIR = ROOT / "marcap"
CACHE_DIR = ROOT / "cache"
DATA_STATUS = ROOT / "data_status.json"
UPDATE_BAT = ROOT / "update_marcap.bat"

TASK_NAME = "KRXBacktesterDataSync"
IS_WINDOWS = os.name == "nt" or platform.system().lower().startswith("win")

SYNC_TIMEOUT_SEC = 600
BACKTEST_TIMEOUT_SEC = 60
SSE_IDLE_TIMEOUT_SEC = 300

STRATEGY_ID_RE = re.compile(r"^[a-z0-9_-]+$")

MSG_NO_DATA = (
    "marcap 데이터가 없습니다. update_marcap.bat 을 실행해 데이터를 먼저 받으세요."
)
MSG_NO_ENGINE = (
    "백테스트 엔진(engine 모듈)을 찾을 수 없습니다. "
    "engine 폴더가 설치되어 있는지 확인하고 install.bat 을 다시 실행하세요."
)


# ======================================================== JSON 안전 직렬화
def sanitize(obj: Any) -> Any:
    """NaN/Inf -> None, numpy/pandas 스칼라 -> 파이썬 기본형, 날짜 -> ISO 문자열.

    파이썬 float('nan') 이 그대로 json.dumps 되면 `NaN` 리터럴이 나가고
    브라우저의 JSON.parse 가 깨진다. 모든 응답이 이 함수를 통과한다.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None

    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else str(k)): sanitize(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [sanitize(v) for v in obj]

    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()

    # NaN / NaT 계열 (자기 자신과 다른 값)
    try:
        if obj != obj:  # noqa: PLR0124
            return None
    except Exception:
        pass

    if _np is not None:
        if isinstance(obj, _np.ndarray):
            return [sanitize(v) for v in obj.tolist()]
        if isinstance(obj, _np.generic):
            return sanitize(obj.item())

    # 0차원 스칼라 (numpy 미설치 환경 대비 덕타이핑)
    if getattr(obj, "shape", None) == () and hasattr(obj, "item"):
        try:
            return sanitize(obj.item())
        except Exception:
            pass

    if hasattr(obj, "tolist"):
        try:
            return sanitize(obj.tolist())
        except Exception:
            pass

    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            pass

    return str(obj)


class SafeJSONResponse(JSONResponse):
    """NaN 을 null 로 바꿔 직렬화하는 기본 응답 클래스."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            sanitize(content),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


# ============================================================== 에러 규격
class ApiError(Exception):
    """`{"ok": false, "error": ..., "detail": ...}` 로 직렬화되는 에러."""

    def __init__(
        self,
        status: int,
        error: str,
        detail: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(error)
        self.status = int(status)
        self.error = error
        self.detail = detail or ""
        self.extra = extra or {}

    def payload(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"ok": False, "error": self.error, "detail": self.detail}
        body.update(self.extra)
        return body


def error_response(
    status: int, error: str, detail: str = "", **extra: Any
) -> SafeJSONResponse:
    body: Dict[str, Any] = {"ok": False, "error": error, "detail": detail}
    body.update(extra)
    return SafeJSONResponse(status_code=status, content=body)


# ------------------------------------------------- errors[].path 표기 통일
_PATH_DOT_INDEX_RE = re.compile(r"\.(\d+)(?=\.|$)")
_PATH_LEADING_DOT_RE = re.compile(r"^\.+")


def normalize_error_path(path: Any) -> str:
    """검증 오류 경로를 `entries[0].size_pct` 형식으로 고정한다.

    프런트 폼의 ``data-path`` 와 1:1 로 맞춰야 하므로 표기가 섞이면 안 된다.
      - `/entries/0/size_pct`  -> `entries[0].size_pct`
      - `entries.0.size_pct`   -> `entries[0].size_pct`
      - `entries[0].size_pct`  -> 그대로
    """
    if path is None:
        return ""
    if isinstance(path, (list, tuple)):
        parts: List[str] = []
        for seg in path:
            if isinstance(seg, int) or (isinstance(seg, str) and seg.isdigit()):
                parts.append(f"[{int(seg)}]")
            else:
                parts.append(("." if parts else "") + str(seg))
        return "".join(parts).lstrip(".")

    text = str(path).strip()
    if not text:
        return ""

    # JSON Pointer / 슬래시 표기 -> 점 표기
    if "/" in text:
        text = text.lstrip("#").lstrip("/")
        text = ".".join(seg for seg in text.split("/") if seg != "")

    # `.0.` / 끝의 `.0` -> `[0]`
    prev = None
    while prev != text:
        prev = text
        text = _PATH_DOT_INDEX_RE.sub(r"[\1]", text)

    text = _PATH_LEADING_DOT_RE.sub("", text)
    return text.replace(".[", "[")


def normalize_errors(errors: Any) -> List[Dict[str, Any]]:
    """errors 배열의 모든 path 를 정규 표기로 바꾼다."""
    out: List[Dict[str, Any]] = []
    for item in list(errors or []):
        if isinstance(item, dict):
            fixed = dict(item)
            fixed["path"] = normalize_error_path(item.get("path"))
            fixed.setdefault("message", "")
            out.append(fixed)
        else:
            out.append({"path": "", "message": str(item)})
    return out


# ================================================================= 앱 생성
app = FastAPI(
    title="KRX Backtester",
    version="1.0.0",
    default_response_class=SafeJSONResponse,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiError)
async def _handle_api_error(request: Request, exc: ApiError) -> SafeJSONResponse:
    return SafeJSONResponse(status_code=exc.status, content=exc.payload())


@app.exception_handler(HTTPException)
async def _handle_http_error(request: Request, exc: HTTPException) -> SafeJSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "ok" in detail:
        return SafeJSONResponse(status_code=exc.status_code, content=detail)
    return SafeJSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": str(detail), "detail": ""},
    )


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> SafeJSONResponse:
    return SafeJSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": "요청 형식이 올바르지 않습니다.",
            "detail": "요청 본문(JSON)을 확인하세요.",
            "errors": normalize_errors(
                [
                    {"path": list(e.get("loc", [])), "message": e.get("msg", "")}
                    for e in exc.errors()
                ]
            ),
        },
    )


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception) -> SafeJSONResponse:
    if _is_data_unavailable(exc):
        return SafeJSONResponse(
            status_code=503,
            content={"ok": False, "error": MSG_NO_DATA, "detail": str(exc)},
        )
    traceback.print_exc()
    return SafeJSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "서버 내부 오류가 발생했습니다.",
            "detail": f"{type(exc).__name__}: {exc}",
        },
    )


# ========================================================== engine 접근자
_STORE: Any = None
_STORE_LOCK = threading.Lock()


def _is_data_unavailable(exc: BaseException) -> bool:
    """engine.data.DataUnavailable 인지 클래스 이름으로 판별 (import 없이)."""
    for klass in type(exc).__mro__:
        if klass.__name__ == "DataUnavailable":
            return True
    return False


def _engine(module: str, *names: str) -> Tuple[Any, ...]:
    """engine 하위 모듈에서 심볼을 가져온다. 없으면 503."""
    try:
        mod = __import__(module, fromlist=list(names) or ["__name__"])
    except Exception as exc:
        raise ApiError(503, MSG_NO_ENGINE, f"{type(exc).__name__}: {exc}") from exc
    out = []
    for name in names:
        if not hasattr(mod, name):
            raise ApiError(503, MSG_NO_ENGINE, f"{module}.{name} 이(가) 없습니다.")
        out.append(getattr(mod, name))
    return tuple(out)


def get_store() -> Any:
    """MarcapStore 싱글턴. engine 이 없으면 503."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            (MarcapStore,) = _engine("engine.data", "MarcapStore")
            try:
                _STORE = MarcapStore(root=str(MARCAP_DIR), cache_dir=str(CACHE_DIR))
            except Exception as exc:
                if _is_data_unavailable(exc):
                    raise ApiError(503, MSG_NO_DATA, str(exc)) from exc
                raise ApiError(
                    503, "데이터 저장소를 초기화하지 못했습니다.", f"{type(exc).__name__}: {exc}"
                ) from exc
        return _STORE


def get_validator():
    """engine.validate.validate_strategy. 없으면 내장 최소 검증기로 대체한다."""
    try:
        from engine.validate import validate_strategy  # type: ignore

        return validate_strategy, True
    except Exception:
        return _fallback_validate, False


def _fallback_validate(obj: Any) -> Tuple[bool, List[Dict[str, str]]]:
    """engine 이 아직 없을 때 쓰는 최소 검증 (필수 키 + schema 값)."""
    errors: List[Dict[str, str]] = []
    if not isinstance(obj, dict):
        return False, [{"path": "", "message": "전략은 JSON 객체여야 합니다."}]
    if obj.get("schema") != "krx-backtest-strategy/v1":
        errors.append(
            {"path": "schema", "message": "schema 는 'krx-backtest-strategy/v1' 이어야 합니다."}
        )
    for key in ("id", "name", "market", "universe", "entries", "exits", "portfolio", "execution", "period"):
        if key not in obj:
            errors.append({"path": key, "message": f"필수 항목 '{key}' 이(가) 없습니다."})
    for key in ("entries", "exits"):
        if key in obj and not isinstance(obj[key], list):
            errors.append({"path": key, "message": f"'{key}' 는 배열이어야 합니다."})
    return (not errors), errors


# ================================================== 4-1. GET /api/status
def _read_data_status() -> Dict[str, Any]:
    try:
        with open(DATA_STATUS, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def query_auto_sync() -> Dict[str, Any]:
    """자동 갱신(작업 스케줄러) 등록 여부. Windows 가 아니면 항상 미등록."""
    base = {"registered": False, "time": None, "task": TASK_NAME}
    if not IS_WINDOWS:
        return base
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return base
    if proc.returncode != 0:
        return base

    out = proc.stdout or b""
    text = ""
    for enc in ("cp949", "utf-8", "latin-1"):
        try:
            text = out.decode(enc)
            break
        except Exception:
            continue

    run_time = None
    for line in text.splitlines():
        if ("Start Time" in line) or ("시작 시간" in line) or ("Start:" in line):
            m = re.search(r"([0-2]?\d:[0-5]\d)", line)
            if m:
                run_time = m.group(1)
                break
    if run_time is None:
        m = re.search(r"\b([0-2]?\d:[0-5]\d)(?::[0-5]\d)?\b", text)
        run_time = m.group(1) if m else None

    return {"registered": True, "time": run_time, "task": TASK_NAME}


def _fallback_status() -> Dict[str, Any]:
    """engine 이나 데이터가 없을 때 data_status.json 만으로 만드는 4-1 페이로드."""
    raw = _read_data_status()
    data_dir = MARCAP_DIR / "data"
    file_count = raw.get("file_count") or 0
    if data_dir.is_dir():
        try:
            file_count = len(list(data_dir.glob("marcap-*")))
        except Exception:
            pass
    available = bool(data_dir.is_dir() and file_count)
    return {
        "available": available,
        "last_sync": raw.get("last_sync") or None,
        "result": raw.get("result") or None,
        "latest_trade_date": None,
        "first_trade_date": None,
        "file_count": file_count or 0,
        "git_rev": raw.get("git_rev") or None,
        "row_count": None,
    }


def build_status() -> Dict[str, Any]:
    payload = _fallback_status()
    try:
        store = get_store()
        engine_status = store.status()
        if isinstance(engine_status, dict):
            for key, value in engine_status.items():
                if value is not None or key not in payload:
                    payload[key] = value
    except ApiError:
        pass
    except Exception as exc:
        if not _is_data_unavailable(exc):
            payload.setdefault("detail", f"{type(exc).__name__}: {exc}")

    payload.setdefault("available", False)
    if not payload.get("available"):
        for key in ("latest_trade_date", "first_trade_date", "row_count"):
            payload.setdefault(key, None)
    payload["auto_sync"] = query_auto_sync()
    payload["features"] = build_features()
    return payload


def build_features() -> Dict[str, bool]:
    """프런트가 기능 유무를 추측하지 않도록 서버가 명시한다.

    ``ai_available`` 은 키 존재 + 패키지 import 가능 여부만 본다 (실제 API 호출 없음).
    """
    return {
        "backtest_progress_sse": True,
        "ai_available": bool(ai_module.is_available()),
        "symbol_search": True,
    }


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    """데이터가 없어도 200 + available:false 를 반환한다."""
    return build_status()


# ==================================================== 4-2. POST /api/sync
_SYNC_LOCK = threading.Lock()

# 프런트가 2초 간격으로 폴링하는 진행 상태. 실행 중에는 log_tail 이 계속 갱신된다.
_SYNC_STATE: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "elapsed_sec": 0,
    "log_tail": "",
    "ok": None,
    "result": None,
}
_SYNC_STATE_LOCK = threading.Lock()


def _sync_set(**fields: Any) -> None:
    with _SYNC_STATE_LOCK:
        _SYNC_STATE.update(fields)


def _sync_snapshot() -> Dict[str, Any]:
    with _SYNC_STATE_LOCK:
        return dict(_SYNC_STATE)


def _clean_log(raw: bytes) -> str:
    """git 진행 표시는 \\r 로 갱신되므로 줄바꿈으로 정규화한다."""
    text = _decode(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip())


def _run_sync_process(cmd: List[str]) -> Tuple[int, str]:
    """동기화 명령을 실행하면서 출력을 실시간으로 _SYNC_STATE 에 반영한다."""
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    buf = bytearray()
    buf_lock = threading.Lock()

    def pump() -> None:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            try:
                chunk = stream.read(4096)
            except Exception:
                break
            if not chunk:
                break
            with buf_lock:
                buf.extend(chunk)
                if len(buf) > 262144:  # 256KB 넘으면 앞부분을 버린다
                    del buf[: len(buf) - 131072]

    reader = threading.Thread(target=pump, name="sync-log", daemon=True)
    reader.start()

    started = time.monotonic()
    timed_out = False
    while True:
        if proc.poll() is not None:
            break
        if time.monotonic() - started > SYNC_TIMEOUT_SEC:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            break
        with buf_lock:
            snapshot = bytes(buf)
        _sync_set(
            elapsed_sec=int(time.monotonic() - started),
            log_tail=_tail(_clean_log(snapshot), 2000),
        )
        time.sleep(0.2)

    reader.join(timeout=3)
    with buf_lock:
        snapshot = bytes(buf)
    log_tail = _tail(_clean_log(snapshot), 4000)
    code = -1 if timed_out else int(proc.returncode or 0)
    if timed_out:
        raise ApiError(
            504,
            f"데이터 갱신이 {SYNC_TIMEOUT_SEC}초 안에 끝나지 않았습니다.",
            "최초 clone 은 수 분이 걸립니다. update_marcap.bat 을 직접 실행해 진행 상황을 확인하세요.",
            {"log_tail": log_tail},
        )
    return code, log_tail


@app.post("/api/sync")
def api_sync() -> Any:
    if not _SYNC_LOCK.acquire(blocking=False):
        raise ApiError(
            409,
            "이미 데이터 갱신이 진행 중입니다.",
            "진행 중인 갱신이 끝난 뒤 다시 시도하세요. 진행 상황은 GET /api/sync/status 로 확인할 수 있습니다.",
        )

    started_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    started_mono = time.monotonic()
    _sync_set(
        running=True,
        started_at=started_at,
        finished_at=None,
        elapsed_sec=0,
        log_tail="",
        ok=None,
        result=None,
    )

    def finish(ok: bool, result: str, log_tail: str) -> None:
        _sync_set(
            running=False,
            finished_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_sec=int(time.monotonic() - started_mono),
            log_tail=log_tail,
            ok=ok,
            result=result,
        )

    try:
        if IS_WINDOWS:
            if not UPDATE_BAT.exists():
                finish(False, "bat_not_found", "")
                raise ApiError(503, "update_marcap.bat 을 찾을 수 없습니다.", f"경로: {UPDATE_BAT}")
            cmd: List[str] = ["cmd", "/c", str(UPDATE_BAT), "/silent"]
        else:
            cmd = ["git", "-C", "marcap", "pull", "--ff-only"]

        try:
            code, log_tail = _run_sync_process(cmd)
        except ApiError as exc:
            finish(False, "timeout", str(exc.extra.get("log_tail", "")))
            raise
        except FileNotFoundError as exc:
            finish(False, "command_not_found", "")
            raise ApiError(
                503,
                "갱신 명령을 실행할 수 없습니다. git 이 설치되어 있는지 확인하세요.",
                str(exc),
                {"log_tail": ""},
            ) from exc

        if code != 0:
            finish(False, "failed", log_tail)
            return error_response(
                500,
                "데이터 갱신에 실패했습니다. update_marcap.bat 을 직접 실행해 로그를 확인하세요.",
                f"exit code {code}",
                log_tail=log_tail,
            )

        # 갱신 후 캐시 전부 무효화
        global _STORE
        with _STORE_LOCK:
            _STORE = None
        _symbols_invalidate()

        finish(True, "updated", log_tail)
        payload = build_status()
        payload["log_tail"] = log_tail
        return payload
    finally:
        with _SYNC_STATE_LOCK:
            if _SYNC_STATE.get("running"):
                _SYNC_STATE["running"] = False
                _SYNC_STATE["finished_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _SYNC_LOCK.release()


@app.get("/api/sync/status")
def api_sync_status() -> Dict[str, Any]:
    """동기화 진행 상황 폴링 (프런트가 2초 간격으로 호출)."""
    state = _sync_snapshot()
    return {
        "running": bool(state.get("running")),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "elapsed_sec": int(state.get("elapsed_sec") or 0),
        "log_tail": state.get("log_tail") or "",
        "ok": state.get("ok"),
        "result": state.get("result"),
    }


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return repr(raw)


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


# ======================================================== 4-3. 전략 CRUD
def _check_id(sid: str) -> str:
    sid = (sid or "").strip()
    if not STRATEGY_ID_RE.match(sid):
        raise ApiError(
            400,
            "전략 id 형식이 올바르지 않습니다.",
            "id 는 소문자 영문/숫자/밑줄/하이픈만 쓸 수 있습니다 (^[a-z0-9_-]+$).",
        )
    return sid


def _strategy_path(sid: str) -> Path:
    return STRATEGY_DIR / f"{_check_id(sid)}.json"


def _load_strategy_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ApiError(500, "전략 파일이 JSON 객체가 아닙니다.", str(path))
    return data


def _backup(path: Path) -> Optional[str]:
    """덮어쓰기/삭제 전에 strategies/.bak/<id>-<timestamp>.json 으로 백업."""
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    dest = BACKUP_DIR / f"{path.stem}-{stamp}.json"
    n = 1
    while dest.exists():
        n += 1
        dest = BACKUP_DIR / f"{path.stem}-{stamp}-{n}.json"
    dest.write_bytes(path.read_bytes())
    return str(dest)


def _write_strategy(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _validate_or_422(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ApiError(
            422,
            "전략은 JSON 객체여야 합니다.",
            "",
            {"errors": [{"path": "", "message": "JSON 객체가 아닙니다."}]},
        )
    validate, from_engine = get_validator()
    try:
        ok, errors = validate(obj)
    except Exception as exc:
        raise ApiError(500, "전략 검증 중 오류가 발생했습니다.", f"{type(exc).__name__}: {exc}") from exc
    if not ok:
        raise ApiError(
            422,
            "전략 검증에 실패했습니다.",
            "" if from_engine else "engine.validate 가 없어 최소 검증만 수행했습니다.",
            {"errors": normalize_errors(errors)},
        )
    return obj


@app.get("/api/strategies")
def api_strategies_list() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not STRATEGY_DIR.is_dir():
        return out
    for path in sorted(STRATEGY_DIR.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            obj = _load_strategy_file(path)
        except Exception:
            continue
        mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime)
        out.append(
            {
                "id": obj.get("id") or path.stem,
                "name": obj.get("name") or path.stem,
                "description": obj.get("description") or "",
                "enabled": bool(obj.get("enabled", True)),
                "updated_at": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return out


@app.post("/api/strategies/validate")
async def api_strategies_validate(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    obj = body.get("strategy") if isinstance(body, dict) and "strategy" in body else body
    validate, from_engine = get_validator()
    if not isinstance(obj, dict):
        return {"valid": False, "errors": [{"path": "", "message": "전략은 JSON 객체여야 합니다."}]}
    try:
        ok, errors = validate(obj)
    except Exception as exc:
        return {
            "valid": False,
            "errors": normalize_errors([{"path": "", "message": f"검증 중 오류: {type(exc).__name__}: {exc}"}]),
        }
    result: Dict[str, Any] = {"valid": bool(ok), "errors": normalize_errors(errors)}
    if not from_engine:
        result["warning"] = "engine.validate 가 없어 최소 검증만 수행했습니다."
    return result


@app.post("/api/strategies")
async def api_strategies_create(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    obj = body.get("strategy") if isinstance(body, dict) and "strategy" in body else body
    if not isinstance(obj, dict):
        raise ApiError(400, "요청 본문이 전략 JSON 객체가 아닙니다.")
    sid = _check_id(str(obj.get("id") or ""))
    path = _strategy_path(sid)
    if path.exists():
        raise ApiError(409, f"'{sid}' 전략이 이미 있습니다.", "다른 id 를 쓰거나 PUT 으로 수정하세요.")
    obj["id"] = sid
    _validate_or_422(obj)
    _write_strategy(path, obj)
    return {"ok": True, "id": sid, "strategy": obj}


@app.get("/api/strategies/{sid}")
def api_strategies_get(sid: str) -> Dict[str, Any]:
    path = _strategy_path(sid)
    if not path.exists():
        raise ApiError(404, f"'{sid}' 전략을 찾을 수 없습니다.", f"경로: {path.name}")
    return _load_strategy_file(path)


@app.put("/api/strategies/{sid}")
async def api_strategies_put(sid: str, request: Request) -> Dict[str, Any]:
    sid = _check_id(sid)
    body = await _json_body(request)
    obj = body.get("strategy") if isinstance(body, dict) and "strategy" in body else body
    if not isinstance(obj, dict):
        raise ApiError(400, "요청 본문이 전략 JSON 객체가 아닙니다.")

    body_id = str(obj.get("id") or "").strip()
    if body_id and body_id != sid:
        raise ApiError(
            400,
            "전략 id 가 경로와 일치하지 않습니다.",
            f"경로 id='{sid}', 본문 id='{body_id}'",
        )
    obj["id"] = sid

    _validate_or_422(obj)

    path = _strategy_path(sid)
    backup = _backup(path)
    _write_strategy(path, obj)
    return {"ok": True, "id": sid, "backup": backup, "strategy": obj}


@app.delete("/api/strategies/{sid}")
def api_strategies_delete(sid: str) -> Dict[str, Any]:
    path = _strategy_path(sid)
    if not path.exists():
        raise ApiError(404, f"'{sid}' 전략을 찾을 수 없습니다.", f"경로: {path.name}")
    backup = _backup(path)
    path.unlink()
    return {"ok": True, "id": _check_id(sid), "backup": backup}


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception as exc:
        raise ApiError(400, "요청 본문이 올바른 JSON 이 아닙니다.", str(exc)) from exc


# =================================================== 4-4. GET /api/indicators
_OVERLAY_DEFAULT = {
    "SMA", "EMA", "WMA", "BBANDS", "VWAP", "ENVELOPE", "DONCHIAN",
}


def _normalize_params(spec: Any) -> List[Dict[str, Any]]:
    """REGISTRY 의 params 를 [{"name","type","default"}] 형태로 정규화."""
    params = None
    if isinstance(spec, dict):
        params = spec.get("params")
    if params is None:
        return []

    out: List[Dict[str, Any]] = []
    if isinstance(params, dict):
        for name, meta in params.items():
            if isinstance(meta, dict):
                out.append(
                    {
                        "name": name,
                        "type": meta.get("type") or _guess_type(meta.get("default")),
                        "default": meta.get("default"),
                    }
                )
            else:
                out.append({"name": name, "type": _guess_type(meta), "default": meta})
        return out

    if isinstance(params, (list, tuple)):
        for item in params:
            if isinstance(item, dict):
                out.append(
                    {
                        "name": item.get("name"),
                        "type": item.get("type") or _guess_type(item.get("default")),
                        "default": item.get("default"),
                    }
                )
            elif isinstance(item, str):
                out.append({"name": item, "type": "int", "default": None})
        return out

    return out


def _guess_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


@app.get("/api/indicators")
def api_indicators() -> List[Dict[str, Any]]:
    (REGISTRY,) = _engine("engine.indicators", "REGISTRY")
    out: List[Dict[str, Any]] = []
    try:
        items = REGISTRY.items() if hasattr(REGISTRY, "items") else enumerate(REGISTRY)
    except Exception as exc:
        raise ApiError(500, "지표 목록을 읽지 못했습니다.", f"{type(exc).__name__}: {exc}") from exc

    for key, spec in items:
        key = str(key)
        label = key
        fields = None
        overlay = key.upper() in _OVERLAY_DEFAULT
        if isinstance(spec, dict):
            label = spec.get("label") or spec.get("name") or key
            fields = spec.get("fields")
            if "overlay" in spec:
                overlay = bool(spec.get("overlay"))
        out.append(
            {
                "key": key,
                "label": label,
                "params": _normalize_params(spec),
                "fields": list(fields) if fields else None,
                "overlay": bool(overlay),
            }
        )
    return out


# ==================================================== GET /api/symbols (신설)
_SYMBOLS_CACHE: Dict[str, Any] = {"key": None, "rows": []}
_SYMBOLS_LOCK = threading.Lock()

SYMBOLS_DEFAULT_LIMIT = 50
SYMBOLS_MAX_LIMIT = 500


def _symbols_invalidate() -> None:
    with _SYMBOLS_LOCK:
        _SYMBOLS_CACHE["key"] = None
        _SYMBOLS_CACHE["rows"] = []


def _symbols_cache_key(store: Any) -> str:
    """data_status.json 의 last_sync 와 최신 거래일이 바뀌면 캐시를 버린다."""
    last_sync = str(_read_data_status().get("last_sync") or "")
    try:
        latest = store.latest_date().isoformat()
    except Exception:
        latest = ""
    return f"{last_sync}|{latest}"


def _build_symbol_snapshot(store: Any) -> List[Dict[str, Any]]:
    """최신 거래일 전종목 스냅샷. 그 날 거래된 종목만 담기므로 상장폐지 종목은 빠진다."""
    latest = store.latest_date()
    year_df = store.load_year(latest.year)

    try:
        import pandas as pd  # engine 이 있으면 반드시 존재한다
    except Exception as exc:  # pragma: no cover
        raise ApiError(503, MSG_NO_ENGINE, f"pandas 를 불러올 수 없습니다: {exc}") from exc

    snap = year_df.loc[year_df["Date"] == pd.Timestamp(latest)]
    if snap.empty:
        raise ApiError(
            503,
            MSG_NO_DATA,
            f"최신 거래일({latest.isoformat()}) 스냅샷이 비어 있습니다.",
        )

    wanted = [c for c in ("Code", "Name", "Market", "Marcap") if c in snap.columns]
    snap = snap[wanted]

    last_date = latest.isoformat()
    rows: List[Dict[str, Any]] = []
    for rec in snap.to_dict("records"):
        code = str(rec.get("Code") or "").strip()
        if not code:
            continue
        name = rec.get("Name")
        name = "" if name is None or name != name else str(name).strip()
        marcap = rec.get("Marcap")
        try:
            marcap_eok = (
                None
                if marcap is None or marcap != marcap
                else int(round(float(marcap) / 1e8))
            )
        except Exception:
            marcap_eok = None
        market = rec.get("Market")
        market = "" if market is None or market != market else str(market)
        rows.append(
            {
                "code": code,
                "name": name,
                "market": market,
                "marcap_eok": marcap_eok,
                "last_date": last_date,
            }
        )

    rows.sort(key=lambda r: (r["marcap_eok"] is None, -(r["marcap_eok"] or 0), r["code"]))
    return rows


def get_symbol_rows() -> List[Dict[str, Any]]:
    """최신 거래일 종목 스냅샷 (시가총액 내림차순). 프로세스 메모리에 캐시한다."""
    store = get_store()
    key = _symbols_cache_key(store)
    with _SYMBOLS_LOCK:
        if _SYMBOLS_CACHE["key"] == key and _SYMBOLS_CACHE["rows"]:
            return _SYMBOLS_CACHE["rows"]

    try:
        rows = _build_symbol_snapshot(store)
    except ApiError:
        raise
    except Exception as exc:
        if _is_data_unavailable(exc):
            raise ApiError(503, MSG_NO_DATA, str(exc)) from exc
        raise ApiError(
            500, "종목 목록을 만들지 못했습니다.", f"{type(exc).__name__}: {exc}"
        ) from exc

    with _SYMBOLS_LOCK:
        _SYMBOLS_CACHE["key"] = key
        _SYMBOLS_CACHE["rows"] = rows
    return rows


def _rank(row: Dict[str, Any], q_upper: str) -> Optional[int]:
    """검색 우선순위. 낮을수록 먼저. None 이면 매칭 실패."""
    code = row["code"].upper()
    name = row["name"].upper()
    if code == q_upper:
        return 0
    if name == q_upper:
        return 1
    if name.startswith(q_upper):
        return 2
    if code.startswith(q_upper):
        return 3
    if q_upper in name:
        return 4
    if q_upper in code:
        return 5
    return None


@app.get("/api/symbols")
def api_symbols(q: Optional[str] = None, limit: int = SYMBOLS_DEFAULT_LIMIT) -> Dict[str, Any]:
    """종목 검색. q 없으면 시가총액 상위, 있으면 코드 완전일치 > 이름 시작일치 > 부분일치 순."""
    try:
        limit = int(limit)
    except Exception:
        limit = SYMBOLS_DEFAULT_LIMIT
    limit = max(1, min(limit, SYMBOLS_MAX_LIMIT))

    rows = get_symbol_rows()
    query = (q or "").strip()

    if not query:
        return {"total": len(rows), "symbols": rows[:limit]}

    q_upper = query.upper()
    scored: List[Tuple[int, int, str, Dict[str, Any]]] = []
    for row in rows:
        rank = _rank(row, q_upper)
        if rank is None:
            continue
        scored.append((rank, -(row["marcap_eok"] or 0), row["code"], row))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return {"total": len(scored), "symbols": [item[3] for item in scored[:limit]]}


def default_chart_code(run_id: Optional[str] = None) -> str:
    """code 가 생략됐을 때 쓸 기본 종목: run_id 첫 거래 종목 -> 시가총액 1위."""
    if run_id:
        result = RUNS.get(run_id)
        if result:
            for trade in result.get("trades") or []:
                code = str(trade.get("code") or "").strip()
                if code:
                    return code.zfill(6)
    rows = get_symbol_rows()
    if not rows:
        raise ApiError(503, MSG_NO_DATA, "종목 스냅샷이 비어 있습니다.")
    return rows[0]["code"]


# =================================================== 4-5. POST /api/backtest
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="backtest")
_PROGRESS: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
_PROGRESS_LOCK = threading.Lock()


def _progress_queue(job_id: str) -> "queue.Queue[Dict[str, Any]]":
    with _PROGRESS_LOCK:
        q = _PROGRESS.get(job_id)
        if q is None:
            q = queue.Queue(maxsize=1000)
            _PROGRESS[job_id] = q
        return q


def _progress_drop(job_id: str) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.pop(job_id, None)


def _run_backtest_sync(strategy: Dict[str, Any], job_id: Optional[str]) -> Dict[str, Any]:
    (run_backtest,) = _engine("engine.backtest", "run_backtest")
    store = get_store()

    progress_cb = None
    if job_id:
        q = _progress_queue(job_id)

        def progress_cb(done: int, total: int, message: str = "") -> None:  # noqa: F811
            try:
                q.put_nowait(
                    {
                        "job_id": job_id,
                        "done": int(done),
                        "total": int(total),
                        "message": str(message or ""),
                        "finished": False,
                    }
                )
            except queue.Full:
                pass

    started = time.time()
    result = run_backtest(strategy, store, progress=progress_cb)
    elapsed = round(time.time() - started, 3)

    if not isinstance(result, dict):
        raise ApiError(500, "백테스트 결과 형식이 올바르지 않습니다.", f"type={type(result).__name__}")

    result.setdefault("elapsed_sec", elapsed)
    if not result.get("run_id"):
        result["run_id"] = RUNS.new_run_id()
    result.setdefault("warnings", [])
    RUNS.put(str(result["run_id"]), result)

    if job_id:
        q = _progress_queue(job_id)
        try:
            q.put_nowait(
                {
                    "job_id": job_id,
                    "done": 1,
                    "total": 1,
                    "message": "완료",
                    "finished": True,
                    "run_id": result["run_id"],
                }
            )
        except queue.Full:
            pass
    return result


@app.post("/api/backtest")
async def api_backtest(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ApiError(400, "요청 본문이 JSON 객체가 아닙니다.")

    strategy = body.get("strategy")
    if not isinstance(strategy, dict):
        raise ApiError(400, "strategy 항목이 없습니다.", "요청은 {\"strategy\": <DSL>, \"period\": {...}} 형태여야 합니다.")

    strategy = json.loads(json.dumps(strategy))  # 방어적 복사

    period = body.get("period")
    if isinstance(period, dict) and period:
        merged = dict(strategy.get("period") or {})
        merged.update({k: v for k, v in period.items() if v is not None})
        strategy["period"] = merged

    job_id = body.get("job_id")
    job_id = str(job_id) if job_id else None

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_POOL, _run_backtest_sync, strategy, job_id)
    try:
        result = await asyncio.wait_for(asyncio.shield(future), timeout=BACKTEST_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        if job_id:
            try:
                _progress_queue(job_id).put_nowait(
                    {
                        "job_id": job_id,
                        "done": 0,
                        "total": 0,
                        "message": "시간 초과",
                        "finished": True,
                        "error": True,
                    }
                )
            except queue.Full:
                pass
        raise ApiError(
            504,
            f"백테스트가 {BACKTEST_TIMEOUT_SEC}초 안에 끝나지 않았습니다.",
            "기간을 줄이거나 종목 선정 조건을 좁혀서 다시 실행하세요.",
        ) from exc
    return result


@app.get("/api/backtest/progress/{job_id}")
async def api_backtest_progress(job_id: str) -> StreamingResponse:
    """SSE 진행률. 프런트가 job_id 를 만들어 먼저 열고, 같은 job_id 로 백테스트를 실행한다."""
    q = _progress_queue(job_id)

    async def gen():
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + SSE_IDLE_TIMEOUT_SEC
        yield "retry: 2000\n\n"
        try:
            while True:
                try:
                    item = await loop.run_in_executor(
                        None, functools.partial(q.get, True, 1.0)
                    )
                except queue.Empty:
                    if time.monotonic() > deadline:
                        yield "event: timeout\ndata: {}\n\n"
                        return
                    yield ": keep-alive\n\n"
                    continue
                deadline = time.monotonic() + SSE_IDLE_TIMEOUT_SEC
                yield "data: " + json.dumps(sanitize(item), ensure_ascii=False, allow_nan=False) + "\n\n"
                if item.get("finished"):
                    return
        finally:
            _progress_drop(job_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runs")
def api_runs() -> Dict[str, Any]:
    """(스펙 외 부가) 메모리에 남아 있는 최근 실행 목록. 디버깅용."""
    return {"runs": RUNS.summaries()}


# ====================================================== 4-6. GET /api/chart
def _col(df: Any, *names: str) -> Optional[str]:
    cols = {str(c).lower(): str(c) for c in df.columns}
    for name in names:
        got = cols.get(name.lower())
        if got is not None:
            return got
    return None


def _series(df: Any, name: Optional[str]) -> List[Any]:
    if name is None:
        return [None] * len(df)
    return [sanitize(v) for v in df[name].tolist()]


def _to_yyyymmdd(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int,)) and 19000101 <= value <= 29991231:
        return int(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.year * 10000 + value.month * 100 + value.day
    text = str(value)
    digits = re.sub(r"\D", "", text)[:8]
    if len(digits) == 8:
        try:
            return int(digits)
        except Exception:
            return None
    return None


def _parse_indicator_tokens(spec: str) -> List[Tuple[str, str, List[str]]]:
    """`SMA:20,SMA:45` -> [("SMA:20", "SMA", ["20"]), ("SMA:45", "SMA", ["45"])]"""
    out: List[Tuple[str, str, List[str]]] = []
    for token in (spec or "").split(","):
        token = token.strip()
        if not token:
            continue
        parts = [p.strip() for p in token.split(":")]
        key = parts[0].upper()
        if not key:
            continue
        out.append((token, key, parts[1:]))
    return out


def _coerce(text: str) -> Any:
    try:
        return int(text)
    except Exception:
        pass
    try:
        return float(text)
    except Exception:
        pass
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    return text


def _indicator_params(key: str, args: List[str], registry: Any) -> Dict[str, Any]:
    """위치 인자를 REGISTRY 의 파라미터 순서에 맞춰 이름 있는 dict 로 바꾼다."""
    order: List[str] = []
    defaults: Dict[str, Any] = {}
    spec = None
    try:
        spec = registry.get(key) if hasattr(registry, "get") else None
    except Exception:
        spec = None
    for item in _normalize_params(spec):
        name = item.get("name")
        if name:
            order.append(str(name))
            if item.get("default") is not None:
                defaults[str(name)] = item.get("default")
    if not order:
        order = ["period", "stddev", "source"]

    params = dict(defaults)
    for i, raw in enumerate(args):
        if "=" in raw:
            name, _, value = raw.partition("=")
            params[name.strip()] = _coerce(value.strip())
        elif i < len(order):
            params[order[i]] = _coerce(raw)
    params.setdefault("source", "close")
    return params


def _requested_date(value: Optional[str]) -> Optional[_dt.date]:
    """쿼리로 들어온 날짜 문자열을 date 로. 'auto'/빈값/파싱 실패는 None."""
    text = (value or "").strip()
    if not text or text.lower() == "auto":
        return None
    digits = re.sub(r"\D", "", text)[:8]
    if len(digits) != 8:
        return None
    try:
        return _dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except Exception:
        return None


def _iso_from_yyyymmdd(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    text = str(int(value))
    if len(text) != 8:
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


@app.get("/api/chart")
def api_chart(
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    indicators: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    code = (code or "").strip()
    auto_picked = False
    if not code:
        # 1) run_id 의 첫 거래 종목  2) 시가총액 1위  3) 데이터 없으면 503
        code = default_chart_code(run_id)
        auto_picked = True

    store = get_store()
    try:
        df = store.bars(code, start=start, end=end)
    except Exception as exc:
        if _is_data_unavailable(exc):
            raise ApiError(503, MSG_NO_DATA, str(exc)) from exc
        raise ApiError(500, "차트 데이터를 읽지 못했습니다.", f"{type(exc).__name__}: {exc}") from exc

    if df is None or len(df) == 0:
        raise ApiError(
            404,
            f"'{code}' 종목의 봉 데이터가 없습니다.",
            "종목코드와 기간을 확인하세요.",
        )

    n = int(len(df))

    # ---- t (YYYYMMDD 정수) ----
    date_col = _col(df, "Date")
    if date_col is not None:
        date_values = list(df[date_col])
    else:
        date_values = list(df.index)
    t = [_to_yyyymmdd(v) for v in date_values]

    o_col = _col(df, "Open")
    h_col = _col(df, "High")
    l_col = _col(df, "Low")
    c_col = _col(df, "Close")
    v_col = _col(df, "Volume")
    a_col = _col(df, "Amount")

    name = ""
    name_col = _col(df, "Name")
    if name_col is not None:
        try:
            for value in reversed(df[name_col].tolist()):
                if value is not None and value == value and str(value).strip():
                    name = str(value)
                    break
        except Exception:
            name = ""
    if not name:
        try:
            name = (store.names() or {}).get(code, "") or ""
        except Exception:
            name = ""

    actual_start = _iso_from_yyyymmdd(next((x for x in t if x is not None), None))
    actual_end = _iso_from_yyyymmdd(next((x for x in reversed(t) if x is not None), None))

    payload: Dict[str, Any] = {
        "code": code,
        "name": name,
        "n": n,
        "start": actual_start,
        "end": actual_end,
        "requested": {"start": start, "end": end},
        "truncated": False,
        "t": t,
        "o": _series(df, o_col),
        "h": _series(df, h_col),
        "l": _series(df, l_col),
        "c": _series(df, c_col),
        "v": _series(df, v_col),
        "amt": _series(df, a_col),
        "indicators": {},
        "markers": [],
        "bands": [],
        "levels": [],
    }
    if auto_picked:
        payload["auto_selected"] = True

    # ---- 실제 적용된 범위 vs 요청 범위 ----
    warnings: List[str] = []
    req_start = _requested_date(start)
    req_end = _requested_date(end)
    truncated = False

    if req_start is not None and actual_start and actual_start != req_start.isoformat():
        truncated = True
        if actual_start > req_start.isoformat():
            warnings.append(
                f"요청 시작일({req_start.isoformat()}) 이전 데이터가 없어 {actual_start} 부터 표시합니다."
            )
    if req_end is not None and actual_end and actual_end != req_end.isoformat():
        truncated = True
        if actual_end < req_end.isoformat():
            warnings.append(
                f"요청 종료일({req_end.isoformat()}) 이후 데이터가 없어 {actual_end} 까지만 표시합니다."
            )

    payload["truncated"] = truncated
    if warnings:
        payload.setdefault("warnings", []).extend(warnings)

    # ---- 지표 ----
    tokens = _parse_indicator_tokens(indicators or "")
    if tokens:
        compute, REGISTRY = _engine("engine.indicators", "compute", "REGISTRY")
        for token, key, args in tokens:
            params = _indicator_params(key, args, REGISTRY)
            try:
                values = compute(key, params, df)
            except Exception as exc:
                payload["indicators"][token] = [None] * n
                payload.setdefault("warnings", []).append(
                    f"{token} 지표 계산 실패: {type(exc).__name__}: {exc}"
                )
                continue
            if isinstance(values, dict):
                for field, arr in values.items():
                    payload["indicators"][f"{token}.{field}"] = _fit(arr, n)
            else:
                payload["indicators"][token] = _fit(values, n)

    # ---- run_id 기반 마커/보유구간/기준선 ----
    if run_id:
        _apply_run_overlays(payload, run_id, code, t)

    return payload


def _fit(values: Any, n: int) -> List[Any]:
    """길이를 n 으로 맞추고 NaN 은 None 으로 바꾼다."""
    try:
        seq = values.tolist() if hasattr(values, "tolist") else list(values)
    except Exception:
        return [None] * n
    out = [sanitize(v) for v in seq]
    if len(out) < n:
        out = [None] * (n - len(out)) + out
    elif len(out) > n:
        out = out[-n:]
    return out


def _apply_run_overlays(
    payload: Dict[str, Any], run_id: str, code: str, t: List[Optional[int]]
) -> None:
    result = RUNS.get(run_id)
    if not result:
        payload.setdefault("warnings", []).append(
            f"run_id '{run_id}' 결과가 서버 메모리에 없습니다. 백테스트를 다시 실행하세요."
        )
        return

    index_of: Dict[int, int] = {}
    for i, value in enumerate(t):
        if value is not None and value not in index_of:
            index_of[value] = i

    def idx(date_value: Any) -> Optional[int]:
        key = _to_yyyymmdd(date_value)
        if key is None:
            return None
        if key in index_of:
            return index_of[key]
        # 정확히 없으면 그 이후 첫 거래일로 근사
        candidates = [v for v in index_of if v >= key]
        return index_of[min(candidates)] if candidates else None

    markers: List[Dict[str, Any]] = []
    bands: List[Dict[str, Any]] = []
    levels: List[Dict[str, Any]] = []
    last_i = max(0, len(t) - 1)

    for trade in trades_for_code(result, code):
        ref_i = idx(trade.get("ref_date"))
        ref_open = trade.get("ref_open")
        if ref_i is not None:
            note = ""
            if trade.get("ref_amount_eok") is not None:
                note = f"거래대금 {trade.get('ref_amount_eok')}억"
            markers.append(
                {
                    "i": ref_i,
                    "type": "ref",
                    "price": ref_open,
                    "label": "기준일",
                    "note": note,
                }
            )
            if ref_open is not None:
                levels.append(
                    {
                        "price": ref_open,
                        "from": ref_i,
                        "label": "기준일 시가",
                        "type": "ref_open",
                    }
                )

        first_fill_i: Optional[int] = None
        for fill in trade.get("fills") or []:
            fi = idx(fill.get("date"))
            if fi is None:
                continue
            if first_fill_i is None:
                first_fill_i = fi
            qty = fill.get("qty")
            markers.append(
                {
                    "i": fi,
                    "type": "buy",
                    "price": fill.get("price"),
                    "label": str(fill.get("rule") or "BUY"),
                    "note": f"{qty}주" if qty is not None else "",
                }
            )

        exit_i = idx(trade.get("exit_date"))
        if exit_i is not None:
            markers.append(
                {
                    "i": exit_i,
                    "type": "sell",
                    "price": trade.get("exit_price"),
                    "label": str(trade.get("exit_rule") or "SELL"),
                    "note": str(trade.get("exit_reason") or ""),
                }
            )

        if first_fill_i is not None:
            bands.append(
                {
                    "from": first_fill_i,
                    "to": exit_i if exit_i is not None else last_i,
                    "type": "holding",
                }
            )

    markers.sort(key=lambda m: (m["i"], m.get("type", "")))
    payload["markers"] = markers
    payload["bands"] = bands
    payload["levels"] = levels


# ================================================= 4-7. POST /api/ai/strategy
@app.post("/api/ai/strategy")
async def api_ai_strategy(request: Request) -> Dict[str, Any]:
    """키가 없거나 패키지가 없어도 500 이 아니라 ok:false 로 응답한다."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    prompt = body.get("prompt") or ""
    base = body.get("base")
    if base is not None and not isinstance(base, dict):
        base = None

    try:
        return ai_module.generate_strategy(str(prompt), base)
    except Exception as exc:  # 어떤 경우에도 500 을 내지 않는다.
        traceback.print_exc()
        return {
            "ok": False,
            "error": "AI 전략 생성 중 오류가 발생했습니다.",
            "how_to": "잠시 후 다시 시도하거나 JSON 탭에서 직접 전략을 작성하세요.",
            "detail": f"{type(exc).__name__}: {exc}",
        }


# ====================================================== 정적 파일 (server/web)
_PLACEHOLDER_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KRX Backtester — 준비 중</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#0f1420;color:#e6ebf5;font:16px/1.7 system-ui,'Malgun Gothic',sans-serif}
 .box{max-width:640px;padding:32px 36px;background:#161c2b;border:1px solid #26304a;border-radius:14px}
 h1{margin:0 0 12px;font-size:20px}
 code{background:#0f1420;padding:2px 6px;border-radius:5px;color:#7fd1ff}
 ul{padding-left:20px} a{color:#7fd1ff}
</style></head><body><div class="box">
<h1>화면 파일이 아직 없습니다</h1>
<p>서버는 정상 동작 중입니다. 프런트엔드 파일을 <code>server/web/index.html</code> 에 두면
이 화면 대신 실제 UI 가 표시됩니다.</p>
<ul>
 <li>데이터 상태: <a href="/api/status">/api/status</a></li>
 <li>전략 목록: <a href="/api/strategies">/api/strategies</a></li>
 <li>지표 목록: <a href="/api/indicators">/api/indicators</a></li>
 <li>API 문서: <a href="/api/docs">/api/docs</a></li>
</ul>
</div></body></html>
"""


def _serve_static(rel_path: str) -> Response:
    rel_path = (rel_path or "").strip().lstrip("/")
    if not rel_path or rel_path.endswith("/"):
        rel_path = (rel_path + "index.html") if rel_path else "index.html"

    if not WEB_DIR.is_dir():
        if rel_path == "index.html":
            return Response(content=_PLACEHOLDER_HTML, media_type="text/html; charset=utf-8")
        return error_response(
            404,
            "정적 파일 폴더(server/web)가 아직 없습니다.",
            f"요청 경로: /{rel_path}",
        )

    try:
        target = (WEB_DIR / rel_path).resolve()
        target.relative_to(WEB_DIR.resolve())  # 경로 조작 차단
    except Exception:
        return error_response(403, "허용되지 않는 경로입니다.", f"요청 경로: /{rel_path}")

    if not target.is_file():
        if rel_path == "index.html":
            return Response(content=_PLACEHOLDER_HTML, media_type="text/html; charset=utf-8")
        return error_response(404, "파일을 찾을 수 없습니다.", f"요청 경로: /{rel_path}")

    media, _ = mimetypes.guess_type(str(target))
    media = media or "application/octet-stream"
    if media.startswith("text/") or media in ("application/javascript", "application/json"):
        media = f"{media}; charset=utf-8"
    return Response(content=target.read_bytes(), media_type=media)


@app.get("/", include_in_schema=False)
def index() -> Response:
    return _serve_static("index.html")


@app.get("/{full_path:path}", include_in_schema=False)
def static_files(full_path: str) -> Response:
    if full_path.startswith("api/") or full_path == "api":
        return error_response(404, "존재하지 않는 API 경로입니다.", f"/{full_path}")
    return _serve_static(full_path)
