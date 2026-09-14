"""无关问题判定（shared-docs/05 §3）单元测试：不启动服务、不访问网络。

重点验证三条硬约束：
1. `off` 模式**不产生任何调用**；
2. 解析失败 / 调用失败 → `None`（未判定），**不能返回 False**；
3. `sample` 模式下会话首问必判。
"""

import asyncio

from langchain_core.messages import AIMessage

from app.core.settings import Settings
from app.llm import classifier
from app.llm.classifier import parse_verdict, should_classify
from app.schemas.chat import CompleteRequest


def _settings(**overrides) -> Settings:
    base = {
        "ai_service_key": "test-key",
        "deepseek_api_key": "test-deepseek-key",
        "prompt_store_mode": "file",
        "prompt_store_file": "prompts/paimon.json",
        "ai_log_json": False,
        # 判定开关必须显式给默认值：不能依赖 .env / 环境变量，
        # 否则本地把 AI_CLASSIFY_MODE 改成 sample 后这条用例就会挂（2026-09-15 实际踩到）
        "ai_classify_mode": "off",
        "ai_classify_sample_rate": 0.0,
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------- 解析


def test_parse_plain_json():
    assert parse_verdict('{"offTopic": true, "reason": "other_domain"}') == (True, "other_domain")
    # offTopic=false 时 reason 归一为 game（不看模型怎么说）
    assert parse_verdict('{"offTopic": false, "reason": "other_domain"}') == (False, "game")


def test_parse_tolerates_code_fence_and_extra_text():
    text = '好的，分类结果如下：\n```json\n{"offTopic": true, "reason": "chitchat"}\n```\n以上。'
    assert parse_verdict(text) == (True, "chitchat")


def test_parse_normalizes_unknown_reason():
    assert parse_verdict('{"offTopic": true, "reason": "something_else"}') == (True, "unclear")
    assert parse_verdict('{"offTopic": true}') == (True, "unclear")


def test_parse_returns_none_when_unusable():
    # 这些都必须返回 None（= 未判定），绝不能落到 False
    unusable = [
        "",
        "   ",
        "我不确定",
        "{}",
        '{"offTopic": "yes"}',
        '{"reason": "chitchat"}',
        "[1,2]",
    ]
    for text in unusable:
        assert parse_verdict(text) is None, text


# ---------------------------------------------------------------- 抽样开关


def test_should_classify_off_by_default():
    s = _settings()
    assert s.ai_classify_mode == "off"
    assert should_classify(s, CompleteRequest(prompt="你好")) is False


def test_should_classify_all_always():
    s = _settings(ai_classify_mode="all")
    assert should_classify(s, CompleteRequest(prompt="你好")) is True
    # 有历史也一样判
    assert (
        should_classify(
            s, CompleteRequest(prompt="继续", history=[{"role": "user", "content": "你好"}])
        )
        is True
    )


def test_should_classify_sample_always_judges_first_turn():
    s = _settings(ai_classify_mode="sample", ai_classify_sample_rate=0.0)
    # 首问（无 history）必判
    assert should_classify(s, CompleteRequest(prompt="新手怎么开局")) is True
    # 非首问且抽样率为 0 → 不判
    assert (
        should_classify(
            s, CompleteRequest(prompt="继续", history=[{"role": "user", "content": "你好"}])
        )
        is False
    )


def test_should_classify_sample_rate_one_judges_everything():
    s = _settings(ai_classify_mode="sample", ai_classify_sample_rate=1.0)
    assert (
        should_classify(
            s, CompleteRequest(prompt="继续", history=[{"role": "user", "content": "你好"}])
        )
        is True
    )


# ---------------------------------------------------------------- 调用与容错


class _StubLLM:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return AIMessage(content=self._text)


def _run_classify(monkeypatch, stub: _StubLLM, **overrides):
    monkeypatch.setattr(classifier, "build_llm", lambda *_a, **_kw: stub)
    return asyncio.run(
        classifier.classify_off_topic(
            _settings(**overrides),
            CompleteRequest(prompt="帮我写一段 Python 排序"),
            asyncio.Semaphore(1),
        )
    )


def test_classify_returns_verdict(monkeypatch):
    stub = _StubLLM(text='{"offTopic": true, "reason": "other_domain"}')
    assert _run_classify(monkeypatch, stub, ai_classify_mode="all") == (True, "other_domain")
    assert stub.calls == 1


def test_classify_failure_returns_none_and_never_raises(monkeypatch):
    # 上游报错、输出不可解析，都必须得到 None（未判定），且不抛异常
    for stub in [
        _StubLLM(error=RuntimeError("上游 500")),
        _StubLLM(text="模型抽风了，什么都没有"),
    ]:
        assert _run_classify(monkeypatch, stub, ai_classify_mode="all") is None
