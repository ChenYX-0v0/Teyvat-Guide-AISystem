"""上下文渲染：终局概要（深渊 / 剧诗 / 危战）必须真的进 prompt。

背景（2026-09-27 线上实测）：`render_context` 过去只渲染 goal / summary /
player.nickname|level / player.characters —— `player.abyss`「theater」「stygian」
被静默丢弃，导致"终局概要已接进 AI 上下文"实际未生效（模型看不到，只能编或拒答）。
"""

from app.chains.context_render import render_context
from app.schemas.context import RequestContext

BUDGET = 4000


def _render(player: dict) -> str:
    ctx = RequestContext.model_validate({"player": player})
    return render_context(ctx, BUDGET)


def test_endgame_fields_are_rendered():
    text = _render(
        {
            "uid": "197794747",
            "nickname": "旅行者",
            "level": 60,
            "abyss": {"floor": 12, "room": 3, "stars": 36},
            "theater": {"act": 10, "mode": 119, "stars": 10},
            "stygian": {"index": 6, "seconds": 324, "id": 5269011},
            "characters": [{"name": "温迪", "level": 90}],
        }
    )
    assert "深渊：12-3，36 星" in text
    assert "幻想真境剧诗：第 10 幕，10 星" in text
    assert "幽境危战：第 6 期，用时 324 秒" in text
    # 终局概要要在角色明细之前（裁剪时先保它）
    assert text.index("深渊：") < text.index("温迪")


def test_missing_endgame_string_is_kept_verbatim():
    """主服务用一句话表达"没数据"，渲染必须原样保留 —— 否则模型看不到"未提供"，
    会退化成凭记忆编战绩。"""
    text = _render(
        {
            "nickname": "旅行者",
            "abyss": "档案中未提供（玩家可能未公开战绩）",
            "characters": [{"name": "温迪"}],
        }
    )
    assert "深渊：档案中未提供（玩家可能未公开战绩）" in text


def test_unknown_player_field_falls_back_to_text():
    """保持"主服务加字段不必改 AI 服务"的约定。"""
    text = _render({"nickname": "旅行者", "newThing": {"a": 1, "b": 2}})
    assert "newThing：a 1，b 2" in text


def test_characters_are_untouched():
    text = _render({"nickname": "旅行者", "characters": [{"name": "胡桃", "level": 90}]})
    assert "胡桃 Lv.90" in text
