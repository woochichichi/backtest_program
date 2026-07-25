"""KRX Backtester 웹 서버 패키지.

- ``server.app``  : FastAPI 애플리케이션 (ARCHITECTURE.md 4장 HTTP API)
- ``server.runs`` : 백테스트 결과 인메모리 보관소 (최근 10건 LRU)
- ``server.ai``   : 자연어 -> 전략 DSL 변환 (Anthropic SDK)
"""

__all__ = ["app", "runs", "ai"]
__version__ = "1.0.0"
