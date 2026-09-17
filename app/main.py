"""FastAPI 入口。

约定：
- 所有响应走统一信封 {code, message, data}；
- HTTP 状态码 == envelope.code（4xx 不可重试 / 5xx 可重试）；
- 任何异常都不外泄内部细节与密钥。
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import complete, health
from app.api import metrics as metrics_api
from app.chains.orchestrator import Orchestrator
from app.core.alerting import AlertManager
from app.core.envelope import ErrorCode, fail
from app.core.errors import AIServiceError
from app.core.logger import configure_logging, get_logger
from app.core.metrics import record_ai_error, record_request
from app.core.settings import get_settings
from app.store.prompt_store import PromptStoreService, create_backend

_log = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.ai_log_level, settings.ai_log_json)

    # 配置错误 fail fast：宁可启动失败，也不要带病上线
    settings.validate_runtime()

    app.state.settings = settings
    app.state.prompt_store = PromptStoreService(
        create_backend(settings), ttl_seconds=settings.prompt_store_cache_ttl
    )
    app.state.orchestrator = Orchestrator(settings, app.state.prompt_store)

    # 告警（P4.4）：进程内评估 + 日志/webhook 双通道投递，规则见 04-部署运维接入说明 §8.3
    app.state.alerts = AlertManager(settings)
    alert_task: asyncio.Task[None] | None = None
    if settings.alert_enabled:
        alert_task = asyncio.create_task(app.state.alerts.run_forever())
    _log.info(
        "alerting.configured",
        enabled=settings.alert_enabled,
        webhook=bool(settings.alert_webhook_url),
        window_seconds=settings.alert_window_seconds,
        eval_interval_seconds=settings.alert_eval_interval_seconds,
    )
    if settings.alert_enabled and not settings.alert_webhook_url:
        _log.warning(
            "alerting.log_only",
            hint="未配置 ALERT_WEBHOOK_URL：告警只写日志（alert.fired / alert.resolved）",
        )

    _log.info(
        "service.started",
        version=__version__,
        host=settings.ai_server_host,
        port=settings.ai_server_port,
        model=settings.llm_model,
        prompt_store=settings.prompt_store_mode,
        insecure_dev=settings.allow_insecure_dev,
    )
    try:
        yield
    finally:
        if alert_task is not None:
            alert_task.cancel()
            with suppress(asyncio.CancelledError):
                await alert_task
        await app.state.prompt_store.aclose()
        _log.info("service.stopped")


app = FastAPI(
    title="teyvat-ai",
    description="AI 派蒙服务（LangChain + DeepSeek）",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,  # 不对外暴露文档
    redoc_url=None,
    openapi_url=None,
)

# ---------------------------------------------------------------- 异常处理


@app.exception_handler(AIServiceError)
async def _handle_ai_error(_request: Request, exc: AIServiceError) -> JSONResponse:
    _log.warning("request.failed", code=exc.code, message=exc.message)
    record_ai_error(exc.code)
    return JSONResponse(status_code=exc.code, content=fail(exc.code, exc.message))


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    detail = first.get("msg", "参数不合法")
    message = f"参数不合法：{loc} {detail}".strip()
    _log.warning("request.invalid", message=message)
    return JSONResponse(
        status_code=ErrorCode.BAD_REQUEST, content=fail(ErrorCode.BAD_REQUEST, message)
    )


@app.exception_handler(Exception)
async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    # 兜底：绝不把原始异常返回给主服务
    _log.errorw("request.unhandled", error=str(exc), error_type=type(exc).__name__)
    return JSONResponse(
        status_code=ErrorCode.INTERNAL,
        content=fail(ErrorCode.INTERNAL, "AI 服务内部错误"),
    )


# ---------------------------------------------------------------- 指标


@app.middleware("http")
async def _record_http_metrics(_request: Request, call_next: Any) -> Any:
    """每个 HTTP 响应计一次（5xx 按状态码判定，涵盖 /health 非 200）。"""
    try:
        response = await call_next(_request)
    except Exception:
        # 极端情况：异常没被上面三个处理器兜住 → 仍算一次 5xx，然后照常抛出
        record_request(500)
        raise
    record_request(response.status_code)
    return response


# ---------------------------------------------------------------- 路由

app.include_router(health.router)
app.include_router(complete.router)
app.include_router(metrics_api.router)

_settings = get_settings()
if _settings.cors_origin_list:  # 默认关闭：AI 服务不应被前端直连
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["POST", "GET"],
        allow_headers=["X-AI-Key", "Content-Type"],
    )


def main() -> None:  # pragma: no cover
    """命令行入口：`python -m app.main`。

    走这里才能读到 .env 的 host/port（直接用 uvicorn CLI 会忽略这些配置）。
    开发热重载：设置环境变量 AI_RELOAD=1。
    """
    import os

    import uvicorn

    settings = get_settings()
    reload_enabled = os.getenv("AI_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}

    uvicorn.run(
        "app.main:app",
        host=settings.ai_server_host,
        port=settings.ai_server_port,
        reload=reload_enabled,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
