"""DeepSeek 客户端工厂。

用 `langchain-openai` 的 `ChatOpenAI` 指向 DeepSeek 的 OpenAI 兼容端点：
- 新模型 `deepseek-flash` 发布即支持（不需要等 langchain-deepseek 跟进）；
- `extra_body` 可透传 `thinking` 等专有参数；
- `stream_usage=True` 对应 `stream_options.include_usage`（流式拿 token 统计必需）。

若将来需要 Anthropic 格式或 Responses API，只改本文件，业务层无感。
"""

import inspect
from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.settings import Settings
from app.llm.profiles import Profile, get_profile

# ChatOpenAI 构造函数参数随版本变化，构造前先按签名过滤，避免版本不兼容直接崩。
_CANDIDATE_KWARGS = (
    "model",
    "api_key",
    "base_url",
    "timeout",
    "max_retries",
    "max_tokens",
    "temperature",
    "extra_body",
    "stream_usage",
)


_CORE_KEYS = ("model", "api_key", "base_url")


def _introspect_supported() -> set[str]:
    """收集 ChatOpenAI 真正接受的参数名。

    两道来源都要看：不同版本的 LangChain 有的走 __init__ 签名，有的只在
    pydantic model_fields 上（且字段名可能是别名，如 model_name / api_key / base_url）。
    """
    params: set[str] = set()

    try:
        params |= set(inspect.signature(ChatOpenAI.__init__).parameters)
    except (TypeError, ValueError):  # pragma: no cover
        pass

    for name, field in (getattr(ChatOpenAI, "model_fields", None) or {}).items():
        params.add(name)
        alias = getattr(field, "alias", None)
        if alias:
            params.add(alias)

    return params


def _filter_supported(kwargs: dict) -> tuple[dict, list[str]]:
    """按实际支持的参数过滤。

    探测失败时**宁可不过滤**（fail open）——把参数原样传下去，
    也好过因自省不准而丢掉 model / api_key 这类必需参数。
    """
    supported_names = _introspect_supported()
    if not supported_names or not (set(_CORE_KEYS) & supported_names):
        return dict(kwargs), []

    supported = {k: v for k, v in kwargs.items() if k in supported_names}
    dropped = sorted(set(kwargs) - set(supported))
    return supported, dropped


@lru_cache(maxsize=64)
def _build_cached(
    model: str,
    profile_name: str,
    temperature: float | None,
    max_tokens: int,
    streaming: bool,
    base_url: str,
    api_key: str,
    timeout: float,
    max_retries: int,
) -> ChatOpenAI:
    profile = get_profile(profile_name)

    kwargs: dict = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "timeout": timeout,
        "max_retries": max_retries,
        "max_tokens": max_tokens,
        "extra_body": {"thinking": profile.thinking_param},
        "stream_usage": streaming,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    supported, dropped = _filter_supported(kwargs)
    if dropped:  # pragma: no cover
        from app.core.logger import get_logger

        get_logger().warning("llm.unsupported_kwargs", dropped=dropped, model=model)

    return ChatOpenAI(**supported)


def build_llm(
    settings: Settings,
    profile: Profile,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = False,
) -> ChatOpenAI:
    """按档位构造 LLM 客户端。

    调用方可覆盖 model / temperature / maxTokens（对应请求的 options 字段），
    但**不能**越过档位去改 thinking 行为——这是避免误用的刻意设计。
    """
    resolved_temperature = profile.temperature if temperature is None else temperature
    if profile.thinking and temperature is not None:
        # 思考模式下 temperature 无效，明确丢弃，避免使用者误以为生效
        from app.core.logger import get_logger

        get_logger().info("llm.temperature_ignored_in_thinking_mode", profile=profile.name)
        resolved_temperature = None

    return _build_cached(
        model or settings.llm_model,
        profile.name,
        resolved_temperature,
        max_tokens or profile.max_tokens,
        streaming,
        settings.deepseek_base_url,
        settings.deepseek_api_key,
        settings.llm_timeout_seconds,
        settings.llm_max_retries,
    )
