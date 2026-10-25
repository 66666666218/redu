"""热搜 → 吃瓜候选(测试版,**只筛选,不写库、不排程**)。

    python scripts/gossip_topics_test.py            # 三平台热搜 → 吃瓜候选
    python scripts/gossip_topics_test.py --json     # 机器可读(供下一步喂给资料生成)
    python scripts/gossip_topics_test.py --min 6    # 只看强信号(命中 2 个瓜词以上)

输出里带 `命中` 与 `平台`:前者让人一眼复核"为什么选它",后者说明它是不是**多平台同时上榜**
(那通常才是真在发酵的瓜)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="三平台热搜 → 吃瓜候选")
    ap.add_argument("--min", type=int, default=3, help="最低分(默认 3 = 命中 1 个强信号)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-llm", action="store_true", help="只用关键词(不调模型判定)")
    args = ap.parse_args()

    from app.services import gossip_topics as gt

    items, fails = gt.fetch_all()
    if args.no_llm:
        picks, note = gt.pick_gossip(items, min_score=args.min, limit=args.limit), ""
    else:
        picks, note = gt.judge_all(items, limit=args.limit)

    if args.json:
        print(json.dumps({"total": len(items), "failed": fails, "picks": picks,
                          "note": note}, ensure_ascii=False, indent=2))
        return 0

    print(f"热搜共 {len(items)} 条（微博/百度/抖音）", end="")
    if fails:
        print(" · ⚠️ 抓取失败:" + ";".join(f"{k}({v[:40]})" for k, v in fails.items()))
    else:
        print()
    if note:
        print(note)
    print(f"吃瓜候选 {len(picks)} 条:\n")
    for i, g in enumerate(picks, 1):
        plats = "·".join(g["platforms"])
        print(f"{i:2d}. [{plats}] {g['title']}")
        bits = [str(g.get("reason") or "")]
        if g.get("score"):
            bits.append(f"关键词 {g['score']}({'、'.join(g['matched'])})")
        print("    " + " · ".join(b for b in bits if b))
    if not picks:
        print("（本轮没有达标的候选 —— 说明现在榜上确实没什么瓜；别把它当成筛选坏了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
