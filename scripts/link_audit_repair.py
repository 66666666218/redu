"""按**链接体检**的结论修数据:把"错链/空链"从库里清掉,让它们回到重搬队列。

用户口径(2026-10-16):错链「**清空错链、让它重搬**」;空链「**标记 + 重搬**」。

## 为什么必须"清掉"而不是只报告
这些链会走**三盘互通(`reuse_if_have`)**被**复用**给别的资源 ——
实测 `pan.quark.cn/s/d1d3c3385b95`(分享内是「中医古籍」)被 **68 个不同标题**共用,
而它**当 `our_url` 出现 69 次、当 `origin_url` 出现 0 次** ⇒ 不是从帖子里发现的,
是**转存/复用环节**写进去的。留着它,下一条匹配到同一资源的行还会拿到它。

## 清掉之后**会被重搬**(每张表各自的队列,已核过)
- `discovered_pan_links` → `status='pending'` ⇒ 进 `pan_discovery` 的**积压队列**(有额度)
- `wechat_articles` → `my_pan_urls=''` ⇒ 进 `_enrich` 的**补链队列**
  (它专门挑 `my_pan_urls` 为空且 `pan_urls` 有值的行,每轮 `pan_transfer_backfill_limit` 条)
- `douyin_leads` → `our_url=''` + `moved_at`/`kouling_tried_at` 清空 ⇒ **回到待办**
  (该表的设计就是"`our_url` 空 = 待办")

⚠️ **有些会搬不回来,而且会如实失败**:像 `5bea6da448fa` 那种**上游把分享清空了**的,
`origin_url` 就是那条空链本身 ⇒ 重搬会回「分享里没有文件」并落 `failed`。
那是**如实记录**,不是静默 —— 与"清空"要一起理解:清空解决的是"**别再复用错链**",
不是"保证能搬回来"。

用法:
    python scripts/link_audit_repair.py            # 只报告(默认)
    python scripts/link_audit_repair.py --apply    # 真改
    python scripts/link_audit_repair.py --links x,y # 自己指定要清的链接(默认用内置那 5 条)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

#: 2026-10-16 首轮体检定性的坏链(`短 id` → 原因)。**换一批时要重新体检确认**。
BAD: dict[str, str] = {
    "d1d3c3385b95": "原链错配(分享内是「中医古籍」,与库里 68 个不同标题都对不上)",
    "0e7a2b7d5d9e": "原链已空(分享里没有任何条目)",
    "a8110359b413": "原链已空(分享里没有任何条目)",
    "bd2013f4b95b": "原链已空(分享里没有任何条目)",
    "5bea6da448fa": "原链已空(上游把分享清空了;曾经是合法链)",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真改(默认只报告)")
    ap.add_argument("--links", default="", help="逗号分隔的短 id(默认用内置那 5 条)")
    ap.add_argument("--db", default="data/platform.db")
    a = ap.parse_args()
    bad = {k.strip(): BAD.get(k.strip(), "体检判定的坏链")
           for k in (a.links.split(",") if a.links else BAD) if k.strip()}
    con = sqlite3.connect(a.db)
    rows: list[tuple[str, str, int, str]] = []      # (表, 动作, 影响的键, 短id)
    for short, why in bad.items():
        like = f"%{short}%"
        rows.append(("discovered_pan_links", "our_url='' + status=pending",
                     con.execute("select count(*) from discovered_pan_links where our_url like ?",
                                 (like,)).fetchone()[0], short))
        rows.append(("douyin_leads", "our_url='' + moved_at/kouling_tried_at 清空",
                     con.execute("select count(*) from douyin_leads where our_url like ?",
                                 (like,)).fetchone()[0], short))
        rows.append(("wechat_articles", "my_pan_urls=''",
                     con.execute("select count(*) from wechat_articles "
                                 "where coalesce(my_pan_urls,'') like ?", (like,)).fetchone()[0],
                     short))
    print(f"{'表':22s}{'动作':44s}{'条数':>6s}  短id")
    for t, act, n, short in rows:
        print(f"{t:22s}{act:44s}{n:>6d}  {short}")
    total = sum(n for _, _, n, _ in rows)
    if not a.apply:
        print(f"\n[dry-run] 共 {total} 行会被改。加 --apply 才真动。")
        return 0
    for t, act, _n, short in rows:
        like = f"%{short}%"
        why = bad[short]
        if t == "discovered_pan_links":
            con.execute("update discovered_pan_links set our_url='', status='pending', "
                        "message=? where our_url like ?", (f"{why};已退回待重搬", like))
        elif t == "douyin_leads":
            con.execute("update douyin_leads set our_url='', moved_at=NULL, "
                        "kouling_tried_at=NULL, last_error=? where our_url like ?",
                        (f"{why};已退回待办", like))
        else:
            con.execute("update wechat_articles set my_pan_urls='' "
                        "where coalesce(my_pan_urls,'') like ?", (like,))
    con.commit()
    print(f"\n✅ 已改 {total} 行。等各自队列重搬(发现=积压额度 / 公众号=补链队列 / 抖音=待办)。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
