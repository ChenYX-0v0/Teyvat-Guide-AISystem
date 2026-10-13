"""角色机制卡评测器（阶段②，设计见 teyvat-docs/ai/07 TODO-20）。

与 run_eval.py 的分工：
- run_eval.py  = 纯模型基线（**不带**任何资料块），用来量"提示词 + 模型本身"的水平；
- 本评测器     = **带机制卡**的同口径复跑，用来量"资料真的注进去之后，模型答得对不对" ✓。

因此它必须拿到"线上会注入的那段文本"，而那段文本由主服务组装（Go 侧 ✓）——
所以先导出、再评测，两步（见下）；评测器只负责"照原样送进去 + 打分" ✓。

用法（在 ai-service/ 目录下）：
    # ① 导出注入文本（在 server/ 目录；与线上**逐字相同**的代码路径 ✓）
    go run ./cmd/mechanicsimport \
        -questions ../AI派蒙服务器/ai-service/tests/eval/mechanics_cases.json \
        -out ../AI派蒙服务器/ai-service/tests/eval/mechanics_contexts.json
    # ② 评测
    .\\.venv\\Scripts\\python.exe tests\\eval\\run_mechanics_eval.py
    .\\.venv\\Scripts\\python.exe tests\\eval\\run_mechanics_eval.py --tag 规则 --ids 24,25

打分口径（写死在 `_scoring` 里，别改 ✗）：
    expect：关键词**组**数组 —— 每组是若干同义写法，命中任一即该组通过 ✓；
    forbid：出现任意一条即**本题判错** ✗（含 cases 文件里的 `_default_forbid` ✓）。

⚠️ **已知局限（务必按它读结果 ✗）**：这是**关键词近似**判分，不是语义判分 ⇒
   模型把答案说对了但措辞不在同义表里，就会被判"缺项" ✗。实测踩过三次：
   「不太够」vs「不足/不够」✓、「不太搭」vs「不搭」✓、「两成」vs「20%」✓
   —— 三次查原文后**都是语义正确** ✓，属于评分口径问题 ✗ 而非缺陷 ✓。
   ⇒ 看到失败**必须先看原文再定性** ✓（`--show` 或结果 JSON 里的 reply ✓），
     确认是措辞差异就把同义写法补进 `expect` ✓；确认是内容错才当缺陷处理 ✓。
   另外模型输出有波动 ⇒ 单条"复跑通过"不能当整轮结论 ✗，看通过率而不是单条 ✓。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import httpx

# Windows 后台进程默认 stdout 是 GBK ⇒ 打印「✓ / ✗」会 UnicodeEncodeError 直接崩 ✗
# （2026-10-13 实测：没带 PYTHONIOENCODING 时整轮评测在第 1 条就挂 ✓）⇒ 自己兜住 ✓
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老解释器或已被重定向时忽略即可
    pass

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # ai-service/
CASES = HERE / "mechanics_cases.json"
CONTEXTS = HERE / "mechanics_contexts.json"
BASE_URL = "http://127.0.0.1:8090"


def load_key() -> str:
    """从 .env 读 AI_SERVICE_KEY。刻意不打印密钥。"""
    env = ROOT / ".env"
    if not env.exists():
        sys.exit("找不到 .env，无法取 AI_SERVICE_KEY")
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("AI_SERVICE_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(".env 里没有 AI_SERVICE_KEY")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="逗号分隔的用例 id")
    ap.add_argument("--tag", default="", help="只跑某个 tag")
    ap.add_argument("--contexts", default=str(CONTEXTS), help="① 导出的注入文本 JSON")
    ap.add_argument("--base", default=BASE_URL, help="AI 服务地址（默认 8090）")
    ap.add_argument("--out", default="", help="输出文件名（默认 mechanics_eval_<时间>.json）")
    ap.add_argument("--show", action="store_true", help="打印每条回答正文（调试用 ✓）")
    args = ap.parse_args()

    spec = json.loads(CASES.read_text(encoding="utf-8"))
    items = spec["questions"]
    default_forbid: list[str] = spec.get("_default_forbid", [])

    ctx_path = pathlib.Path(args.contexts)
    if not ctx_path.exists():
        sys.exit(
            f"找不到注入文本 {ctx_path}\n"
            "  先在 server/ 目录跑：go run ./cmd/mechanicsimport "
            "-questions ../AI派蒙服务器/ai-service/tests/eval/mechanics_cases.json "
            "-out ../AI派蒙服务器/ai-service/tests/eval/mechanics_contexts.json"
        )
    rows = json.loads(ctx_path.read_text(encoding="utf-8"))["items"]
    ctx = {row["id"]: row.get("summary", "") for row in rows}

    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip()}
        items = [i for i in items if i["id"] in want]
    if args.tag:
        items = [i for i in items if i["tag"] == args.tag]
    if not items:
        sys.exit("没有选中的用例")

    key = load_key()
    client = httpx.Client(timeout=180.0, headers={"X-AI-Key": key})
    results: list[dict] = []

    print(f"  {'id':>2}  {'tag':<14} {'字数':>4}  {'命中':>5}  判定  问题")
    print("  " + "-" * 78)
    for it in items:
        summary = ctx.get(it["id"], "")
        payload: dict = {"prompt": it["q"]}
        if summary:
            payload["context"] = {"summary": summary}
        t0 = time.time()
        try:
            resp = client.post(f"{args.base}/v1/complete", json=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            reply = data.get("reply", "")
            tokens = data.get("tokens")
        except Exception as exc:  # noqa: BLE001 - 评测器要记下失败而不是崩掉
            reply, tokens = "", {"error": f"{type(exc).__name__}: {exc}"}
        cost = round(time.time() - t0, 1)

        groups = it.get("expect", [])
        hits = [any(alt in reply for alt in group) for group in groups]
        forbids = default_forbid + it.get("forbid", [])
        violations = [w for w in forbids if w in reply]
        ok = all(hits) and not violations
        hit_txt = f"{sum(hits)}/{len(groups)}" if groups else "-"
        verdict = "通过 ✓" if ok else ("越界 ✗" if violations else "缺项 ✗")

        head = f"  {it['id']:>2}  {it['tag']:<14} {len(reply):>4}  {hit_txt:>5}  {verdict}"
        print(f"{head}  {it['q'][:24]}")
        if violations:
            print(f"      违规词：{'、'.join(violations)}")
        if not all(hits):
            missing = [g for g, h in zip(groups, hits, strict=False) if not h]
            print(f"      缺：{'；'.join('/'.join(g) for g in missing)}")
        if args.show:
            print("      " + reply.replace("\n", "\n      "))
        results.append(
            {
                **it,
                "reply": reply,
                "chars": len(reply),
                "tokens": tokens,
                "wallSeconds": cost,
                "summaryChars": len(summary),
                "groupHits": hits,
                "violations": violations,
                "passed": ok,
            }
        )

    passed = sum(1 for r in results if r["passed"])
    out = HERE / (args.out or f"mechanics_eval_{time.strftime('%Y%m%d_%H%M')}.json")
    out.write_text(
        json.dumps(
            {
                "_meta": {
                    "mode": "AI 服务直连 /v1/complete + context.summary（由 Go 侧同一条链导出 ✓）",
                    "contexts": str(ctx_path.name),
                    "ranAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(results),
                    "passed": passed,
                    "rate": round(passed / len(results) * 100, 1),
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    avg = sum(r["chars"] for r in results) / len(results)
    rate = passed / len(results) * 100
    print(f"\n  通过 {passed}/{len(results)}（{rate:.0f}%），平均 {avg:.0f} 字 → {out}")
    if passed < len(results):
        failed = "、".join(f"[{r['id']}]{r['q'][:16]}" for r in results if not r["passed"])
        print("  失败用例：" + failed)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
