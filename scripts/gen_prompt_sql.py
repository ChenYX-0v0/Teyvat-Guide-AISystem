"""从 prompts/paimon.json 生成入库迁移 SQL（替代已不在仓库里的 deploy/out/gen-prompt-*.js）。

为什么要它：提示词有**两个真源** —— `prompts/paimon.json`（file 兜底）与 `db/migrations/*.sql`
（http 模式实际生效源）。手抄两边必然漂移 ✗，所以让 SQL 从 JSON 生成 ✓。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\gen_prompt_sql.py \\
        --version 11 --out db/migrations/008_ai_prompts_v11.sql

⚠️ 生成的 SQL **不会自动执行** —— 入库属于生产变更，要人工确认后再跑（见 ai/07 TODO-17）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENES = ("paimon_base", "paimon_rules", "paimon_fewshot")


def sql_escape(text: str) -> str:
    """MySQL 单引号字符串转义：反斜杠与单引号都要处理（反斜杠在 MySQL 里是转义符）。"""
    return text.replace("\\", "\\\\").replace("'", "''")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="本次升级的版本号（写进文件头注释）")
    ap.add_argument("--out", required=True, help="输出 SQL 路径（相对 ai-service 根）")
    ap.add_argument("--title", default="", help="文件头一句话摘要")
    args = ap.parse_args()

    spec = json.loads((ROOT / "prompts" / "paimon.json").read_text(encoding="utf-8"))
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[str] = []
    meta: list[str] = []
    for scene in SCENES:
        node = spec.get(scene)
        if not node:
            sys.exit(f"prompts/paimon.json 里没有 {scene}")
        content, version = node["content"], node["version"]
        rows.append(f"('{scene}', '{sql_escape(content)}', {version}, 1, '')")
        # ⚠️ 每一行都必须带 `--`：整份文件是直接丢给 MySQL 执行的，
        # 裸的说明文字会让它在第 8 行就报语法错 ✗（2026-10-12 踩过 ✓）。
        meta.append(f"--    {scene:<16} version={version}  {len(content)} 字符")

    header = [
        f"-- {out_path.name}",
        "-- 由 scripts/gen_prompt_sql.py 从 prompts/paimon.json 生成（勿手改，改 JSON 后重跑 ✓）。",
        f"-- 版本：v{args.version}" + (f"　{args.title}" if args.title else ""),
        "-- 生效：入库后 ≤60s 热更新（http 模式取「启用且版本最高」✓）。",
        "-- 回滚：停用本次新版本行，旧版本自动恢复 ✓。",
        "-- 幂等：同名同版本行会被本文件覆盖（见末尾 ON DUPLICATE KEY UPDATE）——",
        "--   三个场景里只有被改动的那个版本号会变，其余两个仍在文件里",
        "--   （保持「一份 JSON 一个源」✓），所以重跑本文件是安全的 ✓",
        "--   （2026-10-13 加：此前裸 INSERT 会撞 uk_scene_version ✗）。",
        "--",
        "-- 本次生成的行：",
        *meta,
    ]

    cols = "`scene_key`, `content`, `version`, `enabled`, `remark`"
    sql = "\n".join(header) + f"\n\nINSERT INTO `ai_prompts` ({cols})\nVALUES\n"
    sql += ",\n".join(rows)
    # ⚠️ 必须幂等：paimon.json 里始终有 3 个场景，而一次升级通常只改 1 个 ⇒
    #   另外 2 行是**已存在的旧版本**（同 scene_key + version 撞唯一键 ✗）。
    #   加 ON DUPLICATE KEY UPDATE 后重跑安全 ✓（内容一致 ⇒ 无副作用 ✓）。
    sql += (
        "\nON DUPLICATE KEY UPDATE\n"
        "  `content` = VALUES(`content`),\n"
        "  `enabled` = VALUES(`enabled`),\n"
        "  `remark`  = VALUES(`remark`);\n"
    )
    # ⚠️ newline="\n" 必须写死：默认情况下 Windows 会把 \n 翻译成 \r\n，
    # 于是入库的提示词每条换行都多一个 CR（实测 base 325 → 335 字符 ✗，2026-10-12 踩过 ✓）。
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(sql)

    print(f"  已生成 {out_path}")
    for line in meta:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
