"""**干跑**:看抖音线索下一轮会搜什么、为什么(2026-10-04)。

用法:
    python scripts/lead_round_preview.py          # 不联网、不花额度

**为什么有它**:这条链一次要跑几分钟 + 开浏览器 + 花抖音搜索额度,而"这轮会搜哪一类、
出哪几个词"其实**查库就能算出来**。有了它,调类目表/话题词时可以**先看效果再跑**,
不用拿真实配额试错。

顺带把"**为什么是这几个词**"摊开:候选从哪来、类目怎么挑、哪些被排后、人名学到了没有。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    from app.db.database import get_session_local
    from app.services import category_topics as ct
    from app.services.cross_accounts import _keywords_from_library
    from app.services.douyin_leads import (group_keywords, hot_seed_words,
                                           search_keywords, _words_already_resolved_to_group)
    from config.settings import get_settings

    st = get_settings()
    order = list(ct.categories())
    with get_session_local()() as s:
        cat = ct.current_category(s)
        nxt = order[(order.index(cat) + 1) % len(order)]
        mine = group_keywords(s, 1, top=st.douyin_leads_group_keywords)
        lib = _keywords_from_library(s, 1, max(1, st.douyin_leads_keywords))
        hot = hot_seed_words(st)
        kws = search_keywords(s, 1, st.douyin_leads_keywords, st, hot=hot)
        seen = _words_already_resolved_to_group(s, 1)
        names = ct.known_names(s)

    print(f"类目表({len(order)} 类,轮换顺序): {' → '.join(order)}")
    print(f"\n本轮类目:【{cat}】   下轮:【{nxt}】")
    print("\n候选(群里的资源名 + 资源库名称):")
    for w in dict.fromkeys(mine + lib):
        print(f"    {w:<34} → {ct.classify(w, names) or '(未分类)'}")
    print(f"\n本轮搜索词({len(kws)} 个,前面的先搜):")
    for i, w in enumerate(kws, 1):
        tag = []
        if w in seen:
            tag.append("已解析出过群→排后")
        if w in hot:
            tag.append("热榜")
        print(f"    {i}. {w:<34} {' '.join(tag)}")
    print(f"\n已学到的人名:{sorted(names) or '(还没攒够 2 次)'}")
    print("⚠️ 上面是**干跑**;真跑请 `python -c \"from app.services.douyin_leads import "
          "douyin_leads_tick as t; t()\"`(要开浏览器,几分钟)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
