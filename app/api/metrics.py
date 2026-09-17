"""指标与告警状态：GET /metrics（P4.4）。

为什么挂 X-AI-Key：AI 服务只绑回环地址，但"任何内部面都要有鉴权"是项目既有约定
（见 `04-部署运维接入说明.md §7`）；运维侧用同一把 AI 密钥拉取即可。
"""

from fastapi import APIRouter, Depends, Request

from app import __version__
from app.core.envelope import ok
from app.core.metrics import metrics
from app.core.security import require_ai_key

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_ai_key)])


@router.get("/metrics")
async def get_metrics(request: Request) -> dict:
    settings = request.app.state.settings
    alerts = getattr(request.app.state, "alerts", None)
    return ok(
        {
            "version": __version__,
            "metrics": metrics.snapshot(settings.alert_window_seconds),
            # 规则状态与触发条件一起给出，方便对着 §8.3 人工核对
            "alerting": {
                "enabled": settings.alert_enabled,
                "webhookConfigured": bool(settings.alert_webhook_url),
                "windowSeconds": settings.alert_window_seconds,
                "evalIntervalSeconds": settings.alert_eval_interval_seconds,
                "cooldownSeconds": settings.alert_cooldown_seconds,
                "thresholds": {
                    "serverErrorBurst": settings.alert_server_error_threshold,
                    "upstream5xxRatio": settings.alert_5xx_ratio_threshold,
                    "minRequests": settings.alert_min_requests,
                    "cacheMinSamples": settings.alert_cache_min_samples,
                },
            },
            "rules": alerts.status() if alerts is not None else [],
        }
    )
