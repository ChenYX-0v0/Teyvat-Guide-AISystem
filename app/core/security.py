"""X-AI-Key 鉴权。

无鉴权不得暴露 /v1/*。此处用常量时间比较防时序侧信道。
"""

import hmac

from fastapi import Header, Request

from app.core.errors import AuthError
from app.core.settings import Settings


async def require_ai_key(
    request: Request,
    x_ai_key: str | None = Header(default=None, alias="X-AI-Key"),
) -> None:
    settings: Settings = request.app.state.settings

    if settings.allow_insecure_dev and not settings.ai_service_key:
        return

    if not x_ai_key:
        raise AuthError("缺少 X-AI-Key")

    if not hmac.compare_digest(x_ai_key, settings.ai_service_key):
        raise AuthError("X-AI-Key 无效")
