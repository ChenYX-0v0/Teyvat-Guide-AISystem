"""把主服务推送的结构化上下文渲染成模型可读文本。

设计要点：
- 输出是**自然语言摘要**而非 JSON dump —— 同样信息量下模型更好用，也更省 token；
- 字段渲染保持宽松（主服务加字段不需要 AI 服务发版），未知字段按 key: value 兜底；
- 整体受 token 预算约束，超限时先砍角色明细、保留概览。
"""

from collections.abc import Iterable
from typing import Any

from app.core.tokens import estimate_text_tokens, truncate_to_token_budget
from app.schemas.context import RequestContext

_TALENT_LABELS = {
    "normal": "普攻",
    "skill": "战技",
    "burst": "爆发",
    "normalAttack": "普攻",
    "elementalSkill": "战技",
    "elementalBurst": "爆发",
}


def _fmt_talents(talents: dict[str, Any]) -> str:
    if not talents:
        return ""
    items: Iterable[tuple[str, Any]] = talents.items()
    parts = [f"{_TALENT_LABELS.get(k, k)}{v}" for k, v in items if v is not None]
    return "/".join(parts)


def _fmt_weapon(weapon: Any) -> str:
    if not isinstance(weapon, dict):
        return ""
    name = weapon.get("name")
    if not name:
        return ""
    bits = [str(name)]
    if weapon.get("refine") is not None:
        bits.append(f"精{weapon['refine']}")
    if weapon.get("level") is not None:
        bits.append(f"Lv.{weapon['level']}")
    return " ".join(bits)


def _fmt_artifacts(artifacts: Any) -> str:
    if not isinstance(artifacts, list) or not artifacts:
        return ""
    chunks: list[str] = []
    for art in artifacts:
        if not isinstance(art, dict):
            continue
        name = art.get("set") or art.get("name") or "未知套装"
        count = art.get("count")
        chunk = f"{name}×{count}" if count else str(name)
        main_stats = art.get("mainStats") or {}
        if isinstance(main_stats, dict) and main_stats:
            stat_text = "，".join(f"{k} {v}" for k, v in main_stats.items())
            chunk += f"（{stat_text}）"
        chunks.append(chunk)
    return "；".join(chunks)


def _render_character(char: Any) -> str:
    if not isinstance(char, dict):
        return ""
    name = char.get("name")
    if not name:
        return ""
    bits: list[str] = [str(name)]
    if char.get("level") is not None:
        bits.append(f"Lv.{char['level']}")
    if char.get("constellation") is not None:
        bits.append(f"命座{char['constellation']}")
    talents = _fmt_talents(char.get("talents") or {})
    if talents:
        bits.append(f"天赋{talents}")
    line = " ".join(bits)

    extra: list[str] = []
    weapon = _fmt_weapon(char.get("weapon"))
    if weapon:
        extra.append(f"武器：{weapon}")
    artifacts = _fmt_artifacts(char.get("artifacts"))
    if artifacts:
        extra.append(f"圣遗物：{artifacts}")
    if extra:
        line += "\n    · " + "\n    · ".join(extra)

    known = {"name", "level", "constellation", "talents", "weapon", "artifacts"}
    for key, value in char.items():
        if key in known or value in (None, "", [], {}):
            continue
        line += f"\n    · {key}: {value}"
    return line


def render_context(context: RequestContext, budget_tokens: int) -> str:
    """渲染为「【玩家档案】…」文本块。

    超预算时的裁剪策略是**整条丢弃末尾角色**，而不是字符级截断：
    给模型半个角色条目（「胡桃 Lv.9」）会让它误读练度，宁可少给几个角色，
    也不要给残缺的 —— 残缺数据的危害大于数据缺失。
    """
    if context is None or context.is_empty():
        return ""

    prefix_blocks: list[str] = []
    if context.goal:
        prefix_blocks.append(f"【本次目标】{context.goal}")
    if context.summary:
        prefix_blocks.append(f"【玩家情况摘要】{context.summary}")

    player = context.player
    header_line = ""
    char_entries: list[str] = []

    if player is not None:
        header: list[str] = []
        if player.nickname:
            header.append(f"昵称：{player.nickname}")
        if player.level is not None:
            header.append(f"冒险等阶：{player.level}")
        header_line = "【玩家档案】" + ("（" + "，".join(header) + "）" if header else "")

        for char in player.characters:
            rendered = _render_character(char.model_dump(exclude_none=True))
            if rendered:
                char_entries.append(f"  - {rendered}")

    if not prefix_blocks and not header_line:
        return ""

    def compose(kept: int) -> str:
        blocks = list(prefix_blocks)
        if header_line:
            lines = [header_line]
            if char_entries:
                lines.append("角色：")
                lines.extend(char_entries[:kept])
                if kept < len(char_entries):
                    lines.append(f"  （其余 {len(char_entries) - kept} 个角色已省略）")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    kept = len(char_entries)
    text = compose(kept)
    while kept > 0 and estimate_text_tokens(text) > budget_tokens:
        kept -= 1
        text = compose(kept)

    # 兜底：连"档案头 + 角色数"都放不下（预算极小或角色极多）时退化为字符级截断
    if estimate_text_tokens(text) > budget_tokens:
        text = truncate_to_token_budget(text, budget_tokens)
    return text
