"""请求附带的业务上下文（可选扩展字段）。

- 向后兼容：该字段不传时行为与原契约完全一致。
- 定位：主服务推送的「用户私有数据快照」，AI 服务**只读不落库**（见架构文档 §12.2 模式 A）。
- 结构保持宽松（extra="allow"），主服务新增字段不需要 AI 服务发版。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlayerWeapon(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    refine: int | None = None
    level: int | None = None


class PlayerArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    set: str | None = None
    count: int | None = None
    mainStats: dict[str, str] = Field(default_factory=dict)


class PlayerCharacter(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    level: int | None = None
    constellation: int | None = None
    talents: dict[str, int] = Field(default_factory=dict)
    weapon: PlayerWeapon | None = None
    artifacts: list[PlayerArtifact] = Field(default_factory=list)


class PlayerProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    uid: str | None = None
    nickname: str | None = None
    level: int | None = None
    characters: list[PlayerCharacter] = Field(default_factory=list)


class RequestContext(BaseModel):
    """一次请求携带的上下文。

    player   结构化玩家数据（主服务组装）
    goal     本次提问的目标场景，如「深境螺旋 12 层」
    summary  主服务预渲染的自由文本（当结构化数据不适合时使用）
    """

    model_config = ConfigDict(extra="allow")

    player: PlayerProfile | None = None
    goal: str | None = None
    summary: str | None = None

    def is_empty(self) -> bool:
        return self.player is None and not self.goal and not self.summary

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
