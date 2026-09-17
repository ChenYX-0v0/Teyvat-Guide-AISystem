"""进程内指标（滑动窗口计数，零外部依赖）。

为什么不用 Prometheus：P4.4 只要求"出问题能告警"。当前是单机常驻进程，先把**规则与投递**
做出来，指标本身保持"能被 `GET /metrics` 读出、也能被外部拉走"即可——Prometheus / 链路追踪
是 P3 的独立一项（见 05-开发路线图 §6 / §10），不在这里提前引依赖。

事件口径（规则与阈值见 `04-部署运维接入说明.md §8.3`）：
    request_total          每个 HTTP 响应 +1（分母）
    request_server_error   响应 5xx（含 /health 非 200 的情形）
    request_upstream_5xx   AIServiceError 里 code=502/503/504（上游/额度问题）
    persona_fallback       提示词来源完全不可用、已启用最小安全兜底
    prompt_store_failed    主服务内部提示词接口调用失败
    completion             一次 LLM 生成完成
    completion_cache_miss  该次 tokens_cached == 0（前缀缓存没命中）
"""

import threading
import time
from collections import deque
from typing import Any

# 事件名常量：各处只引用常量，避免手写字符串写错导致"指标永远为 0"
REQ_TOTAL = "request_total"
REQ_SERVER_ERROR = "request_server_error"
REQ_UPSTREAM_5XX = "request_upstream_5xx"
PERSONA_FALLBACK = "persona_fallback"
PROMPT_STORE_FAILED = "prompt_store_failed"
COMPLETION = "completion"
COMPLETION_CACHE_MISS = "completion_cache_miss"

# 主服务可重试的上游错误码（与 core/envelope.py 保持一致）
UPSTREAM_CODES = (502, 503, 504)

_ALL_EVENTS = (
    REQ_TOTAL,
    REQ_SERVER_ERROR,
    REQ_UPSTREAM_5XX,
    PERSONA_FALLBACK,
    PROMPT_STORE_FAILED,
    COMPLETION,
    COMPLETION_CACHE_MISS,
)


class SlidingWindowMetrics:
    """按事件名记录时间戳，支持"窗口内计数"与"进程内累计计数"。

    ⚠️ 每个事件名最多保留 `max_events` 个时间戳（有界内存）：超过后最老的会被丢弃，
    于是极高频下窗口计数可能少算。对"该不该告警"这种判断足够；要精确统计请走
    P4.1/P4.2 的落库报表（`chat_messages` 那条线）。
    """

    def __init__(self, max_events: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._counters: dict[str, int] = {}
        self._max_events = max_events
        self.started_at = time.time()

    def incr(self, name: str, amount: int = 1, *, now: float | None = None) -> None:
        if amount <= 0:
            return
        ts = time.time() if now is None else now
        with self._lock:
            dq = self._events.get(name)
            if dq is None:
                dq = deque(maxlen=self._max_events)
                self._events[name] = dq
            for _ in range(amount):
                dq.append(ts)
            self._counters[name] = self._counters.get(name, 0) + amount

    def count(self, name: str, window_seconds: float, *, now: float | None = None) -> int:
        """窗口内事件数；`window_seconds <= 0` 表示全量。"""
        ts_now = time.time() if now is None else now
        with self._lock:
            dq = self._events.get(name)
            if not dq:
                return 0
            if window_seconds <= 0:
                return len(dq)
            cutoff = ts_now - window_seconds
            # deque 内时间递增：从右往左数，遇到过期即可停
            n = 0
            for ts in reversed(dq):
                if ts < cutoff:
                    break
                n += 1
            return n

    def total(self, name: str) -> int:
        """进程内累计（不随窗口滑出而减少）。"""
        with self._lock:
            return self._counters.get(name, 0)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._counters.clear()
            self.started_at = time.time()

    def snapshot(self, window_seconds: float, *, now: float | None = None) -> dict[str, Any]:
        """窗口内计数 + 累计计数 + 派生比率（供 `/metrics` 与规则共用）。"""
        ts_now = time.time() if now is None else now
        window = {name: self.count(name, window_seconds, now=ts_now) for name in _ALL_EVENTS}
        total_req = window[REQ_TOTAL]
        completions = window[COMPLETION]
        return {
            "uptimeSeconds": round(ts_now - self.started_at, 1),
            "window": {"windowSeconds": window_seconds, **window},
            "lifetime": {name: self.total(name) for name in _ALL_EVENTS},
            "derived": {
                "upstream5xxRatio": (
                    round(window[REQ_UPSTREAM_5XX] / total_req, 4) if total_req else 0.0
                ),
                "cacheMissRatio": (
                    round(window[COMPLETION_CACHE_MISS] / completions, 4) if completions else 0.0
                ),
            },
        }


# 进程级单例：hook 点直接调下面的函数即可
metrics = SlidingWindowMetrics()


def record_request(status_code: int) -> None:
    """每个 HTTP 响应的入口（main.py 的中间件调用）。"""
    metrics.incr(REQ_TOTAL)
    if status_code >= 500:
        metrics.incr(REQ_SERVER_ERROR)


def record_ai_error(code: int) -> None:
    """AIServiceError 兜底出口（main.py 的异常处理器调用）。

    只统计"上游错误码分布"，5xx 总数由中间件按 HTTP 状态码计——两处都记会把 5xx 翻倍。
    """
    if code in UPSTREAM_CODES:
        metrics.incr(REQ_UPSTREAM_5XX)


def record_completion(cached_tokens: int) -> None:
    """一次生成完成；`cached_tokens == 0` 记为缓存未命中（用于发现前缀缓存被动态内容打散）。"""
    metrics.incr(COMPLETION)
    if int(cached_tokens or 0) <= 0:
        metrics.incr(COMPLETION_CACHE_MISS)


def record_persona_fallback() -> None:
    metrics.incr(PERSONA_FALLBACK)


def record_prompt_store_failure() -> None:
    metrics.incr(PROMPT_STORE_FAILED)
