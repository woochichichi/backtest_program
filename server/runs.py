"""백테스트 실행 결과 인메모리 보관소.

- ``run_id`` -> 결과 dict 를 최근 N건(기본 10)만 LRU 로 들고 있는다.
- ``/api/chart`` 가 ``run_id`` 로 마커/보유구간/기준선을 채울 때 이 보관소를 참조한다.
- 프로세스 메모리에만 존재한다. 서버를 재시작하면 사라진다.
- 여러 요청이 동시에 들어와도 안전하도록 락으로 감싼다.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_RUNS = 10


class RunStore:
    """run_id -> 백테스트 결과 dict (최근 ``maxlen`` 건만 유지)."""

    def __init__(self, maxlen: int = MAX_RUNS) -> None:
        self._maxlen = int(maxlen)
        self._lock = threading.RLock()
        self._items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._seq = 0

    # ------------------------------------------------------------------ id
    def new_run_id(self, when: Optional[datetime] = None) -> str:
        """``r_20260725_081204`` 형태의 run_id 를 만든다.

        같은 초에 두 번 실행돼도 겹치지 않도록 충돌 시 접미사를 붙인다.
        """
        when = when or datetime.now()
        base = "r_" + when.strftime("%Y%m%d_%H%M%S")
        with self._lock:
            run_id = base
            n = 1
            while run_id in self._items:
                n += 1
                run_id = f"{base}_{n}"
            self._seq += 1
            return run_id

    # --------------------------------------------------------------- store
    def put(self, run_id: str, result: Dict[str, Any]) -> str:
        """결과를 보관한다. 정원을 넘으면 가장 오래된 항목을 버린다."""
        with self._lock:
            if run_id in self._items:
                self._items.pop(run_id)
            self._items[run_id] = result
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)
        return run_id

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        """결과를 꺼낸다. 조회한 항목은 최근 사용으로 갱신한다(LRU)."""
        if not run_id:
            return None
        with self._lock:
            item = self._items.get(run_id)
            if item is not None:
                self._items.move_to_end(run_id)
            return item

    def has(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._items

    def delete(self, run_id: str) -> bool:
        with self._lock:
            return self._items.pop(run_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    # ---------------------------------------------------------------- meta
    def ids(self) -> List[str]:
        """최근 것이 앞에 오도록 run_id 목록을 반환한다."""
        with self._lock:
            return list(reversed(self._items.keys()))

    def summaries(self) -> List[Dict[str, Any]]:
        """디버깅/목록용 요약. API 계약에는 없는 부가 정보다."""
        out: List[Dict[str, Any]] = []
        with self._lock:
            for run_id, res in reversed(self._items.items()):
                metrics = res.get("metrics") or {}
                out.append(
                    {
                        "run_id": run_id,
                        "elapsed_sec": res.get("elapsed_sec"),
                        "trades": metrics.get("trades"),
                        "total_return_pct": metrics.get("total_return_pct"),
                        "period": metrics.get("period"),
                    }
                )
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# 서버 전역에서 공유하는 단일 인스턴스
RUNS = RunStore(MAX_RUNS)


# ----------------------------------------------------------- 조회 헬퍼
def trades_for_code(result: Dict[str, Any], code: str) -> List[Dict[str, Any]]:
    """실행 결과에서 특정 종목의 거래만 골라낸다.

    종목코드는 ``042700`` 처럼 0 으로 시작할 수 있어 문자열 비교를 하되,
    숫자로 저장된 경우도 대비해 6자리 zero-fill 후 비교한다.
    """
    if not result or not code:
        return []
    want = str(code).strip()
    want_pad = want.zfill(6)
    out: List[Dict[str, Any]] = []
    for tr in result.get("trades") or []:
        got = str(tr.get("code", "")).strip()
        if got == want or got.zfill(6) == want_pad:
            out.append(tr)
    return out
