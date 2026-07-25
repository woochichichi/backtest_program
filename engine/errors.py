"""엔진 공통 예외."""

from __future__ import annotations


class EngineError(Exception):
    """엔진 최상위 예외."""


class DataUnavailable(EngineError):
    """marcap 데이터가 없거나 요청 구간을 읽을 수 없을 때."""


class StrategyError(EngineError):
    """전략 DSL 이 스키마 검증을 통과하지 못했을 때.

    ``errors`` 속성에 ``[{"path": ..., "message": ...}]`` 형태의 목록을 담는다.
    """

    def __init__(self, message: str, errors=None):
        super().__init__(message)
        self.errors = list(errors or [])


class IndicatorError(EngineError):
    """알 수 없는 지표 / 잘못된 파라미터."""


class DSLError(EngineError):
    """조건식·수식 평가 실패."""


__all__ = [
    "EngineError",
    "DataUnavailable",
    "StrategyError",
    "IndicatorError",
    "DSLError",
]
