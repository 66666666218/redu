#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按**方向**统计抖音线索的产出(只读,不改任何配置)—— 2026-10-30。

用户口径:「先继续观察,**到一定数据后再进行决策的调整**」。

它回答一个问题:**每个方向轮到时,到底有没有产出"这个方向"的线索?**

数据源:`runs`(kind=douyin_leads)的运行记录 —— 每轮写着
`类目影视 ... 线索5 赛道小游戏1/影视1/短剧5 新群1/...`。

判据(写死,便于回测):
· **本方向线索率** = 本方向线索 / 该轮线索(影视把 漫剧/短剧/动漫/电影/电视剧 都算本方向);
· 样本 ≥ `--min-rounds`(默认 8 轮)才给结论;
· 结论:率 < 20% → **建议调整**(换话题词/降频);20%~50% → **观察**;≥50% → **有效**。

跑法:
    python scripts/direction_stats.py            # 近 14 天
    python scripts/direction_stats.py --days 30 --min-rounds 12
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import RunRecord  # noqa: E402
from app.services.category_topics import categories  # noqa: E402

#: 影视的细分都算「影视」方向
_SIBLINGS = {
    "影视": {"影视", "漫剧", "短剧", "动漫", "电影", "电视剧"},
}


def parse_detail(detail: str) -> dict | None:
    """从运行记录里取出 `{类目, 线索, 赛道, 新群}`;不是线索轮返回 None。"""
    d = str(detail or "")
    cat = re.search(r"类目(\S+)", d)
    #: ⚠️ **`赛道` 是 2026-10-29 才加进运行记录的** —— 更早的轮次没有它,
    #: 算进来会把"没测过"当成"0 产出"(实测 14 天窗口里全是 0~2%)。
    if not cat or "赛道" not in d:
        return None
    n = re.search(r"线索(\d+)", d)
    track = re.search(r"赛道([^\s]+)", d)
    new = re.search(r"新群(\d+)", d)
    counts: dict[str, int] = {}
    for part in (track.group(1) if track else "").split("/"):
        m = re.match(r"^(.*?)(\d+)$", part)
        if m and m.group(1):
            counts[m.group(1)] = int(m.group(2))
    return {"cat": cat.group(1), "leads": int(n.group(1)) if n else 0,
            "tracks": counts, "new_groups": int(new.group(1)) if new else 0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按方向统计抖音线索产出(只读)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-rounds", type=int, default=8)
    args = ap.parse_args(argv)

    session = get_session_local()()
    try:
        rows = session.execute(
            select(RunRecord.started_at, RunRecord.detail).where(
                RunRecord.kind == "douyin_leads",
                RunRecord.started_at >= datetime.now() - timedelta(days=max(1, args.days)))
            .order_by(RunRecord.id)).all()
        agg: dict[str, dict] = defaultdict(lambda: {"rounds": 0, "leads": 0, "mine": 0,
                                                    "zero": 0, "new_groups": 0})
        for started, detail in rows:
            parsed = parse_detail(detail)
            if not parsed:
                continue
            cat = parsed["cat"]
            a = agg[cat]
            a["rounds"] += 1
            a["leads"] += parsed["leads"]
            a["new_groups"] += parsed["new_groups"]
            if parsed["leads"] == 0:
                a["zero"] += 1
            family = _SIBLINGS.get(cat, {cat})
            a["mine"] += sum(v for k, v in parsed["tracks"].items() if k in family)

        if not agg:
            print("窗口内没有可解析的线索轮。")
            return 0
        print(f"近 {args.days} 天抖音线索,按**方向**统计(样本门槛 {args.min_rounds} 轮)\n")
        print(f"{'方向':<6}{'轮次':>4}{'线索':>6}{'本方向':>7}{'本方向率':>9}{'空轮':>5}{'新群':>5}  结论")
        for cat in [c for c in categories() if c in agg] + \
                   [c for c in agg if c not in categories()]:
            a = agg[cat]
            rate = a["mine"] / a["leads"] if a["leads"] else 0.0
            if a["rounds"] < args.min_rounds:
                verdict = "样本不足,继续观察"
            elif rate < 0.2:
                verdict = "**建议调整**(换话题词/降频)"
            elif rate < 0.5:
                verdict = "观察"
            else:
                verdict = "有效"
            print(f"{cat:<6}{a['rounds']:>4}{a['leads']:>6}{a['mine']:>7}"
                  f"{rate:>9.0%}{a['zero']:>5}{a['new_groups']:>5}  {verdict}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
