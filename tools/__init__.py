"""보조 도구 모음 (엔진·서버가 의존하지 않는 실행 스크립트).

    python -m tools.fetch_dart --help      # DART 재무 데이터 다운로더

이 패키지는 ``engine`` / ``server`` 어디에서도 import 하지 않는다.
반대로 ``tools`` 는 ``engine`` 을 읽어도 된다.
"""

from __future__ import annotations

__all__: list[str] = []
