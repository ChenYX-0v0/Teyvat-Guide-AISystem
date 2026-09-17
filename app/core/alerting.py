"""告警规则与投递（P4.4；规则口径见 `04-部署运维接入说明.md §8.3`）。

三条设计取舍：

1. **规则在进程内评估**（单机常驻、零外部依赖）：`GET /metrics` 同时暴露"当前规则状态"，
   便于人工核对，也能被外部探针复用；
2. **投递两条腿**：① 永远打日志（`alert.fired` / `alert.reminded` / `alert.resolved`）——
   没配 webhook 也能在日志里看见；② 配了 `ALERT_WEBHOOK_URL` 再 POST 一份**通用 JSON**。
   企业微信 / 钉钉 / 飞书的机器人各有自己的报文格式，用一个几行的转发函数适配即可，
   本项目不内置（避免"看起来支持、实际投不出去"；示例见 `04 §8.3`）；
3. **冷却 + 恢复**：同一规则在 `ALERT_COOLDOWN_SECONDS` 内不重复通知（防刷屏）；恢复正常时发一次
   `alert.resolved`（避免"响了没人管、恢复了也不知道"）。

**本模块覆盖不到的，必须靠外部**：
- **进程整体挂掉 / 端口不通**：进程内自监控发不出任何东西 → 用 Nginx、云监控或定时
  curl 探 `/health`；本模块覆盖的是"**进程活着但功能坏掉**"这一类
  （提示词来源故障、上游持续 5xx、缓存失效）。
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app import __version__
from app.core.logger import get_logger
from app.core.metrics import (
    COMPLETION,
    COMPLETION_CACHE_MISS,
    PERSONA_FALLBACK,
    PROMPT_STORE_FAILED,
    REQ_SERVER_ERROR,
    REQ_TOTAL,
    REQ_UPSTREAM_5XX,
    SlidingWindowMetrics,
)
from app.core.metrics import (
    metrics as default_metrics,
)
from app.core.settings import Settings

_log = get_logger()

# 规则 key → 中文名（告警文案与 /metrics 都用它）
RULE_NAMES: dict[str, str] = {
    "prompt_source_fallback": "提示词来源完全不可用（已启用最小安全兜底，必须马上修）",
    "prompt_store_http_failed": "主服务提示词接口调用失败",
    "server_error_burst": "服务 5xx 突发（含 /health 非 200）",
    "upstream_5xx_ratio": "上游 5xx 占比超阈值",
    "prompt_cache_degraded": "提示词前缀缓存长期未命中（通常是有动态内容混进了稳定段）",
}


@dataclass(frozen=True)
class Alert:
    """一次命中：规则 key + 证据明细（明细会进日志与 webhook，便于直接定位）。"""

    rule: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return RULE_NAMES.get(self.rule, self.rule)


# ---------------------------------------------------------------- 规则
# 每条规则签名统一：命中返回 Alert，否则 None。规则本身不做 IO，方便单测。


def _rule_prompt_source_fallback(
    settings: Settings, m: SlidingWindowMetrics, now: float
) -> Alert | None:
    n = m.count(PERSONA_FALLBACK, settings.alert_window_seconds, now=now)
    if n > 0:
        return Alert("prompt_source_fallback", {"events": n})
    return None


def _rule_prompt_store_http_failed(
    settings: Settings, m: SlidingWindowMetrics, now: float
) -> Alert | None:
    n = m.count(PROMPT_STORE_FAILED, settings.alert_window_seconds, now=now)
    if n > 0:
        return Alert("prompt_store_http_failed", {"events": n})
    return None


def _rule_server_error_burst(
    settings: Settings, m: SlidingWindowMetrics, now: float
) -> Alert | None:
    n = m.count(REQ_SERVER_ERROR, settings.alert_window_seconds, now=now)
    if n >= settings.alert_server_error_threshold:
        return Alert(
            "server_error_burst",
            {"count": n, "threshold": settings.alert_server_error_threshold},
        )
    return None


def _rule_upstream_5xx_ratio(
    settings: Settings, m: SlidingWindowMetrics, now: float
) -> Alert | None:
    window = settings.alert_window_seconds
    total = m.count(REQ_TOTAL, window, now=now)
    if total < settings.alert_min_requests:
        # 样本太少：1/3 也是 33%，判占比只会误报
        return None
    bad = m.count(REQ_UPSTREAM_5XX, window, now=now)
    ratio = bad / total
    if ratio > settings.alert_5xx_ratio_threshold:
        return Alert(
            "upstream_5xx_ratio",
            {
                "ratio": round(ratio, 4),
                "threshold": settings.alert_5xx_ratio_threshold,
                "upstream5xx": bad,
                "requests": total,
            },
        )
    return None


def _rule_prompt_cache_degraded(
    settings: Settings, m: SlidingWindowMetrics, now: float
) -> Alert | None:
    window = settings.alert_window_seconds
    completions = m.count(COMPLETION, window, now=now)
    if completions < settings.alert_cache_min_samples:
        return None
    miss = m.count(COMPLETION_CACHE_MISS, window, now=now)
    if miss >= completions:  # 窗口内一次都没命中过
        return Alert(
            "prompt_cache_degraded",
            {"completions": completions, "cacheMiss": miss},
        )
    return None


RULES = (
    _rule_prompt_source_fallback,
    _rule_prompt_store_http_failed,
    _rule_server_error_burst,
    _rule_upstream_5xx_ratio,
    _rule_prompt_cache_degraded,
)


# ---------------------------------------------------------------- 投递


class WebhookNotifier:
    """把告警 POST 成一个通用 JSON（自建接收端 / 云函数 / 日志网关都能吃）。"""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout

    async def send(self, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=payload)
                resp.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError) as exc:
            _log.errorw("alert.webhook_failed", error=str(exc), rule=payload.get("rule"))
            return False


class AlertManager:
    """评估规则 → 状态迁移（触发 / 冷却后提醒 / 恢复）→ 投递。"""

    def __init__(
        self,
        settings: Settings,
        metrics: SlidingWindowMetrics | None = None,
        *,
        notifier: WebhookNotifier | None = None,
        now_fn: Any = time.time,
    ) -> None:
        self._settings = settings
        self._metrics = metrics if metrics is not None else default_metrics
        self._now = now_fn
        self._notifier = notifier
        if self._notifier is None and settings.alert_webhook_url:
            self._notifier = WebhookNotifier(settings.alert_webhook_url)
        self._firing: dict[str, float] = {}  # rule → 上次通知时间
        self._last: dict[str, Alert] = {}  # rule → 最近一次命中的明细

    # ---------------- 状态查询（供 /metrics） ----------------

    def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rule, name in RULE_NAMES.items():
            last_sent = self._firing.get(rule)
            out.append(
                {
                    "rule": rule,
                    "name": name,
                    "firing": last_sent is not None,
                    "lastNotifiedAt": (
                        datetime.fromtimestamp(last_sent, tz=UTC).isoformat()
                        if last_sent is not None
                        else None
                    ),
                    "detail": self._last.get(rule).detail if self._last.get(rule) else None,
                }
            )
        return out

    # ---------------- 评估（纯逻辑，可单测） ----------------

    def evaluate(self, *, now: float | None = None) -> list[tuple[str, Alert]]:
        """评估一轮，返回 [(action, alert)]；action ∈ fired / reminded / resolved。

        不产生任何 IO——投递在 `tick()` 里做，这样规则与冷却逻辑能被单测精确控制。
        """
        ts_now = self._now() if now is None else now
        hits: dict[str, Alert] = {}
        for rule in RULES:
            alert = rule(self._settings, self._metrics, ts_now)
            if alert is not None:
                hits[alert.rule] = alert

        actions: list[tuple[str, Alert]] = []
        for rule, alert in hits.items():
            last_sent = self._firing.get(rule)
            self._last[rule] = alert
            if last_sent is None:
                self._firing[rule] = ts_now
                actions.append(("fired", alert))
            elif ts_now - last_sent >= self._settings.alert_cooldown_seconds:
                self._firing[rule] = ts_now
                actions.append(("reminded", alert))

        for rule in list(self._firing):
            if rule in hits:
                continue
            alert = self._last.pop(rule, None) or Alert(rule, {})
            del self._firing[rule]
            actions.append(("resolved", alert))
        return actions

    async def tick(self) -> list[tuple[str, Alert]]:
        actions = self.evaluate()
        for action, alert in actions:
            await self._deliver(action, alert)
        return actions

    async def _deliver(self, action: str, alert: Alert) -> None:
        text = f"[teyvat-ai] {'告警' if action != 'resolved' else '已恢复'}：{alert.name}"
        if action == "resolved":
            _log.infow("alert.resolved", rule=alert.rule, name=alert.name, text=text)
        else:
            # 告警必须进 ERROR 日志：没配 webhook 时这是唯一可见通道
            _log.errorw(
                "alert.fired" if action == "fired" else "alert.reminded",
                rule=alert.rule,
                name=alert.name,
                text=text,
                **alert.detail,
            )
        if self._notifier is not None:
            try:
                await self._notifier.send(
                    {
                        "service": "teyvat-ai",
                        "version": __version__,
                        "status": "firing" if action != "resolved" else "resolved",
                        "action": action,
                        "rule": alert.rule,
                        "ruleName": alert.name,
                        "text": text,
                        "detail": alert.detail,
                        "ts": datetime.now(tz=UTC).isoformat(),
                    }
                )
            except Exception as exc:  # 投递通道自身故障绝不能影响服务
                _log.errorw("alert.deliver_failed", rule=alert.rule, error=str(exc))

    async def run_forever(self) -> None:
        """后台评估循环；任何自身故障都只记日志，绝不影响对话链路。"""
        interval = max(1, self._settings.alert_eval_interval_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底
                _log.errorw("alert.evaluate_failed", error=str(exc))
