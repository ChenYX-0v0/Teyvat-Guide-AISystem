"""纯函数单元测试：不启动服务、不访问网络。"""

from langchain_core.messages import AIMessage

from app.chains.context_render import render_context
from app.chains.orchestrator import _extract_usage
from app.chains.routing import decide_profile
from app.core.envelope import ErrorCode, fail, ok
from app.core.tokens import estimate_text_tokens
from app.llm.profiles import PROFILES
from app.schemas.chat import CompleteRequest
from app.schemas.context import RequestContext


def test_envelope_shape():
    assert ok({"a": 1}) == {"code": 0, "message": "ok", "data": {"a": 1}}
    assert fail(ErrorCode.UPSTREAM_UNAVAILABLE, "上游模型限流") == {
        "code": 503,
        "message": "上游模型限流",
        "data": None,
    }


def test_profiles_contract():
    # 思考模式下不发送 temperature（发了也无效，容易误导）
    assert PROFILES["analysis"].thinking is True
    assert PROFILES["analysis"].temperature is None
    assert PROFILES["analysis"].thinking_param["type"] == "enabled"

    # 闲聊必须关闭思考，否则语气调不了
    assert PROFILES["chat"].thinking is False
    assert PROFILES["chat"].temperature is not None
    assert PROFILES["chat"].thinking_param == {"type": "disabled"}


def test_routing_prefers_explicit_profile():
    req = CompleteRequest(prompt="胡桃圣遗物怎么选", options={"profile": "chat"})
    assert decide_profile(req).name == "chat"


def test_routing_promotes_analysis_intent():
    req = CompleteRequest(prompt="胡桃的圣遗物词条怎么选比较好？")
    assert decide_profile(req).name == "analysis"


def test_routing_keeps_casual_chat():
    req = CompleteRequest(prompt="你好呀派蒙")
    assert decide_profile(req).name == "chat"


def test_routing_promotes_on_colloquial_improvement_wording():
    """口语化的养成诉求也要升档——纯术语规则会漏掉这类问法。"""
    for prompt in [
        "我的胡桃现在这样还需要提升什么？",
        "帮我看看我这队还能怎么优化",
        "接下来应该优先练哪个角色",
        "这套阵容怎么配比较好",
    ]:
        assert decide_profile(CompleteRequest(prompt=prompt)).name == "analysis", prompt


def test_routing_stays_chat_for_plain_questions():
    for prompt in ["派蒙你好呀", "今天天气真好", "你最喜欢吃什么"]:
        assert decide_profile(CompleteRequest(prompt=prompt)).name == "chat", prompt


def test_routing_respects_explicit_thinking():
    req = CompleteRequest(prompt="你好", options={"thinking": True})
    assert decide_profile(req).name == "analysis"


def test_usage_extraction_from_raw_metadata():
    msg = AIMessage(
        content="hi",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 812,
                "completion_tokens": 233,
                "total_tokens": 1045,
                "prompt_cache_hit_tokens": 700,
                "completion_tokens_details": {"reasoning_tokens": 120},
            }
        },
    )
    usage = _extract_usage(msg)
    assert (usage.input, usage.output, usage.total) == (812, 233, 1045)
    assert usage.cached == 700
    assert usage.reasoning == 120


def test_usage_extraction_from_usage_metadata():
    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 8},
        },
    )
    usage = _extract_usage(msg)
    assert (usage.input, usage.output, usage.total, usage.cached) == (10, 5, 15, 8)


def test_render_context_renders_player_profile():
    ctx = RequestContext(
        **{
            "goal": "深境螺旋 12 层",
            "player": {
                "uid": "100000001",
                "level": 58,
                "characters": [
                    {
                        "name": "胡桃",
                        "level": 90,
                        "constellation": 1,
                        "talents": {"normal": 9, "skill": 9, "burst": 8},
                        "weapon": {"name": "护摩之杖", "refine": 1, "level": 90},
                        "artifacts": [{"set": "炽烈的炎之魔女", "count": 4}],
                    }
                ],
            },
        }
    )
    text = render_context(ctx, budget_tokens=1500)
    assert "深境螺旋 12 层" in text
    assert "胡桃 Lv.90 命座1 天赋普攻9/战技9/爆发8" in text
    assert "护摩之杖 精1 Lv.90" in text
    assert "炽烈的炎之魔女×4" in text


def test_render_context_respects_budget_by_dropping_whole_characters():
    chars = [{"name": f"角色{i}", "level": 90} for i in range(200)]
    ctx = RequestContext(**{"player": {"characters": chars}})
    text = render_context(ctx, budget_tokens=200)

    assert "角色：" in text
    assert "已省略" in text          # 明确告知模型有角色被省略
    assert "（已截断）" not in text   # 不允许退化成字符级截断（会留下残缺条目）
    assert "角色199" not in text      # 末尾角色被整条丢弃
    # 真正的约束是 token 预算，不是字符数（中文约 1 token/字，ASCII 约 1 token/4 字）
    assert estimate_text_tokens(text) <= 200


def test_render_context_keeps_all_characters_when_budget_is_enough():
    chars = [{"name": "胡桃", "level": 90, "constellation": 1}]
    ctx = RequestContext(**{"player": {"characters": chars}})
    text = render_context(ctx, budget_tokens=1500)

    assert "已省略" not in text
    assert "胡桃 Lv.90 命座1" in text


def test_render_context_falls_back_to_truncation_when_header_alone_overflows():
    ctx = RequestContext(**{"player": {"nickname": "很长的昵称" * 100, "characters": []}})
    text = render_context(ctx, budget_tokens=20)

    # 已经没有角色可丢，只能字符级截断兜底
    assert text.endswith("（已截断）")
    assert len(text) < 200
