"""对外契约模型：POST /v1/complete。

字段命名严格对齐对外契约；扩展字段一律可选，保证向后兼容。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.context import RequestContext

Role = Literal["user", "assistant"]
ProfileName = Literal["chat", "analysis", "tool", "json"]


class ChatMessage(BaseModel):
    """history 只允许 user / assistant；system 走独立字段（基础契约）。"""

    model_config = ConfigDict(extra="ignore")

    role: Role
    content: str = Field(min_length=0, max_length=8000)


class Options(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    maxTokens: int | None = Field(default=None, ge=1, le=384_000)  # noqa: N815 - 对齐契约

    # --- 扩展（可选） ---
    profile: ProfileName | None = None
    """推理档位。chat=关闭思考可调语气；analysis=开启思考做数据分析；
    tool=工具调用（思考模式不支持 tool_choice=required）；json=结构化输出。"""

    thinking: bool | None = None
    """显式开关思考模式。优先级低于 profile。"""

    reasoningEffort: Literal["none", "low", "high", "max"] | None = None  # noqa: N815


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    system: str | None = None
    history: list[ChatMessage] = Field(default_factory=list)
    prompt: str = Field(min_length=1)
    context: RequestContext | None = None
    options: Options | None = None

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("prompt 不能为空")
        return stripped


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0
    total: int = 0
    cached: int = 0
    """命中的前缀缓存 tokens —— 单价约为未命中的 1/50，务必单独统计。"""
    reasoning: int = 0
    """思考模式产生的 tokens（计入 output）。"""


class CompleteData(BaseModel):
    reply: str
    model: str
    tokens: TokenUsage
    finishReason: str
    elapsedMs: int

    # --- 扩展（可选） ---
    profile: str | None = None
    thinking: bool | None = None
    sources: list[str] | None = None
    """P2 RAG 引用来源。"""


class HealthData(BaseModel):
    status: str
    version: str
    extras: dict[str, Any] | None = None
