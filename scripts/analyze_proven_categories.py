# -*- coding: utf-8 -*-
"""对标号回灌分析:用同行的真实发文数据校准网盘拉新判据(2026-10-01)。

**为什么**:结算反馈只能用自己的发文(样本少、慢);而 142 个对标号发的每一篇
资源文都是"同行在市场里验证过什么赚钱"的活数据——免费、量大、持续。
本脚本统计:各品类(验证品类词表)的文章量、盘链共振度(同盘链被多号同发),
为 PROVEN_CATEGORIES 的名单与权重提供数据校准依据。

用法:
    python scripts/analyze_proven_categories.py [--days 30] [--user 1]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--user", type=int, default=1)
    args = ap.parse_args()

    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import WechatArticle, WechatPanLink
    from app.services.niche_fit import PROVEN_CATEGORIES

    db = get_session_local()()
    try:
        cutoff = datetime.now() - timedelta(days=args.days)
        arts = db.scalars(select(WechatArticle).where(
            WechatArticle.user_id == args.user,
            WechatArticle.created_at >= cutoff)).all()
        print(f"近 {args.days} 天对标文章: {len(arts)} 篇\n")

        # ① 品类分布(按标题/盘链标记)
        by_cat: dict[str, list] = defaultdict(list)
        uncat = []
        for a in arts:
            blob = f"{a.title or ''} {(a.pan_types or '')}".lower()
            hit = None
            for cat, words in PROVEN_CATEGORIES.items():
                if any(w in blob for w in words):
                    hit = cat
                    break
            (by_cat[hit] if hit else uncat).append(a)
        print("=== 品类分布 ===")
        total = len(arts) or 1
        for cat, rows in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            print(f"  {cat:4s} {len(rows):4d} 篇 ({len(rows)/total*100:4.1f}%)")
        print(f"  未分类 {len(uncat):4d} 篇 ({len(uncat)/total*100:4.1f}%)")

        # ② 盘链共振度:同一盘链被多少篇(多少号)同发 = 需求被反复验证的强度
        links = db.execute(select(WechatPanLink.pan_url, WechatPanLink.article_id).where(
            WechatPanLink.user_id == args.user)).all()
        link2arts: dict[str, set] = defaultdict(set)
        for url, aid in links:
            link2arts[url].add(aid)
        multi = {u: s for u, s in link2arts.items() if len(s) >= 2}
        print(f"\n=== 盘链共振(被 ≥2 篇同发的链) ===")
        print(f"  共振链: {len(multi)} 条 / 总链 {len(link2arts)} 条"
              f" ({len(multi)/max(len(link2arts),1)*100:.0f}%)")
        top = sorted(multi.items(), key=lambda kv: -len(kv[1]))[:8]
        art_by_id = {a.id: a for a in arts}
        for url, aids in top:
            titles = [art_by_id[i].title[:24] for i in aids if i in art_by_id][:2]
            print(f"  ×{len(aids)} {url[:44]} | {' / '.join(titles)}")

        # ③ 结论提示
        print("\n=== 校准提示 ===")
        top_cats = sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:3]
        print("  同行发得最多的品类:", ", ".join(f"{c}({len(r)})" for c, r in top_cats))
        if multi:
            print("  共振最强的链指向的品类(需求被反复验证,优先跟):")
            for url, aids in top[:3]:
                for i in aids:
                    a = art_by_id.get(i)
                    if a:
                        print(f"    - {a.title[:40]}")
                        break
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
