"""KRX 백테스트 엔진 (순수 파이썬 · 웹 의존성 없음).

ARCHITECTURE.md 1장의 공개 API 만 이 패키지에서 노출한다.

    from engine.data import MarcapStore
    from engine.indicators import compute, REGISTRY
    from engine.validate import validate_strategy
    from engine.backtest import run_backtest

`marcap` 데이터가 없어도 **import 는 반드시 성공**한다.
"""

from __future__ import annotations

from .errors import (
    DataUnavailable,
    DSLError,
    EngineError,
    IndicatorError,
    StrategyError,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "EngineError",
    "DataUnavailable",
    "StrategyError",
    "IndicatorError",
    "DSLError",
]
