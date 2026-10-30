#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按**网盘里已有的资料**盘点监控方向(2026-10-30)。

用户口径:「根据我们网盘里面的已有资料**进行个分类**,然后**根据这些个分类来进行监控**」。

输出三块:
1. 库里每个方向的**条目数**(分类口径 = `category_topics.type_map`:名字 + 父目录 + 同批次多数);
2. 该方向**能出的搜索词**(`douyin_leads.category_search_words`,只认可信文件夹);
3. 当前轮换的类目里,哪些方向**库里有货**、哪些只能靠话题词。

跑法:
    python scripts/library_directions.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import XunleiResource  # noqa: E402
from app.services import category_topics as ct  # noqa: E402
from app.services.douyin_leads import category_search_words  # noqa: E402


def main() -> int:
    session = get_session_local()()
    try:
        rows = session.execute(
            select(XunleiResource.name, XunleiResource.parent_name)).all()
        pairs = [(str(n or ""), str(p or "")) for n, p in rows]
        types = ct.type_map(pairs)
        by_type: dict[str, list[str]] = {}
        for name, t in types.items():
            if t:
                by_type.setdefault(t, []).append(name)
        print(f"资源库 {len(pairs)} 条,分得出方向 {sum(len(v) for v in by_type.values())} 条\n")

        parent_of = {n: p for n, p in pairs}
        print("== 库内方向(按条目数) ==")
        for t, names in sorted(by_type.items(), key=lambda x: -len(x[1])):
            subs = Counter(ct.subtype_of(n, parent_of.get(n, "")) for n in names
                           if ct.subtype_of(n, parent_of.get(n, "")))
            sub_txt = ("  细分:" + " ".join(f"{k}{v}" for k, v in subs.most_common())) if subs else ""
            print(f"  {t:6} {len(names):5}{sub_txt}")

        print("\n== 每个方向能出的搜索词(监控实际用的) ==")
        for t in sorted(by_type, key=lambda x: -len(by_type[x])):
            words = category_search_words(session, 1, t, top=5)
            print(f"  {t:6} → {words if words else '(无可信目录 ⇒ 用本方向话题词兜底)'}")

        print("\n== 当前轮换类目 vs 库内供给 ==")
        for cat in ct.categories():
            n = len(by_type.get(cat, []))
            words = category_search_words(session, 1, cat, top=3)
            print(f"  {cat:6} 库内 {n:5} 条 | 专属词 {'有' if words else '无'}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
