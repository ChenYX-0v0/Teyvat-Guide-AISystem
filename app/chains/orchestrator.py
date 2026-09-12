"""编排核心：组装 → 裁剪 → 路由 → 调用 → 计费。

对应 /v1/complete 的处理流水线；本文件是唯一"知道所有环节"的地方。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.chains.persona import build_system_prompt, load_stable_rows
from app.chains.routing import decide_profile
from app.core.errors import (
    AIServiceError,
    InvalidRequestError,
    classify_upstream_error,
)
from app.core.logger import get_logger
from app.core.settings import Settings
from app.llm.deepseek import build_llm
from app.llm.profiles import Profile
from app.schemas.chat import ChatMessage, CompleteData, CompleteRequest, TokenUsage
from app.store.prompt_store import PromptStoreService

try:
    from langchain_core.messages import count_tokens_approximately as _approx_counter
except ImportError:  # pragma: no cover
    try:
        from langchain_core.messages.utils import (  # type: ignore[no-redef]
            count_tokens_approximately as _approx_counter,
        )
    except ImportError:
        _approx_counter = None  # type: ignore[assignment]

try:
    from langchain_core.messages import trim_messages as _trim_messages
except ImportError:  # pragma: no cover
    _trim_messages = None  # type: ignore[assignment]

_log = get_logger()

# 当 LangChain 不可用时，历史上限的保守兜底
_FALLBACK_HISTORY_MESSAGES = 10


# ---------------------------------------------------------------- 工具函数


def _text_of(message: Any) -> str:
    """规范化消息内容（multimodal 时 content 可能是部件数组）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content or "")


def _extract_usage(message: Any) -> TokenUsage:
    """从 LangChain 消息里提取 token 用量，兼容原始 usage 与 usage_metadata 两种形态。"""
    raw = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    meta = getattr(message, "usage_metadata", None) or {}

    input_tokens = raw.get("prompt_tokens") or meta.get("input_tokens") or 0
    output_tokens = raw.get("completion_tokens") or meta.get("output_tokens") or 0
    total = raw.get("total_tokens") or meta.get("total_tokens") or (input_tokens + output_tokens)

    cached = (
        raw.get("prompt_cache_hit_tokens")
        or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (meta.get("input_token_details") or {}).get("cache_read")
        or 0
    )
    reasoning = (
        (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
        or (meta.get("output_token_details") or {}).get("reasoning")
        or 0
    )

    return TokenUsage(
        input=int(input_tokens or 0),
        output=int(output_tokens or 0),
        total=int(total or 0),
        cached=int(cached or 0),
        reasoning=int(reasoning or 0),
    )


def _finish_reason(message: Any) -> str:
    metadata = getattr(message, "response_metadata", None) or {}
    return str(metadata.get("finish_reason") or "stop")


def _to_lc_messages(history: list[ChatMessage]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for item in history:
        if item.role == "user":
            out.append(HumanMessage(content=item.content))
        else:
            out.append(AIMessage(content=item.content))
    return out


# ---------------------------------------------------------------- 编排器


class Orchestrator:
    def __init__(self, settings: Settings, prompt_store: PromptStoreService) -> None:
        self._settings = settings
        self._prompt_store = prompt_store
        # 限制并发上游调用：防止同步链路把连接/配额占满（禁止项）
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)

    # ---------------- 校验 ----------------

    def _validate(self, req: CompleteRequest) -> None:
        s = self._settings
        if len(req.prompt) > s.max_prompt_chars:
            raise InvalidRequestError(f"prompt 超长（>{s.max_prompt_chars} 字符）")
        if len(req.history) > s.max_history_messages:
            raise InvalidRequestError(f"history 条数过多（>{s.max_history_messages}）")
        if req.system and len(req.system) > 20_000:
            raise InvalidRequestError("system 超长")
        total_chars = sum(len(m.content) for m in req.history)
        if total_chars > 200_000:
            raise InvalidRequestError("history 内容总长超限")

    # ---------------- 组装 ----------------

    async def _build_messages(
        self, req: CompleteRequest
    ) -> tuple[list[BaseMessage], str]:
        rows = await load_stable_rows(self._prompt_store)
        system_text = build_system_prompt(
            self._settings,
            rows,
            request_system=req.system,
            context=req.context,
        )

        history = _to_lc_messages(req.history)
        trimmed = self._trim_history(history, self._settings.history_budget_tokens)
        if len(trimmed) < len(history):
            _log.info(
                "context.history_trimmed",
                before=len(history),
                after=len(trimmed),
                budget=self._settings.history_budget_tokens,
            )

        messages: list[BaseMessage] = [SystemMessage(content=system_text)]
        messages.extend(trimmed)
        messages.append(HumanMessage(content=req.prompt))
        return messages, system_text

    def _trim_history(self, messages: list[BaseMessage], budget_tokens: int) -> list[BaseMessage]:
        if not messages:
            return []
        if budget_tokens <= 0:
            return []
        if _trim_messages is None or _approx_counter is None:  # pragma: no cover
            return messages[-_FALLBACK_HISTORY_MESSAGES:]
        try:
            return list(
                _trim_messages(
                    messages,
                    max_tokens=budget_tokens,
                    token_counter=_approx_counter,
                    strategy="last",
                    include_system=False,
                    allow_partial=False,
                    start_on="human",
                )
            )
        except Exception as exc:  # pragma: no cover
            _log.warning("context.trim_failed", error=str(exc))
            return messages[-_FALLBACK_HISTORY_MESSAGES:]

    def _build_llm(self, profile: Profile, options: Any, *, streaming: bool) -> Any:
        """构造客户端也纳入错误边界——构造失败同样是上游问题，不能逃逸成 500。"""
        try:
            return build_llm(
                self._settings,
                profile,
                model=getattr(options, "model", None),
                temperature=getattr(options, "temperature", None),
                max_tokens=getattr(options, "maxTokens", None),
                streaming=streaming,
            )
        except AIServiceError:
            raise
        except Exception as exc:
            error = classify_upstream_error(exc)
            _log.errorw(
                "llm.build_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                mapped_code=error.code,
            )
            raise error from exc

    # ---------------- 非流式 ----------------

    async def complete(self, req: CompleteRequest) -> CompleteData:
        self._validate(req)
        profile = decide_profile(req, self._settings.llm_default_profile)
        options = req.options

        llm = self._build_llm(profile, options, streaming=False)
        messages, system_text = await self._build_messages(req)

        started = time.perf_counter()
        async with self._sem:
            try:
                message = await llm.ainvoke(messages)
            except AIServiceError:
                raise
            except Exception as exc:
                error = classify_upstream_error(exc)
                _log.errorw(
                    "llm.invoke_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    profile=profile.name,
                    mapped_code=error.code,
                )
                raise error from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        usage = _extract_usage(message)
        reply = _text_of(message)
        # 思考过程只记长度，不落库、不回传（要求）
        reasoning_text = (getattr(message, "additional_kwargs", None) or {}).get(
            "reasoning_content"
        )

        _log.info(
            "llm.completed",
            profile=profile.name,
            model=llm.model_name,
            thinking=profile.thinking,
            tokens_in=usage.input,
            tokens_out=usage.output,
            tokens_cached=usage.cached,
            tokens_reasoning=usage.reasoning,
            system_tokens=len(system_text),
            reasoning_chars=len(reasoning_text or ""),
            elapsed_ms=elapsed_ms,
        )

        return CompleteData(
            reply=reply,
            model=llm.model_name,
            tokens=usage,
            finishReason=_finish_reason(message),
            elapsedMs=elapsed_ms,
            profile=profile.name,
            thinking=profile.thinking,
        )

    # ---------------- 流式（P3 协议先行实现） ----------------

    async def stream(self, req: CompleteRequest) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """产出 (event, payload)，由 API 层转成 SSE。

        event 取值固定为：delta / done / error。
        """
        self._validate(req)
        profile = decide_profile(req, self._settings.llm_default_profile)
        options = req.options

        try:
            llm = self._build_llm(profile, options, streaming=True)
        except AIServiceError as exc:
            yield "error", {"message": exc.message}
            return
        messages, _ = await self._build_messages(req)

        started = time.perf_counter()
        usage = TokenUsage()
        finish_reason = "stop"

        try:
            async with self._sem:
                async for chunk in llm.astream(messages):
                    text = _text_of(chunk)
                    if text:
                        yield "delta", {"text": text}
                    chunk_usage = _extract_usage(chunk)
                    if chunk_usage.total:
                        usage = chunk_usage
                    reason = _finish_reason(chunk)
                    if reason and reason != "stop":
                        finish_reason = reason
        except AIServiceError as exc:
            yield "error", {"message": exc.message}
            return
        except Exception as exc:
            error = classify_upstream_error(exc)
            _log.errorw("llm.stream_failed", error=str(exc), profile=profile.name)
            yield "error", {"message": error.message}
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _log.info(
            "llm.stream_completed",
            profile=profile.name,
            tokens_total=usage.total,
            tokens_cached=usage.cached,
            elapsed_ms=elapsed_ms,
        )
        yield "done", {
            "tokens": usage.model_dump(),
            "finishReason": finish_reason,
            "model": llm.model_name,
            # profile 为新增可选字段（非破坏性）：主服务据此落库观测档位命中情况
            "profile": profile.name,
            "elapsedMs": elapsed_ms,
        }


def profile_debug_info(profile: Profile) -> dict[str, Any]:  # pragma: no cover
    return {
        "name": profile.name,
        "thinking": profile.thinking,
        "reasoning_effort": profile.reasoning_effort,
        "max_tokens": profile.max_tokens,
        "note": profile.note,
    }
