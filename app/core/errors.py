"""错误分类与异常定义。

目标：主服务能凭 code 判断"能不能重试"，且永远不会看到 LLM 的原始错误。
"""

from app.core.envelope import ErrorCode


class AIServiceError(Exception):
    """所有对外错误的基类。message 是**已脱敏**的、可给主服务看的文案。"""

    code: int = ErrorCode.INTERNAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRequestError(AIServiceError):
    """入参问题：不可重试。"""

    code = ErrorCode.BAD_REQUEST


class AuthError(AIServiceError):
    code = ErrorCode.UNAUTHORIZED


class UpstreamError(AIServiceError):
    """上游模型返回异常：可重试。"""

    code = ErrorCode.UPSTREAM_ERROR


class UpstreamUnavailableError(AIServiceError):
    """上游限流 / 服务不可用：可重试。"""

    code = ErrorCode.UPSTREAM_UNAVAILABLE


class UpstreamTimeoutError(AIServiceError):
    code = ErrorCode.UPSTREAM_TIMEOUT


def classify_upstream_error(exc: BaseException) -> AIServiceError:
    """把 OpenAI SDK / httpx 的异常翻译为对外的 AIServiceError。

    原始异常只记日志，绝不外传（强制项）。
    """
    name = type(exc).__name__
    text = str(exc).lower()

    if "timeout" in name.lower() or "timeout" in text:
        return UpstreamTimeoutError("上游模型响应超时")

    if "ratelimit" in name.lower() or "429" in text or "rate limit" in text:
        return UpstreamUnavailableError("上游模型限流")

    if (
        "authentication" in name.lower()
        or "401" in text
        or "invalid api key" in text
        or "missing credentials" in text
        or "api_key" in text
    ):
        # 上游密钥问题属于我们自己的运维问题，对主服务仍表现为 5xx（可重试/告警）
        return UpstreamError("上游模型鉴权失败")

    if "badrequest" in name.lower() or "400" in text:
        return UpstreamError("上游模型拒绝了请求")

    if "connection" in name.lower() or "connect" in text:
        return UpstreamUnavailableError("无法连接上游模型服务")

    if "overloaded" in text or "503" in text or "502" in text:
        return UpstreamUnavailableError("上游模型服务暂时不可用")

    return UpstreamError("上游模型调用失败")
