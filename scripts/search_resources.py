# -*- coding: utf-8 -*-
"""资源库检索 CLI(2026-10-01):按关键词找对标库里的现成资源。

用法:
    python scripts/search_resources.py 花少           # 关键词检索
    python scripts/search_resources.py --resonance    # 高共振榜(需求被反复验证的金矿)
    python scripts/search_resources.py --profile <pan_url>   # 单资源画像
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _print_res(r: dict) -> None:
    my = ("已转存✓ " + r["my_link"][:52]) if r["my_link"] else "未转存"
    t0 = r["titles"][0][:44] if r["titles"] else "(无标题)"
    print(f"  ×{r['accounts']}号 [{r['pan_type']}] {t0}")
    print(f"    {my} | 最近 {r['last_seen'][:10]} | {r['pan_url'][:56]}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--resonance", action="store_true")
    ap.add_argument("--profile", default="")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--user", type=int, default=1)
    args = ap.parse_args()

    from app.db import get_session_local
    from app.services import resource_library as rl

    db = get_session_local()()
    try:
        if args.profile:
            p = rl.resource_profile(db, args.user, args.profile)
            print("资源画像:", p if p else "(库中无此链)")
            return 0
        s = rl.library_summary(db, args.user, days=args.days)
        print(f"资源库:总链 {s['total_links']} 条 | 多号验证 {s['multi_account']} 条(近 {args.days} 天)\n")
        if args.resonance or not args.query:
            print("== 高共振榜(≥2 号同发) ==")
            for r in rl.resonance_resources(db, args.user, days=args.days, min_accounts=2, limit=15):
                _print_res(r)
        if args.query:
            print(f"\n== 检索「{args.query}」 ==")
            rows = rl.search_resources(db, args.user, args.query, days=args.days, limit=15)
            for r in rows:
                _print_res(r)
            if not rows:
                print("  (无匹配——换个短词试试)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
