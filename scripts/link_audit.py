"""命令行壳:`python scripts/link_audit.py --n 50` 手动全量排查。

**逻辑在 `app/services/link_audit.py`**(定时作业要调它,所以放那一侧);判据说明见
`doc/链接体检.md`。本文件只负责:取库、解析参数、打印。

⚠️ **只读**:只调各盘"取分享详情"那类接口,不转存、不分享、不删。
⚠️ 每次探活都是一次平台请求(夸克要两次)⇒ 默认抽样、默认留间隔。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.services.link_audit import (  # noqa: E402
    CONTAINER_MARK, SOURCES, Prober, _first_url, _verdict, bad_groups,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="每个来源抽样多少条(默认 20)")
    ap.add_argument("--gap", type=float, default=1.0, help="每次探活之间的间隔秒(默认 1)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default="data/platform.db")
    ap.add_argument("--sources", default="", help="只跑这些来源(逗号分隔,子串匹配);默认全跑")
    a = ap.parse_args()
    only = [x for x in (a.sources or "").split(",") if x]

    import time

    con = sqlite3.connect(a.db)
    pr = Prober()
    rows: list[dict] = []
    for label, table, name_col, link_col in SOURCES:
        if only and not any(o in label for o in only):
            continue
        try:
            got = con.execute(
                f"select {name_col}, {link_col} from {table} "
                f"where coalesce({link_col},'') like '%http%' order by id desc limit ?",
                (a.n,)).fetchall()
        except sqlite3.Error as exc:
            print(f"⚠️ {label}: 查不动({exc})")
            continue
        for title, raw in got:
            url = _first_url(str(raw or ""))
            if not url:
                continue
            p = pr.probe(url)
            verdict, why = _verdict(str(title or ""), p)
            rows.append({"source": label, "title": str(title or "")[:44], "url": url,
                         "platform": p.get("platform") or "", "verdict": verdict,
                         "why": str(why)[:44]})
            time.sleep(max(0.0, a.gap))
    con.close()

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    from collections import Counter

    by_v = Counter(r["verdict"] for r in rows)
    print(f"共探 {len(rows)} 条 | " + " | ".join(f"{k} {v}" for k, v in by_v.most_common()))
    #: ⚠️ **先给按链聚合的视角**(2026-11-18):一条链被 N 个标题复用是常态,
    #: 按行看会让人以为"到处都坏",而实际可能只是同一条分享被复用了 N 次。
    groups = bad_groups(rows)
    if groups:
        print(f"\n=== 要人看的链 {len(groups)} 条(按影响面排序)===")
        for url, kinds, why, n in groups[:12]:
            print(f"  [{'/'.join(kinds)}] 影响{n}个标题 | {why[:56]}")
            print(f"      {url[:88]}")
    #: ⚠️ 「待人工看」里混着两种东西(2026-11-18):容器名不符**要人动手查**,
    #: 限流只是我们自己等一等 ⇒ 分两段打印,别让后者把前者淹掉。
    for tag in ("打不开", "空分享", "货不对版", "待人工看"):
        bad = [r for r in rows if r["verdict"] == tag]
        if not bad:
            continue
        groups = [(tag, bad)]
        if tag == "待人工看":
            groups = [("待人工看·容器名不符",
                       [r for r in bad if CONTAINER_MARK in r["why"]]),
                      ("待人工看·限流/网络",
                       [r for r in bad if CONTAINER_MARK not in r["why"]])]
        for name, items in groups:
            if not items:
                continue
            print(f"\n=== {name}({len(items)})===")
            for r in items[:12]:
                print(f"  [{r['source']}/{r['platform']}] {r['title']}")
                print(f"      实际顶层: {r['why']}")
                print(f"      {r['url'][:88]}")
    print(f"\n对齐 {by_v.get('对齐', 0)}/{len(rows)}" + ("" if rows else " (没抽到样本)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
