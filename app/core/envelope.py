"""统一响应信封：{code, message, data}，code == 0 为成功。

与主服务 Go 侧信封逐字段对齐，保证前端与 `remoteAIClient` 无需区分来源。

约定：
- code 同时作为 HTTP 状态码返回；
- 4xx = 入参 / 鉴权问题，不可重试；
- 5xx / 504 = 上游模型问题，主服务可重试 1 次。
"""

from typing import Any


class ErrorCode:
    OK = 0

    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    NOT_FOUND = 404
    TOO_MANY_REQUESTS = 429

    INTERNAL = 500
    UPSTREAM_ERROR = 502
    UPSTREAM_UNAVAILABLE = 503
    UPSTREAM_TIMEOUT = 504


def ok(data: Any) -> dict[str, Any]:
    return {"code": ErrorCode.OK, "message": "ok", "data": data}


def fail(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}
