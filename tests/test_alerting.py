"""告警（P4.4）单元测试：规则判定 / 冷却 / 恢复 / 窗口滑动。

对应 `04-部署运维接入说明.md §8.3` 的告警项，不启动服务、不访问网络
（webhook 用假接收器；时钟用假时钟，避免测试等待真实窗口）。
"""

import asyncio

from fastapi.testclient import TestClient

from app.core.alerting import AlertManager
from app.core.metrics import (
    COMPLETION,
    COMPLETION_CACHE_MISS,
    PERSONA_FALLBACK,
    PROMPT_STORE_FAILED,
    REQ_SERVER_ERROR,
    REQ_TOTAL,
    REQ_UPSTREAM_5XX,
    SlidingWindowMetrics,
    record_ai_error,
    record_completion,
    record_request,
)
from app.core.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "ai_service_key": "test-key",
        "deepseek_api_key": "test-deepseek-key",
        "prompt_store_mode": "file",
        "prompt_store_file": "prompts/paimon.json",
        "ai_log_json": False,
        # 显式给默认值，不依赖 .env / 环境变量（同 test_classify.py 踩过的坑）
        "ai_classify_mode": "off",
        "ai_classify_sample_rate": 0.0,
        "alert_enabled": False,
        "alert_webhook_url": "",
        "alert_window_seconds": 300,
        "alert_eval_interval_seconds": 30,
        "alert_cooldown_seconds": 1800,
        "alert_server_error_threshold": 3,
        "alert_5xx_ratio_threshold": 0.05,
        "alert_min_requests": 20,
        "alert_cache_min_samples": 20,
    }
    base.update(overrides)
    return Settings(**base)


class FakeClock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingNotifier:
    """假接收器：记录投递内容，可模拟投递失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self._fail = fail

    async def send(self, payload: dict) -> bool:
        if self._fail:
            raise RuntimeError("webhook 挂了")
        self.sent.append(payload)
        return True


def _build(**overrides):
    settings = _settings(**overrides)
    metrics = SlidingWindowMetrics()
    clock = FakeClock()
    notifier = RecordingNotifier()
    manager = AlertManager(settings, metrics, notifier=notifier, now_fn=clock)
    return manager, metrics, clock, notifier


# ---------------------------------------------------------------- 规则


def test_persona_fallback_fires_immediately():
    manager, metrics, clock, _ = _build()
    metrics.incr(PERSONA_FALLBACK, now=clock.now)

    actions = manager.evaluate()

    assert [a for a, _ in actions] == ["fired"]
    assert actions[0][1].rule == "prompt_source_fallback"
    assert actions[0][1].detail["events"] == 1


def test_prompt_store_failure_fires_immediately():
    manager, metrics, clock, _ = _build()
    metrics.incr(PROMPT_STORE_FAILED, now=clock.now)

    fired = [alert.rule for action, alert in manager.evaluate() if action == "fired"]

    assert fired == ["prompt_store_http_failed"]


def test_server_error_burst_needs_threshold():
    manager, metrics, clock, _ = _build(alert_server_error_threshold=3)
    metrics.incr(REQ_SERVER_ERROR, 2, now=clock.now)
    assert manager.evaluate() == []

    metrics.incr(REQ_SERVER_ERROR, 1, now=clock.now)
    fired = [alert.rule for action, alert in manager.evaluate() if action == "fired"]
    assert fired == ["server_error_burst"]


def test_upstream_ratio_ignores_small_sample():
    manager, metrics, clock, _ = _build(alert_min_requests=20)
    # 3 个请求里 1 个上游错误 = 33%，但样本不足 → 不告警（否则夜里会被误报吵醒）
    metrics.incr(REQ_TOTAL, 3, now=clock.now)
    metrics.incr(REQ_UPSTREAM_5XX, 1, now=clock.now)
    assert manager.evaluate() == []

    metrics.incr(REQ_TOTAL, 17, now=clock.now)  # 20 个请求
    metrics.incr(REQ_UPSTREAM_5XX, 1, now=clock.now)  # 2/20 = 10% > 5%
    fired = [alert.rule for action, alert in manager.evaluate() if action == "fired"]
    assert fired == ["upstream_5xx_ratio"]


def test_upstream_ratio_below_threshold_is_quiet():
    manager, metrics, clock, _ = _build(alert_min_requests=20)
    metrics.incr(REQ_TOTAL, 20, now=clock.now)
    metrics.incr(REQ_UPSTREAM_5XX, 1, now=clock.now)  # 5% 不在"大于"阈值内
    assert manager.evaluate() == []


def test_cache_degraded_needs_samples_and_full_miss():
    manager, metrics, clock, _ = _build(alert_cache_min_samples=20)
    metrics.incr(COMPLETION, 19, now=clock.now)
    metrics.incr(COMPLETION_CACHE_MISS, 19, now=clock.now)
    assert manager.evaluate() == []  # 样本不足

    metrics.incr(COMPLETION, 1, now=clock.now)
    metrics.incr(COMPLETION_CACHE_MISS, 1, now=clock.now)
    fired = [alert.rule for action, alert in manager.evaluate() if action == "fired"]
    assert fired == ["prompt_cache_degraded"]

    # 有一次命中过 → 不再认为缓存失效
    manager2, metrics2, clock2, _ = _build(alert_cache_min_samples=20)
    metrics2.incr(COMPLETION, 20, now=clock2.now)
    metrics2.incr(COMPLETION_CACHE_MISS, 19, now=clock2.now)
    assert manager2.evaluate() == []


def test_window_slides_out():
    manager, metrics, clock, _ = _build(alert_window_seconds=300)
    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    assert [a for a, _ in manager.evaluate()] == ["fired"]

    clock.now += 301  # 事件滑出窗口
    actions = manager.evaluate()
    assert [a for a, _ in actions] == ["resolved"]


# ---------------------------------------------------------------- 冷却与恢复


def test_cooldown_suppresses_repeat_then_reminds():
    manager, metrics, clock, notifier = _build(alert_cooldown_seconds=1800)
    metrics.incr(PERSONA_FALLBACK, now=clock.now)

    assert [a for a, _ in manager.evaluate()] == ["fired"]
    # 冷却期内反复评估：不再通知（防刷屏）
    clock.now += 60
    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    assert manager.evaluate() == []
    clock.now += 60
    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    assert manager.evaluate() == []

    # 超过冷却时间 → 提醒一次
    clock.now += 1800
    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    assert [a for a, _ in manager.evaluate()] == ["reminded"]


def test_resolved_notified_once():
    manager, metrics, clock, _ = _build()
    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    assert [a for a, _ in manager.evaluate()] == ["fired"]

    metrics.reset()
    assert [a for a, _ in manager.evaluate()] == ["resolved"]
    assert manager.evaluate() == []  # 恢复只报一次


def test_status_reports_firing_state():
    manager, metrics, clock, _ = _build()
    assert all(not item["firing"] for item in manager.status())

    metrics.incr(PERSONA_FALLBACK, now=clock.now)
    manager.evaluate()

    firing = {item["rule"]: item for item in manager.status()}
    assert firing["prompt_source_fallback"]["firing"] is True
    assert firing["prompt_source_fallback"]["lastNotifiedAt"] is not None
    assert firing["server_error_burst"]["firing"] is False


# ---------------------------------------------------------------- 投递


def test_tick_delivers_payload_and_survives_broken_notifier():
    manager, metrics, clock, notifier = _build()
    metrics.incr(PROMPT_STORE_FAILED, now=clock.now)

    asyncio.run(manager.tick())

    assert len(notifier.sent) == 1
    payload = notifier.sent[0]
    assert payload["status"] == "firing"
    assert payload["rule"] == "prompt_store_http_failed"
    assert payload["service"] == "teyvat-ai"

    # 投递通道故障不能把评估循环打断（run_forever 依赖这一点）
    settings = _settings()
    metrics2 = SlidingWindowMetrics()
    clock2 = FakeClock()
    broken = AlertManager(
        settings, metrics2, notifier=RecordingNotifier(fail=True), now_fn=clock2
    )
    metrics2.incr(PERSONA_FALLBACK, now=clock2.now)
    asyncio.run(broken.tick())


# ---------------------------------------------------------------- 指标 hook


def test_record_helpers_do_not_double_count_5xx():
    # hook 写的是模块级单例，断言要对着单例来
    from app.core import metrics as metrics_mod

    before_upstream = metrics_mod.metrics.total(REQ_UPSTREAM_5XX)
    before_server = metrics_mod.metrics.total(REQ_SERVER_ERROR)

    record_ai_error(502)
    assert metrics_mod.metrics.total(REQ_UPSTREAM_5XX) == before_upstream + 1
    # 5xx 总数只由中间件按 HTTP 状态码计：异常处理器不重复计入
    assert metrics_mod.metrics.total(REQ_SERVER_ERROR) == before_server

    record_request(502)
    assert metrics_mod.metrics.total(REQ_SERVER_ERROR) == before_server + 1

    metrics = SlidingWindowMetrics()
    metrics.incr(REQ_SERVER_ERROR, 1)
    assert metrics.count(REQ_SERVER_ERROR, 0) == 1


def test_record_completion_marks_cache_miss():
    m = SlidingWindowMetrics()
    m.incr(COMPLETION)
    m.incr(COMPLETION_CACHE_MISS)
    assert m.count(COMPLETION, 0) == 1
    assert m.count(COMPLETION_CACHE_MISS, 0) == 1

    # 真实 hook 写的是模块级单例：验证计数确实按"未命中"记账
    from app.core import metrics as metrics_mod

    before_all = metrics_mod.metrics.total(COMPLETION)
    before_miss = metrics_mod.metrics.total(COMPLETION_CACHE_MISS)
    record_completion(0)  # tokens_cached == 0 → 未命中
    record_completion(1024)  # 有命中 → 只加完成数

    assert metrics_mod.metrics.total(COMPLETION) == before_all + 2
    assert metrics_mod.metrics.total(COMPLETION_CACHE_MISS) == before_miss + 1


def test_metrics_snapshot_shape():
    metrics = SlidingWindowMetrics()
    metrics.incr(REQ_TOTAL, 10)
    metrics.incr(REQ_UPSTREAM_5XX, 1)
    metrics.incr(COMPLETION, 4)
    metrics.incr(COMPLETION_CACHE_MISS, 1)

    snap = metrics.snapshot(300)

    assert snap["window"]["request_total"] == 10
    assert snap["derived"]["upstream5xxRatio"] == 0.1
    assert snap["derived"]["cacheMissRatio"] == 0.25
    assert snap["lifetime"]["request_total"] == 10


# ---------------------------------------------------------------- 端点


def test_metrics_endpoint_requires_key():
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        resp = client.get("/metrics", headers={"X-AI-Key": "test-key"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert "metrics" in data and "alerting" in data
        rules = {item["rule"] for item in data["rules"]}
        assert "upstream_5xx_ratio" in rules
        assert "prompt_source_fallback" in rules
