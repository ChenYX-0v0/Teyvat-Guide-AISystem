"""推理档位（profile）—— 替代旧设计的"多模型"路由。

DeepSeek V4.1 Flash 只有**一个**模型 ID `deepseek-flash`，
"是否推理"由请求参数控制：

    thinking: { type: "enabled" | "disabled",   # 默认 enabled
                reasoning_effort: "none" | "low" | "high" | "max" }  # 默认 high

关键差异（决定档位怎么切）：
- **思考模式下 `temperature` 完全无效** → 需要调语气（派蒙人设）的场景必须关闭思考；
- **思考模式不支持 `tool_choice=required` / 指定具名工具** → 工具调用必须关闭思考；
- 非思考模式下 `top_p` 被强制为 1.0，传入值被忽略；
- `frequency_penalty` / `presence_penalty` 已不再支持，一律不发。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    thinking: bool
    reasoning_effort: str
    temperature: float | None
    max_tokens: int
    note: str

    @property
    def thinking_param(self) -> dict[str, str]:
        """构造 DeepSeek 的 `thinking` 请求参数。"""
        if not self.thinking:
            return {"type": "disabled"}
        return {"type": "enabled", "reasoning_effort": self.reasoning_effort}


PROFILES: dict[str, Profile] = {
    # 日常对话：语气可调，首字快，成本低
    "chat": Profile(
        name="chat",
        thinking=False,
        reasoning_effort="none",
        temperature=0.85,
        max_tokens=1024,
        note="派蒙日常闲聊/答疑",
    ),
    # 养成分析 / 配队 / 词条收益：需要数值推理
    "analysis": Profile(
        name="analysis",
        thinking=True,
        reasoning_effort="high",
        temperature=None,  # 思考模式下无效，显式不发
        max_tokens=4096,
        note="养成数据分析、配队建议",
    ),
    # 工具调用（P3 LangGraph）：思考模式不支持 required
    "tool": Profile(
        name="tool",
        thinking=False,
        reasoning_effort="none",
        temperature=0.2,
        max_tokens=2048,
        note="工具调用",
    ),
    # 结构化输出（JSON Output）
    "json": Profile(
        name="json",
        thinking=False,
        reasoning_effort="none",
        temperature=0.1,
        max_tokens=2048,
        note="结构化输出",
    ),
    # 无关问题判定（内部使用，见 shared-docs/05-AI使用边界设计（匿名·配额·内容治理）.md §3.2）：
    # 关闭思考、温度 0、输出极短；**不放进对外 profile 枚举**，调用方无法主动指定。
    "classify": Profile(
        name="classify",
        thinking=False,
        reasoning_effort="none",
        temperature=0.0,
        max_tokens=16,
        note="无关问题判定",
    ),
}

DEFAULT_PROFILE = "chat"


def get_profile(name: str | None) -> Profile:
    if not name:
        return PROFILES[DEFAULT_PROFILE]
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])


def profile_for_thinking(thinking: bool) -> Profile:
    return PROFILES["analysis"] if thinking else PROFILES["chat"]
