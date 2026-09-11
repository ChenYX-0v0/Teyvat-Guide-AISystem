"""健康检查：GET /health（无需鉴权，供主服务与探针使用）。"""

from fastapi import APIRouter, Request

from app import __version__
from app.core.envelope import ok

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    return ok(
        {
            "status": "up",
            "version": __version__,
            "extras": {
                "model": settings.llm_model,
                "defaultProfile": settings.llm_default_profile,
                "promptStoreMode": settings.prompt_store_mode,
            },
        }
    )
