"""本地 JSON 提示词后端（开发用）。

文件格式：
    {
      "paimon_base":   { "content": "……", "version": 3 },
      "paimon_rules":  "……"            // 允许简写为纯字符串
    }
"""

import json
import pathlib
from collections.abc import Sequence

from app.core.logger import get_logger
from app.store.base import PromptRow

_log = get_logger()


class FilePromptStore:
    def __init__(self, path: str) -> None:
        self._path = pathlib.Path(path)

    def _load(self) -> dict:
        if not self._path.exists():
            _log.error("prompt_store.file_missing", path=str(self._path))
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.error("prompt_store.file_invalid", path=str(self._path), error=str(exc))
            return {}
        return raw if isinstance(raw, dict) else {}

    async def get_many(self, scene_keys: Sequence[str]) -> dict[str, PromptRow]:
        data = self._load()
        rows: dict[str, PromptRow] = {}
        for key in scene_keys:
            node = data.get(key)
            if node is None:
                continue
            if isinstance(node, str):
                rows[key] = PromptRow(scene_key=key, content=node, version=1)
            elif isinstance(node, dict) and node.get("content"):
                rows[key] = PromptRow(
                    scene_key=key,
                    content=str(node["content"]),
                    version=int(node.get("version", 1)),
                )
        return rows

    async def aclose(self) -> None:
        return None
