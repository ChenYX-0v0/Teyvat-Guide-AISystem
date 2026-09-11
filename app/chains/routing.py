"""推理档位路由。

优先级：显式 profile > 显式 thinking > 触发词启发式 > 默认档位。
不做分类器（P1 用规则就够，成本低、可解释、可回归测试）。
"""

import re

from app.core.logger import get_logger
from app.llm.profiles import Profile, get_profile
from app.schemas.chat import CompleteRequest

_log = get_logger()

# 养成分析类意图：命中则开思考模式（数据分析更能吃住 reasoning）
#
# 注意：规则要覆盖"自然表达"，不能只覆盖专业术语。
# 实测漏判案例：「我的胡桃现在这样还需要提升什么？」——既无"圣遗物"也无"伤害"，
# 但是典型的养成分析诉求，漏判会退化成关思考的闲聊档，建议质量明显下降。
_ANALYSIS_PATTERNS = [
    # 明确的攻略意图
    r"圣遗物.*(怎么|如何|选|搭配|词条)",
    r"词条.*(优|收益|优先|推荐)",
    r"配队|阵容|队伍搭配|怎么组队",
    r"伤害|dps|输出.*(计算|提升|多少)",
    r"毕业|面板|属性.*(够|达标|标准)",
    r"深渊|深境螺旋|12[-—]?[123]层",
    r"命座.*(值得|建议|提升)",
    r"武器.*(选|推荐|哪个好)",
    r"练度|培养|养成.*(建议|规划|优先)",
    # 泛化的养成诉求（口语化问法）
    r"提升|改进|优化|变强|加强",
    r"怎么练|练什么|还要练|接着练|值得练|优先练",
    r"帮我(看看|分析|调|配|优化|规划)",
    r"值不值得|划算不划算",
]
_ANALYSIS_RE = re.compile("|".join(_ANALYSIS_PATTERNS), re.IGNORECASE)


def _looks_like_analysis(text: str) -> bool:
    return bool(_ANALYSIS_RE.search(text))


def decide_profile(req: CompleteRequest, default_profile: str = "chat") -> Profile:
    options = req.options

    if options and options.profile:
        return get_profile(options.profile)

    if options and options.thinking is not None:
        from app.llm.profiles import profile_for_thinking

        profile = profile_for_thinking(options.thinking)
        if options.reasoningEffort:
            # dataclass 是 frozen 的，用 replace 生成新档位
            from dataclasses import replace

            profile = replace(profile, reasoning_effort=options.reasoningEffort)
        return profile

    # 有玩家养成数据且问题是分析类 → 升档
    has_player_data = req.context is not None and req.context.player is not None
    if has_player_data and _looks_like_analysis(req.prompt):
        _log.info("routing.promoted_to_analysis", reason="analysis_intent")
        return get_profile("analysis")

    if _looks_like_analysis(req.prompt):
        _log.info("routing.promoted_to_analysis", reason="analysis_intent_no_player_data")
        return get_profile("analysis")

    return get_profile(default_profile)
