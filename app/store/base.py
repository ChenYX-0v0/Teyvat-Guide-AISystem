"""提示词存储抽象。

提示词属于业务数据，必须入库（项目强制规范）——AI 服务只读，不写。
本抽象提供两种后端：
- file：本地 JSON，开发默认（不依赖主服务即可跑通 P0）
- http：调主服务内部只读接口（生产推荐，保持"AI 服务不碰业务库"）
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PromptRow:
    scene_key: str
    content: str
    version: int = 1


@runtime_checkable
class PromptStore(Protocol):
    async def get_many(self, scene_keys: Sequence[str]) -> dict[str, PromptRow]:
        """返回命中的场景提示词；未命中的键不出现在结果里。"""
        ...

    async def aclose(self) -> None: ...
