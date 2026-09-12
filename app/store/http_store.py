"""主服务内部只读接口后端（生产推荐）。

期望的主服务接口（需另行在主服务实现，见 shared-docs/03-主服务对接说明.md）：
    GET /internal/v1/ai/prompts?scenes=paimon_base,paimon_rules
    Header: X-Internal-Key: <key>
    → {"code":0,"message":"ok","data":{"items":[
         {"sceneKey":"paimon_base","content":"...","version":3}
       ]}}
"""

from collections.abc import Sequence

import httpx

from app.core.errors import UpstreamUnavailableError
from app.core.logger import get_logger
from app.store.base import PromptRow

_log = get_logger()


class HttpPromptStore:
    def __init__(self, base_url: str, key: str, timeout: float = 3.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"X-Internal-Key": key} if key else {},
        )

    async def get_many(self, scene_keys: Sequence[str]) -> dict[str, PromptRow]:
        if not scene_keys:
            return {}
        url = f"{self._base_url}/internal/v1/ai/prompts"
        params = {"scenes": ",".join(scene_keys)}
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            _log.error("prompt_store.http_failed", url=url, error=str(exc))
            raise UpstreamUnavailableError("获取提示词失败") from exc

        if payload.get("code") != 0:
            _log.error("prompt_store.http_bad_envelope", body=payload)
            raise UpstreamUnavailableError("获取提示词失败")

        rows: dict[str, PromptRow] = {}
        for item in (payload.get("data") or {}).get("items") or []:
            key = item.get("sceneKey") or item.get("scene_key")
            content = item.get("content")
            if not key or not content:
                continue
            rows[key] = PromptRow(
                scene_key=key,
                content=str(content),
                version=int(item.get("version", 1)),
            )
        return rows

    async def aclose(self) -> None:
        await self._client.aclose()
