#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""手动跑一轮**评论区「在求什么」**监控(2026-10-30,只读名字、不落网盘)。

定时任务已经挂在 `scheduler`(`demand_watch`,默认 `:50 */4`)。这个脚本用于:
- 改完种子词/阈值后**立刻看效果**;
- 排查"为什么这轮没推"(`runs` 里 `kind=demand_watch` 的明细)。

跑法:
    python scripts/demand_watch.py                # 真跑(会推飞书卡片)
    python scripts/demand_watch.py --dry          # 只看结果,不推卡
    python scripts/demand_watch.py --user 1 --dry

⚠️ 它**会用抖音额度**(搜索 + 拉评论):一轮 = 1 次搜索 + 最多
`demand_max_videos × demand_comment_pages` 次评论请求。刚被限流(`verify_check`)时别连跑。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="评论区在求什么 —— 跑一轮")
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--dry", action="store_true", help="不推飞书卡片,只打印结果")
    args = ap.parse_args(argv)

    from app.db.database import get_session_local, init_db
    from app.services import demand_watch
    from config.settings import get_settings

    if args.dry:
        demand_watch.push_card = lambda rows, settings: False   # noqa: ARG005

    init_db()
    db = get_session_local()()
    try:
        out = demand_watch.run_user(db, args.user, get_settings())
    finally:
        db.close()
    print(f"结果:{out}")
    if out.get("skipped"):
        print(f"⚠️ 本轮跳过:{out['skipped']}(检查 settings 的 demand_* 配置)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
