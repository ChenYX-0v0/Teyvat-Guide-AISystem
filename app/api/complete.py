"""补全接口：POST /v1/complete 与 POST /v1/complete/stream。

全部挂 X-AI-Key 鉴权（无鉴权不得暴露 /v1/*）。
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_orchestrator
from app.chains.orchestrator import Orchestrator
from app.core.envelope import ok
from app.core.security import require_ai_key
from app.schemas.chat import CompleteRequest

router = APIRouter(prefix="/v1", tags=["complete"], dependencies=[Depends(require_ai_key)])

OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关闭 Nginx 缓冲，否则流式会被攒包
    "X-Accel-Buffering": "no",
}


@router.post("/complete")
async def complete(payload: CompleteRequest, orchestrator: OrchestratorDep) -> dict:
    data = await orchestrator.complete(payload)
    return ok(data.model_dump())


@router.post("/complete/stream")
async def complete_stream(
    payload: CompleteRequest,
    request: Request,
    orchestrator: OrchestratorDep,
) -> StreamingResponse:
    async def event_source():
        async for event, body in orchestrator.stream(payload):
            if await request.is_disconnected():
                return
            data = json.dumps(body, ensure_ascii=False)
            yield f"event: {event}\ndata: {data}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream", headers=_SSE_HEADERS)
