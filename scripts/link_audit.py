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

from app.services.link_audit import SOURCES, Prober, _first_url, _verdict  # noqa: E402


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
    for tag in ("打不开", "空分享", "货不对版", "待人工看"):
        bad = [r for r in rows if r["verdict"] == tag]
        if not bad:
            continue
        print(f"\n=== {tag}({len(bad)})===")
        for r in bad[:12]:
            print(f"  [{r['source']}/{r['platform']}] {r['title']}")
            print(f"      实际顶层: {r['why']}")
            print(f"      {r['url'][:88]}")
    print(f"\n对齐 {by_v.get('对齐', 0)}/{len(rows)}" + ("" if rows else " (没抽到样本)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
