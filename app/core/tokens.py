"""token 估算。

优先用 LangChain 的近似计数器（无需下载 tokenizer，离线可用），
不可用时退化为字符数估算（中文约 1.6 字符/token）。
"""

from collections.abc import Sequence
from typing import Any

try:  # langchain-core 较新版本
    from langchain_core.messages import count_tokens_approximately as _lc_counter
except ImportError:  # pragma: no cover - 兼容旧版本路径
    try:
        from langchain_core.messages.utils import (  # type: ignore[no-redef]
            count_tokens_approximately as _lc_counter,
        )
    except ImportError:
        _lc_counter = None  # type: ignore[assignment]

CHARS_PER_TOKEN = 1.6


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    if _lc_counter is not None:
        try:
            return int(_lc_counter([text]))
        except Exception:  # pragma: no cover
            pass
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: Sequence[Any]) -> int:
    if not messages:
        return 0
    if _lc_counter is not None:
        try:
            return int(_lc_counter(list(messages)))
        except Exception:  # pragma: no cover
            pass
    total = 0
    for m in messages:
        content = getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else "")
        total += estimate_text_tokens(str(content))
    return total


def chars_for_token_budget(tokens: int) -> int:
    """把 token 预算换算成字符上限，用于粗粒度裁剪。"""
    return max(1, int(tokens * CHARS_PER_TOKEN))


def truncate_to_token_budget(text: str, tokens: int, *, suffix: str = "……（已截断）") -> str:
    limit = chars_for_token_budget(tokens)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
