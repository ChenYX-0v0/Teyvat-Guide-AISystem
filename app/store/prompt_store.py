"""提示词服务：后端选择 + TTL 缓存。

缓存的作用不只是省一次网络：稳定的提示词内容能让 DeepSeek 的前缀缓存持续命中
（命中价约 $0.003/1M vs 未命中 $0.15/1M），所以**不要在提示词里注入时间戳、
随机数、每次变化的统计值**——动态内容一律拼在消息尾部。
"""

import time

from app.core.logger import get_logger
from app.core.settings import Settings
from app.store.base import PromptRow, PromptStore
from app.store.file_store import FilePromptStore
from app.store.http_store import HttpPromptStore

_log = get_logger()


def create_backend(settings: Settings) -> PromptStore:
    if settings.prompt_store_mode == "http":
        _log.info("prompt_store.backend", mode="http", url=settings.prompt_store_url)
        return HttpPromptStore(
            base_url=settings.prompt_store_url,
            key=settings.prompt_store_key,
            timeout=settings.prompt_store_timeout_seconds,
        )
    _log.info("prompt_store.backend", mode="file", path=settings.prompt_store_file)
    return FilePromptStore(settings.prompt_store_file)


class PromptStoreService:
    def __init__(self, backend: PromptStore, ttl_seconds: int = 60) -> None:
        self._backend = backend
        self._ttl = max(0, ttl_seconds)
        self._cache: dict[str, tuple[float, PromptRow]] = {}

    async def get_many(self, scene_keys: list[str]) -> dict[str, PromptRow]:
        now = time.monotonic()
        result: dict[str, PromptRow] = {}
        missing: list[str] = []

        for key in scene_keys:
            hit = self._cache.get(key)
            if hit and (self._ttl == 0 or now - hit[0] < self._ttl):
                result[key] = hit[1]
            else:
                missing.append(key)

        if missing:
            fetched = await self._backend.get_many(missing)
            for key, row in fetched.items():
                self._cache[key] = (now, row)
                result[key] = row

        return result

    async def aclose(self) -> None:
        await self._backend.aclose()
