"""接口层测试：鉴权、信封、入参校验、编排链路（LLM 被 mock）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.main import app

HEADERS = {"X-AI-Key": "test-key"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_llm(monkeypatch):
    """替换 LLM 工厂，记录被选中的档位，避免真实调用上游。"""

    captured: dict[str, Any] = {}

    class FakeLLM:
        model_name = "deepseek-flash"

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(
                content="派蒙来啦！",
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_cache_hit_tokens": 80,
                    }
                },
            )

        async def astream(self, messages):  # pragma: no cover - 流式单测另行补充
            yield AIMessage(content="派蒙")
            yield AIMessage(content="来啦！")

    def fake_build_llm(settings, profile, **kwargs):
        captured["profile"] = profile.name
        captured["streaming"] = kwargs.get("streaming")
        return FakeLLM()

    monkeypatch.setattr("app.chains.orchestrator.build_llm", fake_build_llm)
    return captured


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "up"
    assert "key" not in str(body).lower()  # 不泄漏任何密钥字段


def test_missing_key_rejected(client):
    resp = client.post("/v1/complete", json={"prompt": "你好"})
    assert resp.status_code == 401
    assert resp.json() == {"code": 401, "message": "缺少 X-AI-Key", "data": None}


def test_wrong_key_rejected(client):
    resp = client.post("/v1/complete", json={"prompt": "你好"}, headers={"X-AI-Key": "bad"})
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


def test_invalid_body_returns_400_envelope(client):
    resp = client.post("/v1/complete", json={}, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["code"] == 400


def test_history_role_restricted_to_user_assistant(client):
    resp = client.post(
        "/v1/complete",
        json={"prompt": "你好", "history": [{"role": "system", "content": "越权"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_complete_success_envelope(client, fake_llm):
    resp = client.post(
        "/v1/complete",
        json={
            "system": "补充指令",
            "history": [{"role": "user", "content": "胡桃怎么玩？"}],
            "prompt": "那圣遗物呢",
            "options": {"profile": "chat"},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0

    data = body["data"]
    assert data["reply"] == "派蒙来啦！"
    assert data["model"] == "deepseek-flash"
    assert data["tokens"] == {
        "input": 100,
        "output": 20,
        "total": 120,
        "cached": 80,
        "reasoning": 0,
    }
    assert data["finishReason"] == "stop"
    assert isinstance(data["elapsedMs"], int)
    assert fake_llm["profile"] == "chat"

    # system 中必须已注入派蒙人格（来自提示词来源，而非硬编码在业务代码里）
    system_text = fake_llm["messages"][0].content
    assert "派蒙" in system_text
    assert "补充指令" in system_text


def test_analysis_keyword_promotes_profile(client, fake_llm):
    resp = client.post(
        "/v1/complete",
        json={"prompt": "胡桃的圣遗物词条怎么选？"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert fake_llm["profile"] == "analysis"


def test_oversized_prompt_rejected(client):
    resp = client.post("/v1/complete", json={"prompt": "啊" * 5000}, headers=HEADERS)
    assert resp.status_code == 400
    assert "超长" in resp.json()["message"]


def test_stream_returns_sse_events(client, fake_llm):
    resp = client.post(
        "/v1/complete/stream",
        json={"prompt": "你好", "options": {"profile": "chat"}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: delta" in resp.text
    assert "event: done" in resp.text
