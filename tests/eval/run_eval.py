"""解释类回答的基线 / 回归评测器（纯只读 AI 服务，不写对话记录）。

设计见 teyvat-docs/ai/07 TODO-17。

口径（关键，别改错了）：
- 打的是 **AI 服务直连** `POST /v1/complete`，**不走主服务** ⇒ 不携带【游戏资料】块。
  为什么这样设计：把变量钉死，改提示词（阶段①）与加攻略卡（阶段②）后能用**同一口径**复跑对比。
- 纯模型基线对"配队 / 毕业线 / 天赋顺序"三类有代表性（库里本来就没有这三类资料）；
  但"帮我看看我的XX"这类**带玩家档案**的问题不代表真实链路（真实链路会升 analysis 档 + 注入面板），
  那部分基线以**线上历史问答**为准（见 ai/07 TODO-17 的基线表）。

用法：
    .\\.venv\\Scripts\\python.exe tests\\eval\\run_eval.py                 # 全量 20 条
    .\\.venv\\Scripts\\python.exe tests\\eval\\run_eval.py --ids 9,11,13   # 只跑指定 id
    .\\.venv\\Scripts\\python.exe tests\\eval\\run_eval.py --tag 配队      # 只跑某类
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[2]
QUESTIONS = pathlib.Path(__file__).with_name("questions.json")
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
    ap.add_argument("--ids", default="", help="逗号分隔的问题 id")
    ap.add_argument("--tag", default="", help="只跑某个 tag")
    ap.add_argument("--profile", default="", help="覆盖推理档位（默认交给服务端路由）")
    ap.add_argument("--out", default="", help="输出文件名（默认 baseline_<日期>.json）")
    ap.add_argument("--base", default=BASE_URL, help="AI 服务地址（默认 8090；对比 v11 时可指向隔离实例）")
    args = ap.parse_args()

    spec = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    items = spec["questions"]
    if args.ids:
        want = {int(x) for x in args.ids.split(",") if x.strip()}
        items = [i for i in items if i["id"] in want]
    if args.tag:
        items = [i for i in items if i["tag"] == args.tag]
    if not items:
        sys.exit("没有选中的问题")

    key = load_key()
    client = httpx.Client(timeout=180.0, headers={"X-AI-Key": key})
    results: list[dict] = []

    for it in items:
        payload: dict = {"prompt": it["q"]}
        if args.profile:
            payload["options"] = {"profile": args.profile}
        t0 = time.time()
        try:
            resp = client.post(f"{args.base}/v1/complete", json=payload)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            reply = data.get("reply", "")
            meta = {
                "tokens": data.get("tokens"),
                "model": data.get("model"),
                "elapsedMs": data.get("elapsedMs"),
                "finishReason": data.get("finishReason"),
            }
        except Exception as exc:  # noqa: BLE001 - 评测器要记下失败而不是崩掉
            reply, meta = "", {"error": f"{type(exc).__name__}: {exc}"}
        cost = round(time.time() - t0, 1)

        results.append({**it, "reply": reply, "chars": len(reply), **meta, "wallSeconds": cost})
        print(f"[{it['id']:>2}] {it['tag']:<6} {cost:>5.1f}s  {meta.get('tokens', '-')!s:>5} tok  "
              f"{len(reply):>4} 字  {it['q'][:34]}")

    out = QUESTIONS.parent / (args.out or f"baseline_{time.strftime('%Y%m%d_%H%M')}.json")
    out.write_text(
        json.dumps(
            {
                "_meta": {
                    "mode": "AI 服务直连 /v1/complete（无【游戏资料】块，无玩家档案）",
                    "profileOverride": args.profile or "服务端路由默认",
                    "ranAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(results),
                    "note": (
                        "纯模型基线：对配队/毕业线/天赋顺序有代表性；"
                        "带档案的养成分析以线上历史问答为准"
                    ),
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    avg = sum(r["chars"] for r in results) / len(results)
    print(f"\n  完成 {len(results)} 条，平均 {avg:.0f} 字 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
