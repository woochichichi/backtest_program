"""DART OpenAPI 재무 데이터 다운로더.

    python -m tools.fetch_dart --from 2015 --to 2026 [--out dart] [--batch 50]
    python -m tools.fetch_dart --force            # 이미 받은 분기까지 전부 다시
    python -m tools.fetch_dart --selftest         # 키·연결·삼성전자 1건 확인

전자공시(DART) 의 **다중회사 주요계정**(``fnlttMultiAcnt.json``) 을 연도×분기로 훑어
``dart/fundamentals-YYYY.parquet`` 을 만든다. 기업 고유번호 매핑은 ``dart/corp_map.parquet``.

인증키는 코드에 넣지 않는다. ``DART_API_KEY`` 환경변수 → ``dart_key.txt`` 순으로 읽는다.

받은 데이터는 다시 받지 않는다
------------------------------
분기 재무는 한 번 확정되면 거의 바뀌지 않는다. 그래서 **기본 동작이 "이미 받은 분기는 건너뛴다"** 다.

* ``dart/.fetch_state.json`` 에 분기별 수집 결과를 남기고, 완료된 분기는 건너뛴다
* 다만 **가장 최근 2개 분기**(``--refresh-recent``) 는 정정공시·지각제출 때문에 매번 다시 확인한다
* 분기 종료 후 45일(``--disclosure-lag-days``) 이 지나지 않은 분기는 **아예 요청하지 않는다**
  (아직 공시 기간이 아니므로 013 만 잔뜩 돌아온다)
* ``corp_map`` 은 하루 한 번만 받는다
* 하루 호출 20,000건 한도를 스스로 세어 넘기 전에 멈추고, 다음 실행에서 이어받는다
* 전부 다시 받으려면 ``--force``

이 모듈이 지키는 두 가지 규칙 (자세한 배경은 ``docs/DART.md``)
----------------------------------------------------------
1. **룩어헤드 편향 차단** — 재무제표는 분기 종료 후 45~90 일 뒤에 공시된다.
   ``rcept_no`` 앞 8자리(접수일자) 를 ``disclosed_at`` 으로 저장하고,
   조회는 ``engine.dart.DartStore.as_of()`` 로만 한다.
2. **분기 손익은 누적값 차분** — 손익계산서(IS) 금액은 누적이다.
   ``Q2 = 반기누적 - 1분기누적`` 처럼 차분해 ``op_income_quarter`` 를 만든다.
   중간 분기가 비면 ``None`` 으로 두고 "모른다" 로 처리한다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

__all__ = [
    "DartError", "DartStatusError", "AuthError", "NoData", "RateLimitExceeded",
    "BadFieldError", "TooManyCompanies", "MaintenanceError", "NetworkError",
    "BudgetExhausted",
    "read_api_key", "http_get",
    "parse_corp_code_zip", "fetch_corp_codes",
    "parse_multi_acnt", "rows_from_items", "parse_amount",
    "BatchFetcher", "fetch_quarter", "CallBudget",
    "quarterly_from_cumulative", "add_quarter_columns", "to_frame",
    "FetchState", "plan_quarters", "quarter_end", "disclosure_due_date",
    "run_fetch", "selftest", "main",
    "REPRT_CODE_BY_QUARTER", "QUARTER_BY_REPRT_CODE",
    "AMOUNT_COLUMNS", "OUTPUT_COLUMNS", "DAILY_CALL_LIMIT",
]

# ======================================================================================
# 상수
# ======================================================================================

DART_BASE = "https://opendart.fss.or.kr/api"
CORP_CODE_URL = f"{DART_BASE}/corpCode.xml"
MULTI_ACNT_URL = f"{DART_BASE}/fnlttMultiAcnt.json"

USER_AGENT = "krx-backtester/1.0 (+dart fetcher)"

#: 분기 → 보고서 코드
REPRT_CODE_BY_QUARTER: Dict[int, str] = {
    1: "11013",   # 1분기보고서
    2: "11012",   # 반기보고서
    3: "11014",   # 3분기보고서
    4: "11011",   # 사업보고서(연간)
}
QUARTER_BY_REPRT_CODE: Dict[str, int] = {v: k for k, v in REPRT_CODE_BY_QUARTER.items()}

QUARTER_LABEL = {1: "1분기", 2: "반기", 3: "3분기", 4: "사업보고서"}

#: 분기 종료 후 이 일수가 지나야 보고서가 나온다 (자본시장법상 분기/반기 45일, 사업보고서 90일)
DISCLOSURE_LAG_DAYS = 45
ANNUAL_DISCLOSURE_LAG_DAYS = 90

#: DART 응답 status 코드
ST_OK = "000"
ST_NO_KEY = "010"          # 등록되지 않은 인증키
ST_KEY_DISABLED = "011"    # 사용할 수 없는 인증키
ST_IP_DENIED = "012"       # 접근할 수 없는 IP
ST_NO_DATA = "013"         # 조회된 데이터 없음  ← 오류가 아니다
ST_NO_FILE = "014"
ST_RATE_LIMIT = "020"      # 요청 제한 초과 (일 20,000 건)
ST_TOO_MANY_CORP = "021"   # 조회 가능한 회사 개수 초과
ST_BAD_FIELD = "100"       # 필드의 부적절한 값
ST_BAD_ACCESS = "101"
ST_MAINTENANCE = "800"     # 시스템 점검
ST_UNDEFINED = "900"

#: 하루 호출 한도 (DART 공지 기준)
DAILY_CALL_LIMIT = 20_000
#: 남은 호출이 이보다 적으면 경고한다
BUDGET_WARN_AT = 500

#: 다중회사 조회 1회당 최대 회사 수 (status 021 근거). 기본 배치는 이보다 작게 잡는다.
MAX_CORP_PER_CALL = 100

AMOUNT_COLUMNS = [
    "유동자산", "유동부채", "자산총계", "부채총계", "자본총계",
    "매출액", "영업이익", "당기순이익",
]

META_COLUMNS = ["code", "corp_code", "year", "quarter", "disclosed_at", "fs_div", "currency"]
DERIVED_COLUMNS = ["debt_ratio_pct", "current_ratio_pct", "op_income_quarter"]
OUTPUT_COLUMNS = META_COLUMNS + AMOUNT_COLUMNS + DERIVED_COLUMNS

STATE_FILENAME = ".fetch_state.json"
CORP_MAP_FILENAME = "corp_map.parquet"
PARTS_DIRNAME = ".parts"

_CODE_RE = re.compile(r"^\d{6}$")


# ======================================================================================
# 예외
# ======================================================================================


class DartError(Exception):
    """DART 연동 최상위 예외."""


class NetworkError(DartError):
    """DNS/TLS/프록시/타임아웃 등 연결 자체가 실패."""


class BudgetExhausted(DartError):
    """오늘 쓸 수 있는 호출 수를 (우리 쪽 계산으로) 다 썼다. DART 가 020 을 주기 전에 멈춘다."""


class DartStatusError(DartError):
    """DART 가 ``status`` 로 오류를 돌려준 경우."""

    def __init__(self, status: str, message: str = ""):
        self.status = str(status)
        self.dart_message = message or ""
        super().__init__(f"DART status={self.status} {self.dart_message}".strip())


class AuthError(DartStatusError):
    """인증키가 잘못됐거나 사용할 수 없다 (010 / 011 / 012)."""


class NoData(DartStatusError):
    """조회된 데이터가 없다 (013). **오류가 아니다** — 조용히 건너뛴다."""


class RateLimitExceeded(DartStatusError):
    """일일 요청 한도 초과 (020). 즉시 중단하고 상태를 저장한다."""


class BadFieldError(DartStatusError):
    """필드 값이 부적절하다 (100). 배치가 너무 크면 여기로 떨어진다."""


class TooManyCompanies(DartStatusError):
    """조회 가능한 회사 개수 초과 (021). 배치를 줄여 재시도한다."""


class MaintenanceError(DartStatusError):
    """시스템 점검 중 (800)."""


_STATUS_EXC = {
    ST_NO_KEY: AuthError,
    ST_KEY_DISABLED: AuthError,
    ST_IP_DENIED: AuthError,
    ST_NO_DATA: NoData,
    ST_RATE_LIMIT: RateLimitExceeded,
    ST_TOO_MANY_CORP: TooManyCompanies,
    ST_BAD_FIELD: BadFieldError,
    ST_MAINTENANCE: MaintenanceError,
}

#: 사용자에게 보여줄 한국어 안내
STATUS_HELP = {
    ST_NO_KEY: "등록되지 않은 인증키입니다. dart_key.txt 의 값을 다시 확인하세요.",
    ST_KEY_DISABLED: "사용할 수 없는 인증키입니다. DART 사이트에서 키 상태를 확인하세요.",
    ST_IP_DENIED: "접근할 수 없는 IP 입니다. 회사 방화벽/프록시 환경이면 개인 네트워크에서 시도하세요.",
    ST_NO_DATA: "조회된 데이터가 없습니다. (해당 분기 보고서를 제출하지 않은 회사입니다)",
    ST_RATE_LIMIT: "오늘 호출 한도(20,000건)를 다 썼습니다. 내일 이어받으세요.",
    ST_TOO_MANY_CORP: "한 번에 조회 가능한 회사 개수를 넘었습니다. 배치 크기를 줄이세요.",
    ST_BAD_FIELD: "요청 필드 값이 부적절합니다. 배치 크기를 줄여 재시도합니다.",
    ST_BAD_ACCESS: "부적절한 접근입니다.",
    ST_MAINTENANCE: "DART 시스템 점검 중입니다. 잠시 후 다시 시도하세요.",
    ST_UNDEFINED: "DART 에서 정의되지 않은 오류가 돌아왔습니다.",
}


def raise_for_status(status: str, message: str = "") -> None:
    """``status`` 가 정상(000)이 아니면 대응하는 예외를 던진다."""
    s = str(status or "").strip()
    if s == ST_OK:
        return
    exc = _STATUS_EXC.get(s, DartStatusError)
    raise exc(s, message or STATUS_HELP.get(s, ""))


# ======================================================================================
# 인증키
# ======================================================================================

KEY_FILENAME = "dart_key.txt"

KEY_MISSING_HELP = f"""\
DART 인증키를 찾을 수 없습니다.

  1) https://opendart.fss.or.kr 에서 회원가입 후 '오픈API 인증키 신청' 을 하세요.
  2) 발급받은 40자리 키를 이 폴더의 {KEY_FILENAME} 파일에 한 줄로 저장하세요.
     (또는 환경변수 DART_API_KEY 에 설정)

  {KEY_FILENAME} 은 .gitignore 에 들어 있어 커밋되지 않습니다."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_api_key(
    key_file: str | os.PathLike | None = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """``DART_API_KEY`` 환경변수 → ``dart_key.txt`` 순으로 인증키를 읽는다.

    둘 다 없으면 ``None``. **키를 로그에 그대로 찍지 마라** (``mask_key`` 를 써라).
    """
    env = os.environ if env is None else env
    v = (env.get("DART_API_KEY") or "").strip()
    if v:
        return v

    candidates: List[Path] = []
    if key_file is not None:
        candidates.append(Path(key_file))
    else:
        candidates += [_project_root() / KEY_FILENAME, Path(KEY_FILENAME)]

    for p in candidates:
        try:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def mask_key(key: Optional[str]) -> str:
    """로그·화면 출력용. ``abcd****wxyz (40자)`` 형태로 가린다."""
    if not key:
        return "(없음)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]} ({len(key)}자)"


# ======================================================================================
# HTTP
# ======================================================================================


def http_get(url: str, params: Dict[str, str], timeout: float = 30.0) -> bytes:
    """GET 요청의 본문을 bytes 로 돌려준다. 테스트에서는 이 함수를 대체(fetch=) 한다."""
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:  # pragma: no cover - 네트워크
        body = ""
        try:
            body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            pass
        raise NetworkError(f"HTTP {e.code} {e.reason} {body}".strip()) from e
    except urllib.error.URLError as e:  # pragma: no cover - 네트워크
        raise NetworkError(f"연결 실패: {e.reason}") from e
    except OSError as e:  # pragma: no cover - 네트워크
        raise NetworkError(f"연결 실패: {e}") from e


Fetcher = Callable[[str, Dict[str, str]], bytes]


def _default_fetch(url: str, params: Dict[str, str]) -> bytes:
    return http_get(url, params)


# ======================================================================================
# 1. 기업 고유번호 매핑 (corpCode.xml)
# ======================================================================================


def parse_corp_code_zip(blob: bytes) -> pd.DataFrame:
    """corpCode.xml ZIP 응답을 파싱한다.

    ``stock_code`` 가 빈 문자열이면 **비상장**이므로 버린다.
    반환 컬럼: ``corp_code``(8자리) / ``corp_name`` / ``code``(6자리 종목코드) / ``modify_date``
    """
    if not blob:
        raise DartError("corpCode 응답이 비어 있습니다.")

    # ZIP 이 아니라 XML 오류 본문이 그대로 오는 경우가 있다.
    if blob[:2] != b"PK":
        status, message = _try_read_status_xml(blob)
        if status is not None:
            raise_for_status(status, message)
        raise DartError("corpCode 응답이 ZIP 이 아닙니다. 인증키/네트워크를 확인하세요.")

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise DartError("ZIP 안에 XML 이 없습니다.")
            xml = z.read(names[0])
    except zipfile.BadZipFile as e:
        raise DartError(f"corpCode ZIP 을 열 수 없습니다: {e}") from e

    root = ET.fromstring(xml)
    rows: List[dict] = []
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not _CODE_RE.match(stock):
            continue  # 비상장(공백) 또는 형식 이상
        corp = (el.findtext("corp_code") or "").strip()
        if not corp:
            continue
        rows.append(
            {
                "corp_code": corp.zfill(8),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "code": stock,
                "modify_date": (el.findtext("modify_date") or "").strip(),
            }
        )

    df = pd.DataFrame(rows, columns=["corp_code", "corp_name", "code", "modify_date"])
    if not df.empty:
        # 같은 종목코드가 여러 corp_code 에 붙는 경우 최신 modify_date 를 남긴다.
        df = (
            df.sort_values(["code", "modify_date"], kind="stable")
            .drop_duplicates(subset="code", keep="last")
            .sort_values("code", kind="stable")
            .reset_index(drop=True)
        )
    return df


def _try_read_status_xml(blob: bytes) -> Tuple[Optional[str], str]:
    """``<result><status>013</status><message>..</message></result>`` 를 읽어본다."""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return None, ""
    status = root.findtext("status")
    message = root.findtext("message") or ""
    return (status.strip() if status else None), message.strip()


def fetch_corp_codes(key: str, fetch: Optional[Fetcher] = None) -> pd.DataFrame:
    """corpCode.xml 을 받아 상장사 매핑 DataFrame 으로 돌려준다."""
    fetch = fetch or _default_fetch
    blob = fetch(CORP_CODE_URL, {"crtfc_key": key})
    return parse_corp_code_zip(blob)


# ======================================================================================
# 2. 다중회사 주요계정 (fnlttMultiAcnt.json)
# ======================================================================================


def parse_multi_acnt(payload) -> List[dict]:
    """``fnlttMultiAcnt.json`` 응답을 리스트로 돌려준다.

    - ``status == "000"`` → ``list``
    - ``status == "013"`` → ``[]``  (데이터 없음은 오류가 아니다)
    - 그 밖의 status → 대응 예외
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(payload).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as e:
            raise DartError(f"JSON 응답을 해석할 수 없습니다: {e}") from e
    elif isinstance(payload, str):
        payload = json.loads(payload)

    if not isinstance(payload, dict):
        raise DartError("fnlttMultiAcnt 응답 형식이 올바르지 않습니다.")

    status = str(payload.get("status", "")).strip()
    message = str(payload.get("message", "")).strip()
    if status == ST_NO_DATA:
        return []
    raise_for_status(status, message)
    items = payload.get("list") or []
    if not isinstance(items, list):
        raise DartError("fnlttMultiAcnt 의 list 가 배열이 아닙니다.")
    return items


# --- 계정명 정규화 -------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")

#: 정규화된 account_nm → 저장 컬럼
_ACCOUNT_EXACT = {
    "유동자산": "유동자산",
    "유동부채": "유동부채",
    "자산총계": "자산총계",
    "부채총계": "부채총계",
    "자본총계": "자본총계",
    "매출액": "매출액",
    "수익(매출액)": "매출액",
    "영업수익": "매출액",
    "영업이익": "영업이익",
    "영업이익(손실)": "영업이익",
    "당기순이익": "당기순이익",
    "당기순이익(손실)": "당기순이익",
}

_ACCOUNT_PREFIX = [
    ("영업이익", "영업이익"),
    ("당기순이익", "당기순이익"),
    ("매출액", "매출액"),
]


def normalize_account(name) -> Optional[str]:
    """``account_nm`` 을 저장 컬럼명으로 정규화한다. 관심 없는 계정은 ``None``."""
    if not name:
        return None
    s = _WS_RE.sub("", str(name))
    hit = _ACCOUNT_EXACT.get(s)
    if hit:
        return hit
    for prefix, col in _ACCOUNT_PREFIX:
        if s.startswith(prefix):
            return col
    return None


def parse_amount(v) -> Optional[int]:
    """``thstrm_amount`` 같은 문자열 금액을 원 단위 int 로. 비었으면 ``None``."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return None if pd.isna(v) else int(v)
    s = str(v).strip().replace(",", "").replace(" ", "").replace(" ", "")
    if s in ("", "-", "--", "N/A", "NaN", "nan", "None"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    while s[:1] in ("△", "▲", "-", "−"):
        neg = True
        s = s[1:]
    if not s:
        return None
    try:
        val = int(round(float(s)))
    except ValueError:
        return None
    return -val if neg else val


def _receipt_date(rcept_no) -> Optional[str]:
    """``rcept_no`` 앞 8자리 = 접수일자(YYYYMMDD) → ``YYYY-MM-DD``.

    **룩어헤드 편향 차단의 핵심.** 이 날짜 이전에는 그 재무를 알 수 없었다.
    """
    s = re.sub(r"\D", "", str(rcept_no or ""))
    if len(s) < 8:
        return None
    y, m, d = s[0:4], s[4:6], s[6:8]
    try:
        dt.date(int(y), int(m), int(d))
    except ValueError:
        return None
    return f"{y}-{m}-{d}"


def rows_from_items(
    items: Iterable[dict],
    year: int,
    quarter: int,
    corp_to_code: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """응답 항목들을 회사별 1행으로 접는다.

    - ``fs_div`` 는 **CFS(연결) 우선, 없으면 OFS(별도)**
    - 금액은 ``thstrm_amount`` (당기금액) 만 쓴다
    - ``disclosed_at`` 은 ``rcept_no`` 앞 8자리
    """
    by_corp: Dict[str, Dict[str, List[dict]]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        corp = str(it.get("corp_code") or "").strip().zfill(8)
        if not corp or corp == "0" * 8:
            continue
        fs = str(it.get("fs_div") or "").strip().upper() or "OFS"
        by_corp.setdefault(corp, {}).setdefault(fs, []).append(it)

    out: List[dict] = []
    for corp, groups in by_corp.items():
        chosen_fs = "CFS" if groups.get("CFS") else ("OFS" if groups.get("OFS") else None)
        if chosen_fs is None:
            # CFS/OFS 가 아닌 값이 오면 첫 그룹을 그대로 쓴다.
            chosen_fs = next(iter(groups))
        rows = groups[chosen_fs]

        row: Dict[str, object] = {c: None for c in OUTPUT_COLUMNS}
        row["corp_code"] = corp
        row["year"] = int(year)
        row["quarter"] = int(quarter)
        row["fs_div"] = chosen_fs

        code = ""
        currency = ""
        rcept: List[str] = []
        for it in rows:
            col = normalize_account(it.get("account_nm"))
            if col is not None and row.get(col) is None:
                row[col] = parse_amount(it.get("thstrm_amount"))
            sc = str(it.get("stock_code") or "").strip()
            if not code and _CODE_RE.match(sc):
                code = sc
            cur = str(it.get("currency") or "").strip()
            if not currency and cur:
                currency = cur
            rn = str(it.get("rcept_no") or "").strip()
            if rn:
                rcept.append(rn)

        if not code and corp_to_code:
            code = corp_to_code.get(corp, "") or ""
        if not _CODE_RE.match(code or ""):
            continue  # 종목코드를 붙일 수 없으면 marcap 과 이을 수 없다

        row["code"] = code
        row["currency"] = currency or "KRW"
        row["disclosed_at"] = _receipt_date(max(rcept)) if rcept else None
        out.append(row)

    out.sort(key=lambda r: str(r["code"]))
    return out


# ======================================================================================
# 3. 호출 예산 (하루 20,000건 자체 추적)
# ======================================================================================


class CallBudget:
    """오늘 남은 호출 수를 세어 DART 가 020 을 주기 전에 우리가 먼저 멈춘다."""

    def __init__(self, used: int = 0, limit: int = DAILY_CALL_LIMIT):
        self.used = max(0, int(used))
        self.limit = int(limit)
        self.spent_this_run = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def check(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExhausted(
                f"오늘 호출 한도({self.limit:,}건)에 도달했습니다. 내일 이어받으세요."
            )

    def spend(self, n: int = 1) -> None:
        self.check(n)
        self.used += n
        self.spent_this_run += n


# ======================================================================================
# 4. 배치 조회 (적응형)
# ======================================================================================


class BatchFetcher:
    """corp_code 를 묶어 ``fnlttMultiAcnt`` 를 호출한다.

    ``100``(필드 부적절) 또는 ``021``(회사 개수 초과) 이 오면 **배치를 절반으로 줄여
    같은 구간을 재시도**하고, 줄어든 크기를 이후 호출에도 유지한다.
    ``020``(요청 한도 초과) 이면 ``RateLimitExceeded`` 를 그대로 올려 즉시 중단시킨다.
    """

    def __init__(
        self,
        key: str,
        batch: int = 50,
        fetch: Optional[Fetcher] = None,
        sleep: float = 0.0,
        min_batch: int = 1,
        budget: Optional[CallBudget] = None,
    ):
        self.key = key
        self.batch = max(1, min(int(batch), MAX_CORP_PER_CALL))
        self.min_batch = max(1, int(min_batch))
        self.fetch = fetch or _default_fetch
        self.sleep = float(sleep)
        self.budget = budget
        self.calls = 0

    # -- 단건 호출 -------------------------------------------------------------------
    def _call(self, corp_codes: Sequence[str], year: int, quarter: int) -> List[dict]:
        if self.budget is not None:
            self.budget.spend(1)
        params = {
            "crtfc_key": self.key,
            "corp_code": ",".join(corp_codes),
            "bsns_year": str(int(year)),
            "reprt_code": REPRT_CODE_BY_QUARTER[int(quarter)],
        }
        self.calls += 1
        blob = self.fetch(MULTI_ACNT_URL, params)
        if self.sleep:
            time.sleep(self.sleep)
        return parse_multi_acnt(blob)

    # -- 구간 호출 -------------------------------------------------------------------
    def fetch_all(
        self,
        corp_codes: Sequence[str],
        year: int,
        quarter: int,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[dict]:
        codes = list(corp_codes)
        items: List[dict] = []
        i = 0
        while i < len(codes):
            size = min(self.batch, len(codes) - i)
            chunk = codes[i : i + size]
            try:
                items.extend(self._call(chunk, year, quarter))
            except (BadFieldError, TooManyCompanies) as e:
                if self.batch <= self.min_batch or size <= self.min_batch:
                    raise
                self.batch = max(self.min_batch, self.batch // 2)
                print(
                    f"        [조정] 요청이 거부되어(status={e.status}) 배치 크기를 "
                    f"{self.batch}개로 줄여 다시 시도합니다.",
                    flush=True,
                )
                continue  # 같은 위치에서 더 작은 배치로 재시도
            i += size
            if on_progress is not None:
                on_progress(min(i, len(codes)), len(codes))
        return items


def fetch_quarter(
    key: str,
    corp_codes: Sequence[str],
    year: int,
    quarter: int,
    batch: int = 50,
    fetch: Optional[Fetcher] = None,
    sleep: float = 0.0,
    on_progress: Optional[Callable[[int, int], None]] = None,
    budget: Optional[CallBudget] = None,
) -> Tuple[List[dict], int, int]:
    """한 (연도, 분기) 를 전부 훑는다. ``(items, 최종 배치크기, 호출 수)``."""
    bf = BatchFetcher(key, batch=batch, fetch=fetch, sleep=sleep, budget=budget)
    items = bf.fetch_all(corp_codes, year, quarter, on_progress=on_progress)
    return items, bf.batch, bf.calls


# ======================================================================================
# 5. 분기 차분 · 파생 지표
# ======================================================================================


def quarterly_from_cumulative(
    cumulative: Dict[int, Optional[int]],
    fs_div: Optional[Dict[int, Optional[str]]] = None,
) -> Dict[int, Optional[int]]:
    """누적 손익 → 분기 손익.

        Q1 = 1분기 누적
        Q2 = 반기 누적   - 1분기 누적
        Q3 = 3분기 누적  - 반기 누적
        Q4 = 연간(사업)  - 3분기 누적

    직전 분기가 없으면 **``None``**(모른다). 임의로 0 이나 흑자로 채우지 않는다.
    ``fs_div`` 를 주면 연결/별도 기준이 다른 분기끼리는 차분하지 않는다(``None``).
    """
    out: Dict[int, Optional[int]] = {}
    for q in (1, 2, 3, 4):
        cur = cumulative.get(q)
        if cur is None:
            out[q] = None
            continue
        if q == 1:
            out[q] = int(cur)
            continue
        prev = cumulative.get(q - 1)
        if prev is None:
            out[q] = None
            continue
        if fs_div is not None and fs_div.get(q) != fs_div.get(q - 1):
            out[q] = None
            continue
        out[q] = int(cur) - int(prev)
    return out


def _ratio(numer, denom) -> Optional[float]:
    n = _as_opt_int(numer)
    d = _as_opt_int(denom)
    if n is None or d is None or d <= 0:
        # 자본잠식(자본총계<=0)·분모 0 은 "계산 불가" 로 둔다.
        # 필터에서는 값이 없으므로 보수적으로 제외된다.
        return None
    return round(n / d * 100.0, 2)


def add_quarter_columns(df: pd.DataFrame) -> pd.DataFrame:
    """``debt_ratio_pct`` / ``current_ratio_pct`` / ``op_income_quarter`` 를 채운다.

    ``op_income_quarter`` 는 **같은 회사·같은 연도 안에서** 누적 영업이익을 차분한 값이다.
    """
    if df.empty:
        return df

    out = df.copy()
    out["debt_ratio_pct"] = [_ratio(a, b) for a, b in zip(out["부채총계"], out["자본총계"])]
    out["current_ratio_pct"] = [_ratio(a, b) for a, b in zip(out["유동자산"], out["유동부채"])]

    q_values: List[Optional[int]] = [None] * len(out)
    positions: Dict[Tuple[str, int], Dict[int, int]] = {}
    for pos, (corp, year, quarter) in enumerate(
        zip(out["corp_code"], out["year"], out["quarter"])
    ):
        positions.setdefault((str(corp), int(year)), {})[int(quarter)] = pos

    op = list(out["영업이익"])
    fs = list(out["fs_div"])
    for qmap in positions.values():
        cum = {q: _as_opt_int(op[p]) for q, p in qmap.items()}
        fsm = {q: fs[p] for q, p in qmap.items()}
        diffed = quarterly_from_cumulative(cum, fsm)
        for q, p in qmap.items():
            q_values[p] = diffed.get(q)

    out["op_income_quarter"] = pd.array(q_values, dtype="Int64")
    return out


def _as_opt_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_frame(rows: Sequence[dict]) -> pd.DataFrame:
    """행 리스트를 저장 규격 DataFrame 으로. 금액은 nullable ``Int64`` (원 단위)."""
    rows = list(rows)
    if rows:
        df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    else:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in OUTPUT_COLUMNS})
    for c in ("code", "corp_code", "fs_div", "currency"):
        df[c] = df[c].astype("string")
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int32")
    df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce").astype("Int8")
    df["disclosed_at"] = pd.to_datetime(df["disclosed_at"], errors="coerce")
    for c in AMOUNT_COLUMNS + ["op_income_quarter"]:
        df[c] = pd.array([_as_opt_int(v) for v in df[c]], dtype="Int64")
    for c in ("debt_ratio_pct", "current_ratio_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df[OUTPUT_COLUMNS]


# ======================================================================================
# 6. 공시 시점 — 아직 나오지 않은 분기는 요청하지 않는다
# ======================================================================================


def quarter_end(year: int, quarter: int) -> dt.date:
    """분기 종료일. Q1=3/31, Q2=6/30, Q3=9/30, Q4=12/31."""
    return {
        1: dt.date(int(year), 3, 31),
        2: dt.date(int(year), 6, 30),
        3: dt.date(int(year), 9, 30),
        4: dt.date(int(year), 12, 31),
    }[int(quarter)]


def disclosure_due_date(
    year: int,
    quarter: int,
    lag_days: int = DISCLOSURE_LAG_DAYS,
    annual_lag_days: Optional[int] = None,
) -> dt.date:
    """그 분기 보고서를 조회할 만한 가장 이른 날짜.

    분기·반기보고서는 분기 종료 후 45일, 사업보고서(Q4)는 90일이 법정 기한이다.
    ``annual_lag_days`` 를 주지 않으면 Q4 에도 ``lag_days`` 를 그대로 쓴다
    (``--disclosure-lag-days`` 로 한 값만 조절할 때).
    """
    lag = int(lag_days)
    if int(quarter) == 4 and annual_lag_days is not None:
        lag = int(annual_lag_days)
    return quarter_end(year, quarter) + dt.timedelta(days=lag)


#: 계획 항목 사유
PLAN_FETCH = "fetch"
PLAN_RECENT = "recent"
PLAN_DONE = "done"
PLAN_NOT_DUE = "not_due"

PLAN_REASON_TEXT = {
    PLAN_DONE: "이미 받음",
    PLAN_NOT_DUE: "아직 공시 기간이 아님",
    PLAN_RECENT: "최근 분기 재확인",
    PLAN_FETCH: "받는 중",
}


def plan_quarters(
    year_from: int,
    year_to: int,
    state: "FetchState",
    today: Optional[dt.date] = None,
    force: bool = False,
    refresh_recent: int = 2,
    lag_days: int = DISCLOSURE_LAG_DAYS,
    annual_lag_days: Optional[int] = ANNUAL_DISCLOSURE_LAG_DAYS,
) -> List[dict]:
    """받을 (연도, 분기) 목록과 각각의 사유를 만든다.

    반환 원소: ``{"year","quarter","action","reason","rows","due"}``
    ``action`` 은 ``"fetch"`` 또는 ``"skip"``.
    """
    today = today or dt.date.today()

    due: List[Tuple[int, int, dt.date]] = []
    not_due: List[Tuple[int, int, dt.date]] = []
    for y in range(int(year_from), int(year_to) + 1):
        for q in (1, 2, 3, 4):
            d = disclosure_due_date(y, q, lag_days, annual_lag_days)
            (due if today >= d else not_due).append((y, q, d))

    n_recent = max(0, int(refresh_recent))
    recent = {(y, q) for y, q, _ in sorted(due)[-n_recent:]} if n_recent else set()
    not_due_set = {(y, q) for y, q, _ in not_due}

    plan: List[dict] = []
    for y, q, d in sorted(due + not_due):
        rec = state.quarter(y, q)
        rows = int(rec.get("rows", 0)) if rec else 0
        if (y, q) in not_due_set:
            plan.append({"year": y, "quarter": q, "action": "skip",
                         "reason": PLAN_NOT_DUE, "rows": rows, "due": d})
            continue
        if force:
            plan.append({"year": y, "quarter": q, "action": "fetch",
                         "reason": PLAN_FETCH, "rows": rows, "due": d})
            continue
        if (y, q) in recent:
            plan.append({"year": y, "quarter": q, "action": "fetch",
                         "reason": PLAN_RECENT, "rows": rows, "due": d})
            continue
        if rec and rec.get("complete"):
            plan.append({"year": y, "quarter": q, "action": "skip",
                         "reason": PLAN_DONE, "rows": rows, "due": d})
            continue
        plan.append({"year": y, "quarter": q, "action": "fetch",
                     "reason": PLAN_FETCH, "rows": rows, "due": d})
    return plan


# ======================================================================================
# 7. 진행 상태 (이어받기 · 호출 수 추적)
# ======================================================================================


class FetchState:
    """``dart/.fetch_state.json``.

    ```json
    {
      "version": 2,
      "quarters": {"2024-4": {"fetched_at": "...", "rows": 2681, "corps": 2712, "complete": true}},
      "corp_map_fetched_at": "2026-08-05 11:58:00",
      "api_calls_today": 1084,
      "api_calls_date": "2026-08-05"
    }
    ```
    """

    VERSION = 2

    def __init__(self, path: str | os.PathLike, today: Optional[dt.date] = None):
        self.path = Path(path)
        self.today = today or dt.date.today()
        self.data: Dict[str, object] = {
            "version": self.VERSION,
            "quarters": {},
            "corp_map_fetched_at": None,
            "api_calls_today": 0,
            "api_calls_date": self.today.isoformat(),
        }
        self.load()

    # -- 키 ---------------------------------------------------------------------------
    @staticmethod
    def key(year: int, quarter: int) -> str:
        return f"{int(year)}-{int(quarter)}"

    # -- 입출력 -----------------------------------------------------------------------
    def load(self) -> "FetchState":
        try:
            if not self.path.is_file():
                return self
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self
        if not isinstance(raw, dict):
            return self

        quarters = raw.get("quarters")
        if not isinstance(quarters, dict):
            # v1 포맷(done) 에서 올라온 경우 — 완료 표시만 살린다.
            quarters = {}
            for k, v in (raw.get("done") or {}).items():
                m = re.match(r"^(\d{4})Q([1-4])$", str(k))
                if not m:
                    continue
                rows = int(v.get("rows", 0)) if isinstance(v, dict) else 0
                quarters[self.key(int(m.group(1)), int(m.group(2)))] = {
                    "fetched_at": (v or {}).get("at") if isinstance(v, dict) else None,
                    "rows": rows,
                    "corps": None,
                    "complete": True,
                }
        self.data["quarters"] = quarters
        self.data["corp_map_fetched_at"] = raw.get("corp_map_fetched_at")

        # 날짜가 바뀌면 호출 수는 0 으로 리셋된다.
        prev_date = str(raw.get("api_calls_date") or "")
        if prev_date == self.today.isoformat():
            self.data["api_calls_today"] = int(raw.get("api_calls_today") or 0)
            self.data["api_calls_date"] = prev_date
        else:
            self.data["api_calls_today"] = 0
            self.data["api_calls_date"] = self.today.isoformat()
        return self

    def save(self) -> None:
        self.data["version"] = self.VERSION
        self.data["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:  # pragma: no cover
            print(f"  [경고] 진행 상태를 저장하지 못했습니다: {e}", flush=True)

    # -- 분기 -------------------------------------------------------------------------
    def quarter(self, year: int, quarter: int) -> Optional[dict]:
        rec = (self.data.get("quarters") or {}).get(self.key(year, quarter))
        return rec if isinstance(rec, dict) else None

    def mark_quarter(self, year: int, quarter: int, rows: int, corps: int,
                     complete: bool = True) -> None:
        self.data.setdefault("quarters", {})[self.key(year, quarter)] = {
            "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows": int(rows),
            "corps": int(corps),
            "complete": bool(complete),
        }

    # -- corp_map ---------------------------------------------------------------------
    def corp_map_is_fresh(self) -> bool:
        """오늘 이미 corp_map 을 받았는가."""
        v = str(self.data.get("corp_map_fetched_at") or "")
        return v[:10] == self.today.isoformat()

    def mark_corp_map(self) -> None:
        self.data["corp_map_fetched_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -- 호출 수 ----------------------------------------------------------------------
    @property
    def api_calls_today(self) -> int:
        if str(self.data.get("api_calls_date") or "") != self.today.isoformat():
            return 0
        return int(self.data.get("api_calls_today") or 0)

    def set_api_calls(self, used: int) -> None:
        self.data["api_calls_date"] = self.today.isoformat()
        self.data["api_calls_today"] = int(used)

    def budget(self, limit: int = DAILY_CALL_LIMIT) -> CallBudget:
        return CallBudget(used=self.api_calls_today, limit=limit)


# ======================================================================================
# 8. 실행
# ======================================================================================


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds != seconds or seconds <= 0:
        return "계산 중"
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60}초"
    return f"{s // 3600}시간 {(s % 3600) // 60}분"


def _load_corp_map(out_dir: Path) -> Optional[pd.DataFrame]:
    p = out_dir / CORP_MAP_FILENAME
    if not p.is_file():
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:  # pragma: no cover
        return None
    return df if not df.empty else None


def _part_path(out_dir: Path, year: int, quarter: int) -> Path:
    return out_dir / PARTS_DIRNAME / f"fundamentals-{int(year)}-Q{int(quarter)}.parquet"


def _rebuild_year(out_dir: Path, year: int) -> int:
    """그 해의 분기 파트들을 모아 ``fundamentals-YYYY.parquet`` 를 다시 만든다."""
    frames = []
    for q in (1, 2, 3, 4):
        p = _part_path(out_dir, year, q)
        if not p.is_file():
            continue
        try:
            f = pd.read_parquet(p)
        except Exception:  # pragma: no cover
            continue
        if not f.empty:
            frames.append(f)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df = add_quarter_columns(df)
    df = df.sort_values(["code", "year", "quarter"], kind="stable").reset_index(drop=True)
    df.to_parquet(out_dir / f"fundamentals-{int(year)}.parquet", index=False)
    return len(df)


def run_fetch(
    key: str,
    year_from: int,
    year_to: int,
    out_dir: str | os.PathLike = "dart",
    batch: int = 50,
    force: bool = False,
    refresh_recent: int = 2,
    fetch: Optional[Fetcher] = None,
    sleep: float = 0.0,
    refresh_corp_map: bool = False,
    today: Optional[dt.date] = None,
    lag_days: int = DISCLOSURE_LAG_DAYS,
    annual_lag_days: Optional[int] = ANNUAL_DISCLOSURE_LAG_DAYS,
    call_limit: int = DAILY_CALL_LIMIT,
) -> int:
    """전체 수집 절차. 종료 코드를 돌려준다.

    0 정상 / 1 오류 / 4 인증키 / 5 네트워크 / 6 한도초과 / 7 점검중
    """
    today = today or dt.date.today()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / PARTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    state = FetchState(out / STATE_FILENAME, today=today)
    budget = state.budget(call_limit)

    print(f"오늘 이미 쓴 DART 호출: {budget.used:,}건 / {budget.limit:,}건 "
          f"(남은 {budget.remaining:,}건)", flush=True)
    print("", flush=True)

    # ---- 1) 기업 고유번호 -----------------------------------------------------------
    corp_df = None if (refresh_corp_map or force) else _load_corp_map(out)
    if corp_df is not None and not (refresh_corp_map or force) and state.corp_map_is_fresh():
        print(f"[1단계] 기업 고유번호  건너뜀 (오늘 이미 받음, {len(corp_df):,}개)", flush=True)
    elif corp_df is not None and not (refresh_corp_map or force):
        print(f"[1단계] 기업 고유번호  기존 파일 사용 ({len(corp_df):,}개)", flush=True)
        print("        새로 받으려면 --refresh-corp-map 을 주세요.", flush=True)
    else:
        print("[1단계] 기업 고유번호(corpCode) 를 내려받는 중...", flush=True)
        try:
            budget.spend(1)
            corp_df = fetch_corp_codes(key, fetch=fetch)
        except BudgetExhausted as e:
            print(f"  [중단] {e}", flush=True)
            state.set_api_calls(budget.used)
            state.save()
            return 6
        except AuthError as e:
            print(f"  [중단] 인증키 오류입니다: {e.dart_message or e.status}", flush=True)
            state.set_api_calls(budget.used)
            state.save()
            return 4
        except RateLimitExceeded:
            print("  [중단] 오늘 DART 호출 한도를 다 썼습니다. 내일 이어받으세요.", flush=True)
            state.set_api_calls(budget.limit)
            state.save()
            return 6
        except MaintenanceError:
            print("  [중단] DART 시스템 점검 중입니다.", flush=True)
            state.set_api_calls(budget.used)
            state.save()
            return 7
        except NetworkError as e:
            print(f"  [중단] 네트워크 오류입니다: {e}", flush=True)
            state.set_api_calls(budget.used)
            state.save()
            return 5
        corp_df.to_parquet(out / CORP_MAP_FILENAME, index=False)
        state.mark_corp_map()
        state.set_api_calls(budget.used)
        state.save()
        print(f"        상장사 {len(corp_df):,}개를 {out / CORP_MAP_FILENAME} 에 저장했습니다.",
              flush=True)

    corp_codes = [str(c) for c in corp_df["corp_code"].tolist()]
    corp_to_code = dict(zip(corp_df["corp_code"].astype(str), corp_df["code"].astype(str)))
    if not corp_codes:
        print("[오류] 상장사 목록이 비어 있습니다. 인증키와 응답을 확인하세요.", flush=True)
        return 1

    # ---- 2) 받을 분기 계획 -----------------------------------------------------------
    plan = plan_quarters(
        year_from, year_to, state, today=today, force=force,
        refresh_recent=refresh_recent, lag_days=lag_days, annual_lag_days=annual_lag_days,
    )
    targets = [p for p in plan if p["action"] == "fetch"]
    skipped = [p for p in plan if p["action"] == "skip"]

    per_quarter_calls = math.ceil(len(corp_codes) / max(1, batch))
    est_calls = len(targets) * per_quarter_calls

    print("", flush=True)
    print(f"[2단계] {year_from}~{year_to}년 · 상장사 {len(corp_codes):,}개", flush=True)
    print(f"        전체 {len(plan)}개 분기 중  받을 {len(targets)}개 / 건너뛸 {len(skipped)}개",
          flush=True)
    print(f"        예상 호출 약 {est_calls:,}건 "
          f"(하루 한도 {budget.limit:,}건의 {est_calls / budget.limit * 100:.0f}%, "
          f"오늘 남은 {budget.remaining:,}건)", flush=True)
    if est_calls > budget.remaining:
        print("        [안내] 오늘 남은 호출로는 다 못 받습니다. "
              "받는 데까지 받고 내일 이어받습니다.", flush=True)
    print("", flush=True)

    # 건너뛰는 분기부터 이유와 함께 보여준다.
    for p in plan:
        tag = f"{p['year']}-{p['quarter']}"
        if p["action"] == "skip":
            if p["reason"] == PLAN_DONE:
                print(f"  {tag}  건너뜀 (이미 받음, {p['rows']:,}행)", flush=True)
            else:
                print(f"  {tag}  건너뜀 (아직 공시 기간이 아님, {p['due']} 이후 수집)", flush=True)
    if skipped:
        print("", flush=True)

    if not targets:
        state.set_api_calls(budget.used)
        state.save()
        print("새로 받을 분기가 없습니다. 이미 최신 상태입니다.", flush=True)
        return 0

    # ---- 3) 수집 ---------------------------------------------------------------------
    t0 = time.time()
    touched_years: set = set()
    cur_batch = batch
    total_calls = 0
    unit_total = len(targets) * max(1, len(corp_codes))
    unit_done = 0
    rc = 0

    for idx, item in enumerate(targets, start=1):
        year, quarter = item["year"], item["quarter"]
        tag = f"{year}-{quarter}"
        note = " [최근 분기 재확인]" if item["reason"] == PLAN_RECENT else ""
        last_print = [0.0]

        def _progress(done: int, total: int, _base=unit_done) -> None:
            now = time.time()
            if done < total and now - last_print[0] < 2.0:
                return
            last_print[0] = now
            u = _base + done
            pct = u / unit_total * 100.0
            el = now - t0
            eta = (el / u * (unit_total - u)) if u > 0 else None
            print(f"  {tag}  받는 중... {pct:4.0f}%  "
                  f"({done:,}/{total:,} 종목, 남은 예상 {_fmt_eta(eta)})", flush=True)

        print(f"  {tag}  받는 중...{note}  (배치 {cur_batch})", flush=True)
        try:
            items, cur_batch, calls = fetch_quarter(
                key, corp_codes, year, quarter, batch=cur_batch, fetch=fetch,
                sleep=sleep, on_progress=_progress, budget=budget,
            )
        except (RateLimitExceeded, BudgetExhausted) as e:
            total_calls = budget.spent_this_run
            state.set_api_calls(budget.limit if isinstance(e, RateLimitExceeded) else budget.used)
            state.save()
            print("", flush=True)
            print(f"  [중단] 오늘 DART 호출 한도({budget.limit:,}건)를 다 썼습니다.", flush=True)
            print("         내일 update_dart.bat 을 다시 실행하면 여기서부터 이어받습니다.", flush=True)
            print(f"         진행 상태: {out / STATE_FILENAME}", flush=True)
            rc = 6
            break
        except MaintenanceError:
            state.set_api_calls(budget.used)
            state.save()
            print("  [중단] DART 시스템 점검 중입니다. 잠시 후 다시 실행하세요.", flush=True)
            rc = 7
            break
        except AuthError as e:
            state.set_api_calls(budget.used)
            state.save()
            print(f"  [중단] 인증키 오류입니다: {e.dart_message or e.status}", flush=True)
            rc = 4
            break
        except NetworkError as e:
            state.set_api_calls(budget.used)
            state.save()
            print(f"  [중단] 네트워크 오류입니다: {e}", flush=True)
            print("         잠시 후 update_dart.bat 을 다시 실행하면 이어받습니다.", flush=True)
            rc = 5
            break

        total_calls += calls
        unit_done += len(corp_codes)

        rows = rows_from_items(items, year, quarter, corp_to_code=corp_to_code)
        part = _part_path(out, year, quarter)
        part.parent.mkdir(parents=True, exist_ok=True)
        to_frame(rows).to_parquet(part, index=False)

        state.mark_quarter(year, quarter, rows=len(rows), corps=len(corp_codes), complete=True)
        state.set_api_calls(budget.used)
        state.save()
        touched_years.add(year)

        if rows:
            print(f"  {tag}  완료 ({len(rows):,}행 · 호출 {calls:,}건)", flush=True)
        else:
            print(f"  {tag}  완료 (보고서 없음 · 호출 {calls:,}건)", flush=True)

        if 0 < budget.remaining <= BUDGET_WARN_AT:
            print(f"        [주의] 오늘 남은 호출이 {budget.remaining:,}건입니다.", flush=True)

    # ---- 4) 연도 파일 재구성 ---------------------------------------------------------
    for y in sorted(touched_years):
        n = _rebuild_year(out, y)
        print(f"[저장] {out}/fundamentals-{y}.parquet  ({n:,}행)", flush=True)

    state.set_api_calls(budget.used)
    state.save()

    print("", flush=True)
    print(f"[{'완료' if rc == 0 else '중단'}] 이번 실행 호출 {budget.spent_this_run:,}건 · "
          f"{_fmt_eta(time.time() - t0)} 걸렸습니다.", flush=True)
    print(f"       오늘 누적 호출 {budget.used:,}건 / {budget.limit:,}건", flush=True)
    print(f"       저장 위치: {out.resolve()}", flush=True)
    return rc


# ======================================================================================
# 9. 셀프테스트 (test_dart.bat 이 호출)
# ======================================================================================


SELFTEST_OK = 0
SELFTEST_ERROR = 1
SELFTEST_NO_KEY = 3
SELFTEST_BAD_KEY = 4
SELFTEST_NETWORK = 5
SELFTEST_RATE_LIMIT = 6
SELFTEST_MAINTENANCE = 7

SAMSUNG_CORP_CODE = "00126380"


def selftest(
    key: Optional[str] = None,
    fetch: Optional[Fetcher] = None,
    year: int = 2024,
    quarter: int = 4,
) -> int:
    """키 → 연결 → 삼성전자 실제 조회 순으로 확인한다. 종료 코드를 돌려준다."""
    line = "=" * 58
    print(line, flush=True)
    print(" DART 연동 점검", flush=True)
    print(line, flush=True)
    print("", flush=True)

    # ---- 1 ------------------------------------------------------------------------
    print("[1/3] 인증키 확인", flush=True)
    key = key or read_api_key()
    if not key:
        print("      결과: 실패 - 인증키를 찾을 수 없습니다.", flush=True)
        print("", flush=True)
        for ln in KEY_MISSING_HELP.splitlines():
            print(f"      {ln}", flush=True)
        return SELFTEST_NO_KEY
    src = "환경변수 DART_API_KEY" if os.environ.get("DART_API_KEY") else f"{KEY_FILENAME} 파일"
    print(f"      결과: 성공 - {src}에서 읽었습니다.", flush=True)
    print(f"      키   : {mask_key(key)}", flush=True)
    if len(key) != 40:
        print(f"      [주의] DART 인증키는 보통 40자입니다. 지금 {len(key)}자입니다.", flush=True)
    print("", flush=True)

    # ---- 2 ------------------------------------------------------------------------
    print("[2/3] DART 접속 확인 (기업 고유번호 내려받기)", flush=True)
    try:
        corp_df = fetch_corp_codes(key, fetch=fetch)
    except AuthError as e:
        print(f"      결과: 실패 - 인증키 오류 (status={e.status})", flush=True)
        print(f"      안내: {e.dart_message or STATUS_HELP.get(e.status, '')}", flush=True)
        return SELFTEST_BAD_KEY
    except RateLimitExceeded:
        print("      결과: 실패 - 오늘 호출 한도(20,000건)를 다 썼습니다.", flush=True)
        print("      안내: 내일 다시 실행하세요. 받던 데이터는 이어받습니다.", flush=True)
        return SELFTEST_RATE_LIMIT
    except MaintenanceError:
        print("      결과: 실패 - DART 시스템 점검 중입니다.", flush=True)
        print("      안내: 잠시 후 다시 실행하세요.", flush=True)
        return SELFTEST_MAINTENANCE
    except NetworkError as e:
        print(f"      결과: 실패 - 네트워크 오류 ({e})", flush=True)
        print("      안내: 인터넷 연결, 회사 방화벽/프록시, 백신 차단을 확인하세요.", flush=True)
        print("            opendart.fss.or.kr 이 막혀 있으면 개인 네트워크에서 시도하세요.", flush=True)
        return SELFTEST_NETWORK
    except DartError as e:
        print(f"      결과: 실패 - {e}", flush=True)
        return SELFTEST_ERROR

    print(f"      결과: 성공 - 상장사 {len(corp_df):,}개를 확인했습니다.", flush=True)
    for _, r in corp_df.head(3).iterrows():
        print(f"            {r['code']}  {r['corp_name']}", flush=True)
    print("", flush=True)

    # ---- 3 ------------------------------------------------------------------------
    print(f"[3/3] 삼성전자 {year}년 {QUARTER_LABEL[quarter]} 주요계정 조회 및 계산", flush=True)
    try:
        items, _b, _c = fetch_quarter(
            key, [SAMSUNG_CORP_CODE], year, quarter, batch=1, fetch=fetch
        )
    except RateLimitExceeded:
        print("      결과: 실패 - 오늘 호출 한도를 다 썼습니다.", flush=True)
        return SELFTEST_RATE_LIMIT
    except MaintenanceError:
        print("      결과: 실패 - DART 시스템 점검 중입니다.", flush=True)
        return SELFTEST_MAINTENANCE
    except NetworkError as e:
        print(f"      결과: 실패 - 네트워크 오류 ({e})", flush=True)
        return SELFTEST_NETWORK
    except DartError as e:
        print(f"      결과: 실패 - {e}", flush=True)
        return SELFTEST_ERROR

    rows = rows_from_items(items, year, quarter, corp_to_code={SAMSUNG_CORP_CODE: "005930"})
    if not rows:
        print("      결과: 실패 - 데이터가 비어 있습니다 (status 013).", flush=True)
        print("      안내: 연도를 바꿔 다시 시도해 보세요. 키 문제는 아닙니다.", flush=True)
        return SELFTEST_ERROR

    df = add_quarter_columns(to_frame(rows))
    r = df.iloc[0]
    eok = 100_000_000.0

    def _eok(v) -> str:
        n = _as_opt_int(v)
        return "모름" if n is None else f"{n / eok:,.0f}억원"

    def _pct(v) -> str:
        return "계산 불가" if v is None or pd.isna(v) else f"{float(v):,.1f}%"

    disclosed = (
        pd.Timestamp(r["disclosed_at"]).date() if pd.notna(r["disclosed_at"]) else "모름"
    )
    print("      결과: 성공", flush=True)
    print("", flush=True)
    print(f"      종목코드   : {r['code']}", flush=True)
    print(f"      재무 기준  : {r['fs_div']} ({'연결' if r['fs_div'] == 'CFS' else '별도'})", flush=True)
    print(f"      공시일     : {disclosed}   <- 이 날짜 전에는 이 재무를 쓸 수 없습니다", flush=True)
    print("", flush=True)
    print(f"      유동자산   : {_eok(r['유동자산'])}", flush=True)
    print(f"      유동부채   : {_eok(r['유동부채'])}", flush=True)
    print(f"      부채총계   : {_eok(r['부채총계'])}", flush=True)
    print(f"      자본총계   : {_eok(r['자본총계'])}", flush=True)
    print(f"      영업이익   : {_eok(r['영업이익'])}  (연간 누적)", flush=True)
    print("", flush=True)
    print(f"      부채비율   : {_pct(r['debt_ratio_pct'])}   = 부채총계 / 자본총계", flush=True)
    print(f"      유동비율   : {_pct(r['current_ratio_pct'])}   = 유동자산 / 유동부채", flush=True)
    print("", flush=True)
    print(line, flush=True)
    print(" 점검 통과. 이제 update_dart.bat 을 실행하세요.", flush=True)
    print(line, flush=True)
    return SELFTEST_OK


# ======================================================================================
# 10. 진단 리포트 (--report) — 네트워크를 쓰지 않는다
# ======================================================================================

REPORT_OK = 0
REPORT_ISSUES = 1
REPORT_NO_DATA = 2

RULE = "=" * 74
THIN = "-" * 74

#: 대표 종목
SAMPLE_CODES = [("005930", "삼성전자"), ("000660", "SK하이닉스")]

#: 삼성전자의 상식 범위 (이 밖이면 파싱 오류를 의심한다)
SAMSUNG_DEBT_RATIO_RANGE = (10.0, 60.0)
SAMSUNG_CURRENT_RATIO_RANGE = (100.0, 500.0)

#: 결측률이 이보다 높으면 계정명 매칭 실패를 의심한다
MISSING_WARN_PCT = 30.0
#: 유동자산/유동부채는 금융업에 원래 없어서 기준을 느슨하게 잡는다
MISSING_WARN_PCT_CURRENT = 55.0
#: 부채비율이 이 값을 넘으면 이상치로 센다
DEBT_RATIO_ABSURD = 10_000.0


def _w(s) -> int:
    """콘솔 표시 폭. 한글은 2칸."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _lj(s, n: int) -> str:
    return f"{s}{' ' * max(0, n - _w(s))}"


def _rj(s, n: int) -> str:
    return f"{' ' * max(0, n - _w(s))}{s}"


def _num(v, digits: int = 0) -> str:
    """숫자를 사람이 읽는 문자열로. 없으면 ``-``."""
    if v is None:
        return "-"
    try:
        if pd.isna(v):
            return "-"
    except (TypeError, ValueError):
        pass
    return f"{float(v):,.{digits}f}"


def _eok(v) -> str:
    """원 단위 정수를 억원으로."""
    n = _as_opt_int(v)
    return "-" if n is None else f"{n / 100_000_000.0:,.0f}"


def _stats(series: pd.Series) -> Optional[dict]:
    """최소/25%/중앙/75%/최대."""
    s = pd.to_numeric(series, errors="coerce").astype("float64").dropna()
    if s.empty:
        return None
    return {
        "n": int(len(s)),
        "min": float(s.min()),
        "p25": float(s.quantile(0.25)),
        "median": float(s.median()),
        "p75": float(s.quantile(0.75)),
        "max": float(s.max()),
    }


class _Report:
    """출력과 함께 이상 징후를 모은다."""

    def __init__(self, out=None):
        self._out = out or (lambda s: print(s, flush=True))
        self.issues: List[str] = []
        self.notes: List[str] = []

    def p(self, s: str = "") -> None:
        self._out(s)

    def issue(self, msg: str) -> None:
        """사람이 확인해야 하는 문제."""
        self.issues.append(msg)
        self.p(f"      [확인 필요] {msg}")

    def note(self, msg: str) -> None:
        """알아 두면 좋은 참고 사항."""
        self.notes.append(msg)
        self.p(f"      [참고] {msg}")


# --- marcap 쪽 읽기 (engine 을 수정하지 않고 컬럼 하나만 읽는다) -----------------------


def _marcap_files(marcap_root: str | os.PathLike) -> Dict[int, Path]:
    root = Path(marcap_root)
    out: Dict[int, Path] = {}
    for d in (root / "data", root):
        if not d.is_dir():
            continue
        for p in d.glob("marcap-*.parquet"):
            m = re.search(r"marcap-(\d{4})\.parquet$", p.name, re.IGNORECASE)
            if m:
                out.setdefault(int(m.group(1)), p)
        if out:
            break
    return out


def _marcap_codes(marcap_root, years: Optional[Iterable[int]] = None) -> Dict[int, set]:
    """연도별 등장 종목코드. ``Code`` 컬럼만 읽어 빠르게 센다."""
    files = _marcap_files(marcap_root)
    if years is not None:
        want = set(int(y) for y in years)
        files = {y: p for y, p in files.items() if y in want}
    out: Dict[int, set] = {}
    for y, p in sorted(files.items()):
        try:
            col = pd.read_parquet(p, columns=["Code"])["Code"]
        except Exception:  # pragma: no cover - 손상 파일
            continue
        out[y] = set(col.astype(str).str.zfill(6).unique())
    return out


def _marcap_live_codes(marcap_root) -> set:
    """가장 최근 연도 파일의 마지막 거래일에 존재하는 종목 = 현재 상장."""
    files = _marcap_files(marcap_root)
    if not files:
        return set()
    p = files[max(files)]
    try:
        df = pd.read_parquet(p, columns=["Date", "Code"])
    except Exception:  # pragma: no cover
        return set()
    if df.empty:
        return set()
    last = pd.to_datetime(df["Date"]).max()
    live = df.loc[pd.to_datetime(df["Date"]) == last, "Code"]
    return set(live.astype(str).str.zfill(6).unique())


# --- 각 절 ---------------------------------------------------------------------------


def _section_collection(r: _Report, df: pd.DataFrame, files: Dict[int, Path],
                        root: Path, marcap_root) -> None:
    r.p("[가] 수집 현황")
    r.p(THIN)

    state_path = root / STATE_FILENAME
    last_fetch = "모름"
    try:
        if state_path.is_file():
            raw = json.loads(state_path.read_text(encoding="utf-8-sig"))
            last_fetch = str(raw.get("updated_at") or raw.get("corp_map_fetched_at") or "모름")
    except (OSError, ValueError):  # pragma: no cover
        pass

    years = sorted(int(y) for y in df["year"].dropna().unique())
    periods = sorted(
        (int(y), int(q))
        for y, q in zip(df["year"].dropna(), df["quarter"].dropna())
    )
    codes = df["code"].dropna().unique()

    r.p(f"  연도 파일 수      : {len(files):,}개")
    r.p(f"  총 행 수          : {len(df):,}행")
    r.p(f"  연도 범위         : {min(years)} ~ {max(years)}" if years else "  연도 범위         : -")
    if periods:
        a, b = periods[0], periods[-1]
        r.p(f"  분기 범위         : {a[0]}-{a[1]} ~ {b[0]}-{b[1]}")
    r.p(f"  마지막 수집 시각  : {last_fetch}")
    r.p(f"  커버 종목 수      : {len(codes):,}개")

    live = _marcap_live_codes(marcap_root)
    if live:
        covered = len(set(str(c) for c in codes) & live)
        r.p(f"  marcap 현재 상장  : {len(live):,}개 중 {covered:,}개 커버 "
            f"({covered / len(live) * 100:.1f}%)")
        if covered / len(live) < 0.80:
            r.issue(f"현재 상장 종목의 {covered / len(live) * 100:.0f}% 만 재무가 있습니다. "
                    "corp_map 매칭이나 수집 범위를 의심하세요.")
    else:
        r.note("marcap 데이터가 없어 종목 커버리지를 비교하지 못했습니다.")

    # --- 분기별 행 수 표 ---
    r.p("")
    r.p("  분기별 행 수 (X=비었음, *=다른 분기의 절반 미만)")
    counts: Dict[int, Dict[int, int]] = {}
    for (y, q), n in df.groupby(
        [df["year"].astype("Int64"), df["quarter"].astype("Int64")]
    ).size().items():
        counts.setdefault(int(y), {})[int(q)] = int(n)

    nonzero = [n for row in counts.values() for n in row.values() if n > 0]
    median = float(pd.Series(nonzero).median()) if nonzero else 0.0

    header = ("  " + _lj("연도", 6) + _rj("1분기", 9) + _rj("반기", 9)
              + _rj("3분기", 9) + _rj("사업보고서", 12) + _rj("합계", 10))
    r.p(header)
    thin_rows = []
    empty_rows = []
    for y in years:
        row = counts.get(y, {})
        cells = []
        for q in (1, 2, 3, 4):
            n = row.get(q, 0)
            if n == 0:
                cells.append("X")
                empty_rows.append(f"{y}-{q}")
            elif median and n < median * 0.5:
                cells.append(f"{n:,}*")
                thin_rows.append(f"{y}-{q}({n:,}행)")
            else:
                cells.append(f"{n:,}")
        total = sum(row.values())
        r.p("  " + _lj(str(y), 6) + _rj(cells[0], 9) + _rj(cells[1], 9)
            + _rj(cells[2], 9) + _rj(cells[3], 12) + _rj(f"{total:,}", 10))

    r.p("")
    if empty_rows:
        recent = [s for s in empty_rows if int(s.split("-")[0]) >= max(years) - 1]
        old = [s for s in empty_rows if s not in recent]
        if recent:
            r.note(f"아직 공시 기간이 아닌 분기가 비어 있습니다: {', '.join(recent)}")
        if old:
            r.issue(f"과거 분기가 비어 있습니다: {', '.join(old[:8])}"
                    f"{' …' if len(old) > 8 else ''} — 그 분기를 못 받았을 수 있습니다.")
    if thin_rows:
        r.issue(f"행 수가 유난히 적은 분기가 있습니다: {', '.join(thin_rows[:8])}"
                f"{' …' if len(thin_rows) > 8 else ''}")
    if not empty_rows and not thin_rows:
        r.p("      분기별 행 수가 고릅니다. 빠진 분기 없음.")
    r.p("")


def _section_parsing(r: _Report, df: pd.DataFrame) -> None:
    r.p("[나] 파싱 점검 — 계정명·금액 표기가 제대로 잡혔는지")
    r.p(THIN)
    n = len(df)

    r.p("  재무 항목별 결측률")
    r.p("  " + _lj("항목", 14) + _rj("값 있음", 10) + _rj("결측", 10) + _rj("결측률", 10))
    for c in AMOUNT_COLUMNS:
        have = int(df[c].notna().sum())
        miss = n - have
        pct = miss / n * 100.0 if n else 0.0
        r.p("  " + _lj(c, 14) + _rj(f"{have:,}", 10) + _rj(f"{miss:,}", 10)
            + _rj(f"{pct:.1f}%", 10))
        limit = MISSING_WARN_PCT_CURRENT if c in ("유동자산", "유동부채") else MISSING_WARN_PCT
        if pct > limit:
            r.issue(f"'{c}' 가 {pct:.0f}% 결측입니다. "
                    "DART 응답의 account_nm 표기와 매칭이 어긋났을 수 있습니다.")
    r.p("")

    # --- 파생 지표 분포 ---
    r.p("  파생 지표 분포")
    r.p("  " + _lj("지표", 18) + _rj("건수", 9) + _rj("최소", 12) + _rj("25%", 11)
        + _rj("중앙", 11) + _rj("75%", 11) + _rj("최대", 13))
    for col, unit, digits in (
        ("debt_ratio_pct", "%", 1),
        ("current_ratio_pct", "%", 1),
        ("op_income_quarter", "억원", 0),
    ):
        s = df[col]
        if col == "op_income_quarter":
            s = pd.to_numeric(s, errors="coerce").astype("float64") / 100_000_000.0
        st = _stats(s)
        if st is None:
            r.p("  " + _lj(f"{col}({unit})", 18) + _rj("0", 9) + "  (값 없음)")
            r.issue(f"'{col}' 에 값이 하나도 없습니다.")
            continue
        r.p("  " + _lj(f"{col}({unit})", 18) + _rj(f"{st['n']:,}", 9)
            + _rj(_num(st["min"], digits), 12) + _rj(_num(st["p25"], digits), 11)
            + _rj(_num(st["median"], digits), 11) + _rj(_num(st["p75"], digits), 11)
            + _rj(_num(st["max"], digits), 13))
        miss_pct = (1 - st["n"] / n) * 100.0 if n else 0.0
        if miss_pct > MISSING_WARN_PCT_CURRENT:
            r.issue(f"'{col}' 이 {miss_pct:.0f}% 결측입니다.")

    st = _stats(df["debt_ratio_pct"])
    if st is not None:
        if not (5.0 <= st["median"] <= 400.0):
            r.issue(f"부채비율 중앙값이 {st['median']:,.1f}% 입니다. "
                    "보통 40~150% 범위입니다 — 단위나 부호 파싱을 의심하세요.")
        absurd = int((pd.to_numeric(df["debt_ratio_pct"], errors="coerce")
                      > DEBT_RATIO_ABSURD).sum())
        if absurd:
            pct = absurd / max(1, st["n"]) * 100.0
            msg = (f"부채비율이 {DEBT_RATIO_ABSURD:,.0f}% 를 넘는 행이 {absurd:,}건"
                   f"({pct:.2f}%) 있습니다.")
            if pct > 1.0:
                r.issue(msg + " 단위/부호 파싱 오류를 의심하세요.")
            else:
                r.note(msg + " 자본잠식 직전 회사면 정상일 수 있습니다.")

    st = _stats(df["current_ratio_pct"])
    if st is not None and not (30.0 <= st["median"] <= 1000.0):
        r.issue(f"유동비율 중앙값이 {st['median']:,.1f}% 입니다. 보통 100~250% 범위입니다.")
    r.p("")

    # --- 음수가 실제로 잡혔는가 ---
    r.p("  음수 표기 파싱 확인")
    op_cum = pd.to_numeric(df["영업이익"], errors="coerce")
    op_q = pd.to_numeric(df["op_income_quarter"], errors="coerce")
    neg_cum = int((op_cum < 0).sum())
    neg_q = int((op_q < 0).sum())
    have_cum = int(op_cum.notna().sum())
    have_q = int(op_q.notna().sum())
    r.p(f"    영업이익(누적) 음수      : {neg_cum:,}건 / {have_cum:,}건 "
        f"({neg_cum / have_cum * 100 if have_cum else 0:.1f}%)")
    r.p(f"    영업이익(분기 차분) 음수 : {neg_q:,}건 / {have_q:,}건 "
        f"({neg_q / have_q * 100 if have_q else 0:.1f}%)")
    r.p(f"    당기순이익 음수          : "
        f"{int((pd.to_numeric(df['당기순이익'], errors='coerce') < 0).sum()):,}건")
    if have_cum and neg_cum == 0:
        r.issue("영업이익 음수가 한 건도 없습니다. "
                "'△' 나 '(1,000)' 같은 음수 표기 파싱이 실패했을 가능성이 큽니다.")
    elif have_cum and neg_cum / have_cum < 0.03:
        r.issue(f"영업이익 음수 비율이 {neg_cum / have_cum * 100:.1f}% 로 지나치게 낮습니다. "
                "상장사 적자 비율은 보통 20~35% 입니다.")
    r.p("")

    # --- fs_div 분포 ---
    r.p("  연결/별도 분포")
    vc = df["fs_div"].value_counts(dropna=False)
    for k, v in vc.items():
        label = {"CFS": "CFS (연결)", "OFS": "OFS (별도)"}.get(str(k), f"{k}")
        r.p(f"    {_lj(label, 14)}{_rj(f'{int(v):,}', 9)}  ({v / n * 100:.1f}%)")
    if "CFS" not in set(str(x) for x in vc.index):
        r.issue("연결(CFS) 재무가 하나도 없습니다. fs_div 우선순위 처리를 확인하세요.")
    r.p("")

    # --- 유동자산/유동부채가 아예 없는 종목 ---
    by_code = df.groupby("code")[["유동자산", "유동부채"]].count()
    no_current = by_code[(by_code["유동자산"] == 0) & (by_code["유동부채"] == 0)]
    total_codes = int(df["code"].nunique())
    r.p(f"  유동자산/유동부채가 한 번도 없는 종목 : {len(no_current):,}개 "
        f"/ {total_codes:,}개 ({len(no_current) / total_codes * 100 if total_codes else 0:.1f}%)")
    r.p("    금융업(은행·보험·증권)은 유동/비유동 구분을 하지 않아 원래 비어 있습니다.")
    if total_codes and len(no_current) / total_codes > 0.30:
        r.issue(f"유동비율을 계산할 수 없는 종목이 {len(no_current) / total_codes * 100:.0f}% 입니다. "
                "금융업 비중치고 지나치게 높습니다.")
    r.p("")


def _pick_loss_code(df: pd.DataFrame) -> Optional[str]:
    """적자 이력이 있는 대표 종목 하나 (행이 가장 많은 것)."""
    op = pd.to_numeric(df["op_income_quarter"], errors="coerce")
    loss_codes = set(df.loc[op < 0, "code"].dropna().astype(str))
    loss_codes -= {c for c, _n in SAMPLE_CODES}
    if not loss_codes:
        return None
    sub = df[df["code"].astype(str).isin(loss_codes)]
    counts = sub.groupby(sub["code"].astype(str)).size().sort_values(
        ascending=False, kind="stable"
    )
    return str(counts.index[0])


def _section_samples(r: _Report, store, df: pd.DataFrame, limit: int = 12) -> None:
    r.p("[다] 대표 종목 샘플 — 숫자가 상식적인지 눈으로 확인")
    r.p(THIN)

    targets = list(SAMPLE_CODES)
    loss = _pick_loss_code(df)
    if loss:
        name = "적자 이력 종목"
        targets.append((loss, name))
    else:
        r.note("적자 이력이 있는 종목을 찾지 못했습니다. 음수 파싱을 의심하세요.")

    for code, label in targets:
        sub = df[df["code"].astype(str) == code].sort_values(
            ["year", "quarter"], kind="stable"
        )
        r.p("")
        r.p(f"  {code} {label}")
        if sub.empty:
            r.p("      데이터 없음")
            if code in ("005930", "000660"):
                r.issue(f"{code} {label} 재무가 하나도 없습니다. 수집이 제대로 안 됐습니다.")
            continue

        r.p("  " + _lj("분기", 10) + _lj("공시일", 13) + _rj("부채비율", 11)
            + _rj("유동비율", 11) + _rj("분기영업이익(억)", 20))
        for _, row in sub.tail(limit).iterrows():
            d = row["disclosed_at"]
            r.p("  "
                + _lj(f"{_as_opt_int(row['year'])}-{_as_opt_int(row['quarter'])}", 10)
                + _lj("-" if pd.isna(d) else str(pd.Timestamp(d).date()), 13)
                + _rj(_num(row["debt_ratio_pct"], 1), 11)
                + _rj(_num(row["current_ratio_pct"], 1), 11)
                + _rj(_eok(row["op_income_quarter"]), 20))
        if len(sub) > limit:
            r.p(f"      (최근 {limit}개만 표시. 전체 {len(sub)}행)")

        if code == "005930":
            st = _stats(sub["debt_ratio_pct"])
            lo, hi = SAMSUNG_DEBT_RATIO_RANGE
            if st is None:
                r.issue("삼성전자 부채비율이 전부 비어 있습니다.")
            elif not (lo <= st["median"] <= hi):
                r.issue(f"삼성전자 부채비율 중앙값이 {st['median']:,.1f}% 입니다. "
                        f"정상 범위는 {lo:.0f}~{hi:.0f}% 입니다 — 파싱 오류를 의심하세요.")
            else:
                r.p(f"      삼성전자 부채비율 중앙값 {st['median']:.1f}% — 정상 범위입니다.")
            st = _stats(sub["current_ratio_pct"])
            lo, hi = SAMSUNG_CURRENT_RATIO_RANGE
            if st is not None and not (lo <= st["median"] <= hi):
                r.issue(f"삼성전자 유동비율 중앙값이 {st['median']:,.1f}% 입니다. "
                        f"정상 범위는 {lo:.0f}~{hi:.0f}% 입니다.")
            elif st is not None:
                r.p(f"      삼성전자 유동비율 중앙값 {st['median']:.1f}% — 정상 범위입니다.")
    r.p("")


def _section_asof(r: _Report, store, df: pd.DataFrame) -> None:
    r.p("[라] as-of 동작 확인 — 공시 전 재무를 미리 보고 있지 않은지")
    r.p(THIN)

    code = "005930"
    sub = df[df["code"].astype(str) == code]
    if sub.empty:
        code = str(df["code"].dropna().iloc[0])
        sub = df[df["code"].astype(str) == code]
    r.p(f"  기준 종목: {code}")
    r.p("")

    dates = sorted(pd.Timestamp(d) for d in sub["disclosed_at"].dropna().unique())
    probes: List[pd.Timestamp] = []
    for d in dates[-4:]:
        probes += [d - pd.Timedelta(days=1), d]
    if not probes:
        r.issue("공시일(disclosed_at)이 하나도 없습니다. rcept_no 파싱을 확인하세요.")
        r.p("")
        return

    r.p("  " + _lj("조회일", 13) + _lj("반환 분기", 12) + _lj("그 분기 공시일", 16)
        + _lj("판정", 10))
    violations = 0
    for probe in probes:
        got = store.as_of(code, probe.date())
        if got is None:
            r.p("  " + _lj(str(probe.date()), 13) + _lj("(없음)", 12)
                + _lj("-", 16) + _lj("정상", 10))
            continue
        disc = got["disclosed_at"]
        bad = disc is not None and pd.Timestamp(disc) > probe
        if bad:
            violations += 1
        r.p("  " + _lj(str(probe.date()), 13)
            + _lj(f"{got['year']}-{got['quarter']}", 12)
            + _lj(str(disc), 16)
            + _lj("룩어헤드!!" if bad else "정상", 10))

    # 전 종목 전수 확인
    r.p("")
    span = sorted(pd.Timestamp(d) for d in df["disclosed_at"].dropna().unique())
    scan_dates = []
    if span:
        lo, hi = span[0], span[-1]
        step = max(1, (hi - lo).days // 7)
        scan_dates = [lo + pd.Timedelta(days=step * i) for i in range(8)]
    total_bad = 0
    for d in scan_dates:
        panel = store.as_of_panel(None, d.date())
        if panel.empty:
            continue
        bad = int((panel["disclosed_at"] > d).sum())
        total_bad += bad
    r.p(f"  전 종목 전수 확인: {len(scan_dates)}개 시점에서 공시일이 조회일보다 뒤인 행 "
        f"{total_bad:,}건")

    if violations or total_bad:
        r.p("")
        r.p("  " + "!" * 60)
        r.issue(f"룩어헤드 편향 발견: 공시 전 재무가 {violations + total_bad:,}건 조회됩니다. "
                "이대로 백테스트하면 수익률이 실제보다 좋게 나옵니다. 즉시 알려주세요.")
        r.p("  " + "!" * 60)
    else:
        r.p("      공시 전 재무가 조회되는 경우 없음. as-of 규칙이 지켜지고 있습니다.")
    r.p("")


def _section_survivorship(r: _Report, df: pd.DataFrame, marcap_root) -> None:
    r.p("[마] 생존 편향 규모 — marcap 에는 있는데 재무가 없는 종목")
    r.p(THIN)

    years = sorted(int(y) for y in df["year"].dropna().unique())
    by_year = _marcap_codes(marcap_root, years=years)
    if not by_year:
        r.note("marcap 데이터가 없어 생존 편향 규모를 재지 못했습니다. "
               "update_marcap.bat 을 먼저 실행하면 이 항목도 나옵니다.")
        r.p("")
        return

    marcap_all = set().union(*by_year.values())
    dart_codes = set(str(c) for c in df["code"].dropna().unique())
    missing = marcap_all - dart_codes
    live = _marcap_live_codes(marcap_root)

    missing_live = missing & live
    missing_gone = missing - live

    r.p(f"  marcap 등장 종목 ({min(years)}~{max(years)}) : {len(marcap_all):,}개")
    r.p(f"  DART 재무가 한 건도 없는 종목        : {len(missing):,}개 "
        f"({len(missing) / len(marcap_all) * 100:.1f}%)")
    r.p(f"    - 지금도 상장 중                   : {len(missing_live):,}개")
    r.p(f"    - 지금은 없음 (상장폐지 추정)      : {len(missing_gone):,}개")
    r.p("")
    r.p("  상장폐지 종목의 과거 재무는 corpCode.xml 에 종목코드가 남지 않아 받을 수 없습니다.")
    r.p("  재무 필터를 켜면 그 종목들이 통째로 빠지므로, 실제보다 결과가 좋게 나올 수 있습니다.")

    if len(missing_gone):
        r.note(f"재무 필터를 켠 백테스트에서는 상장폐지 종목 {len(missing_gone):,}개가 "
               "자동 제외됩니다. 필터를 끈 결과와 비교해 보세요.")
    if len(missing_live) and len(missing_live) / max(1, len(live)) > 0.15:
        r.issue(f"현재 상장 중인데 재무가 없는 종목이 {len(missing_live):,}개 "
                f"({len(missing_live) / len(live) * 100:.0f}%) 입니다. "
                "스팩·리츠·외국계를 빼도 많다면 수집이 덜 됐을 수 있습니다.")
    r.p("")


def run_report(
    dart_root: str | os.PathLike = "dart",
    marcap_root: str | os.PathLike = "marcap",
    out=None,
) -> int:
    """받아 둔 ``dart/`` 를 읽어 한국어 진단 리포트를 낸다. **네트워크를 쓰지 않는다.**

    0 = 정상 / 1 = 확인 필요 / 2 = 데이터 없음
    """
    from engine.dart import DartStore  # 순환 의존을 피하려고 여기서 import

    r = _Report(out)
    root = Path(dart_root)

    r.p(RULE)
    r.p(" DART 재무 데이터 진단 리포트")
    r.p(f" 생성 시각 : {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    r.p(f" 데이터 폴더: {root.resolve() if root.exists() else root}")
    r.p(RULE)
    r.p("")

    store = DartStore(root=root)
    if not store.available:
        r.p("  받아 둔 재무 데이터가 없습니다.")
        r.p("")
        r.p(f"  찾은 위치 : {root.resolve() if root.exists() else root}")
        r.p("")
        r.p("  1) test_dart.bat 을 실행해 인증키와 연결을 먼저 확인하세요.")
        r.p("  2) 그 다음 update_dart.bat 을 실행해 데이터를 받으세요.")
        r.p("")
        r.p(RULE)
        r.p(" 판정: 확인 필요 (데이터 없음)")
        r.p(RULE)
        return REPORT_NO_DATA

    df = store.load()
    files = {y: root / f"fundamentals-{y}.parquet" for y in store.years}

    if df.empty:
        r.p("  파일은 있는데 행이 하나도 없습니다. update_dart.bat 을 다시 실행하세요.")
        r.p(RULE)
        r.p(" 판정: 확인 필요 (행 없음)")
        r.p(RULE)
        return REPORT_NO_DATA

    _section_collection(r, df, files, root, marcap_root)
    _section_parsing(r, df)
    _section_samples(r, store, df)
    _section_asof(r, store, df)
    _section_survivorship(r, df, marcap_root)

    # ---- 판정 -----------------------------------------------------------------------
    r.p(RULE)
    if r.issues:
        r.p(f" 판정: 확인 필요 — {len(r.issues)}건")
        r.p(RULE)
        for i, msg in enumerate(r.issues, start=1):
            r.p(f"  {i}. {msg}")
        r.p("")
        r.p(" 위 내용을 그대로 담당자에게 보내주세요.")
    else:
        r.p(" 판정: 정상")
        r.p(RULE)
        r.p("  계정명 매칭, 음수 표기, 부호·단위, as-of 규칙 모두 이상 없습니다.")
        r.p("  재무 필터를 켠 백테스트를 돌려도 됩니다.")
    if r.notes:
        r.p("")
        r.p(" 참고 사항")
        for i, msg in enumerate(r.notes, start=1):
            r.p(f"  {i}. {msg}")
    r.p(RULE)
    return REPORT_ISSUES if r.issues else REPORT_OK


# ======================================================================================
# 11. CLI
# ======================================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.fetch_dart",
        description="DART OpenAPI 재무 데이터 다운로더 (이미 받은 분기는 자동으로 건너뜁니다)",
    )
    p.add_argument("--from", dest="year_from", type=int, default=2015, help="시작 연도 (기본 2015)")
    p.add_argument("--to", dest="year_to", type=int, default=dt.date.today().year, help="끝 연도")
    p.add_argument("--out", dest="out_dir", default="dart", help="저장 폴더 (기본 dart)")
    p.add_argument("--batch", type=int, default=50, help="한 번에 조회할 회사 수 (기본 50, 최대 100)")
    p.add_argument("--force", action="store_true",
                   help="이미 받은 분기까지 전부 다시 받는다")
    p.add_argument("--refresh-recent", dest="refresh_recent", type=int, default=2,
                   help="정정공시 대비로 다시 확인할 최근 분기 수 (기본 2, 0이면 재확인 안 함)")
    p.add_argument("--resume", action="store_true",
                   help="(구버전 호환) 기본 동작이므로 아무 효과 없음")
    p.add_argument("--sleep", type=float, default=0.0, help="호출 사이 대기 초 (기본 0)")
    p.add_argument("--key-file", dest="key_file", default=None, help="인증키 파일 경로")
    p.add_argument("--refresh-corp-map", dest="refresh_corp_map", action="store_true",
                   help="기업 고유번호 매핑을 새로 받는다")
    p.add_argument("--disclosure-lag-days", dest="lag_days", type=int,
                   default=DISCLOSURE_LAG_DAYS,
                   help=f"분기 종료 후 이 일수가 지나야 요청한다 (기본 {DISCLOSURE_LAG_DAYS})")
    p.add_argument("--annual-lag-days", dest="annual_lag_days", type=int,
                   default=ANNUAL_DISCLOSURE_LAG_DAYS,
                   help=f"사업보고서(4분기) 기준 일수 (기본 {ANNUAL_DISCLOSURE_LAG_DAYS})")
    p.add_argument("--call-limit", dest="call_limit", type=int, default=DAILY_CALL_LIMIT,
                   help=f"하루 호출 한도 (기본 {DAILY_CALL_LIMIT})")
    p.add_argument("--selftest", action="store_true", help="키·연결·삼성전자 1건만 확인하고 끝낸다")
    p.add_argument("--report", action="store_true",
                   help="받아 둔 데이터를 읽어 진단 리포트를 낸다 (네트워크 안 씀)")
    p.add_argument("--marcap", dest="marcap_root", default="marcap",
                   help="--report 에서 비교할 marcap 폴더 (기본 marcap)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    # --report 는 인증키도 네트워크도 필요 없다. 키 검사보다 먼저 처리한다.
    if args.report:
        return run_report(args.out_dir, args.marcap_root)

    key = read_api_key(args.key_file)
    if args.selftest:
        return selftest(key)

    if not key:
        print(KEY_MISSING_HELP, flush=True)
        return SELFTEST_NO_KEY

    if args.year_to < args.year_from:
        print(f"[오류] --to({args.year_to}) 가 --from({args.year_from}) 보다 작습니다.", flush=True)
        return 2
    if args.batch < 1:
        print("[오류] --batch 는 1 이상이어야 합니다.", flush=True)
        return 2
    if args.batch > MAX_CORP_PER_CALL:
        print(f"[안내] --batch 는 최대 {MAX_CORP_PER_CALL} 입니다. "
              f"{MAX_CORP_PER_CALL} 로 줄여 실행합니다.", flush=True)
        args.batch = MAX_CORP_PER_CALL

    try:
        return run_fetch(
            key,
            args.year_from,
            args.year_to,
            out_dir=args.out_dir,
            batch=args.batch,
            force=args.force,
            refresh_recent=args.refresh_recent,
            sleep=args.sleep,
            refresh_corp_map=args.refresh_corp_map,
            lag_days=args.lag_days,
            annual_lag_days=args.annual_lag_days,
            call_limit=args.call_limit,
        )
    except KeyboardInterrupt:  # pragma: no cover
        print("", flush=True)
        print("[중단] 사용자가 취소했습니다. 다시 실행하면 이어받습니다.", flush=True)
        return 130
    except DartStatusError as e:  # pragma: no cover
        print(f"[오류] DART status={e.status} {e.dart_message}", flush=True)
        return 1
    except DartError as e:  # pragma: no cover
        print(f"[오류] {e}", flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
