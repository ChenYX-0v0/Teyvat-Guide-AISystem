"""结构化日志。

输出 JSON 行，字段名对齐主服务 Go 侧 zap 的约定（level / ts / msg / logger），
便于两边用同一套采集规则解析。

同时提供 `errorw / warnw / infow / debugw` 别名，与主服务 `logger.L().Errorw(...)`
的写法保持一致——两个项目看日志不用切换思维。
"""

import logging
import sys
from typing import Any

import structlog

_configured = False

# 需要脱敏的字段名（小写匹配）
_REDACT_KEYS = {"api_key", "authorization", "x-ai-key", "x-internal-key", "deepseek_api_key"}


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***"
    return event_dict


class ZapLikeLogger:
    """薄包装：透传 structlog 方法，并补上 zap 风格的 *w 别名。"""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def debugw(self, event: str, **kw: Any) -> Any:
        return self._inner.debug(event, **kw)

    def infow(self, event: str, **kw: Any) -> Any:
        return self._inner.info(event, **kw)

    def warnw(self, event: str, **kw: Any) -> Any:
        return self._inner.warning(event, **kw)

    def errorw(self, event: str, **kw: Any) -> Any:
        return self._inner.error(event, **kw)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _configured
    if _configured:
        return

    resolved = level.upper() if isinstance(level, str) else "INFO"
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved)

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(resolved)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "ai-service") -> ZapLikeLogger:
    return ZapLikeLogger(structlog.get_logger(name))
