#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 分数线校准 / 历史回填(2026-10-29)。

用法:
    python scripts/ai_calibrate.py --backfill     # 给历史线索补 AI 分数(一次性,几十次调用)
    python scripts/ai_calibrate.py                # 只看:各分数段的真实搬成率(不写库)
    python scripts/ai_calibrate.py --apply        # 校准并把阈值写进 system_config

为什么先回填:校准要「AI 分数 ↔ 真实搬成」配对;库里已有近一个月线索与结果,
补一次分数就能**立刻**得到第一条结论,不用等几天。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _table(pairs: list[tuple[int, int]]) -> None:
    if not pairs:
        print("没有可用的配对样本(先 --backfill)")
        return
    n = len(pairs)
    print(f"样本 {n} 条;阈值 → 保留 / 保留占比 / 保留部分**高热率**")
    for t in range(0, 11):
        kept = [m for s, m in pairs if s >= t]
        if not kept:
            continue
        print(f"  >= {t:2d}  留 {len(kept):4d} ({len(kept) / n:5.0%})  高热 "
              f"{sum(kept) / len(kept):5.0%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI 分数线校准 / 历史回填")
    ap.add_argument("--backfill", action="store_true", help="给历史线索补 AI 分数")
    ap.add_argument("--apply", action="store_true", help="把校准结果写进 system_config")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args(argv)

    from app.db import get_session_local
    from app.services import ai_calibration as ac
    from config.settings import get_settings

    settings = get_settings()
    session = get_session_local()()
    try:
        if args.backfill:
            print("回填中(每批 40 条调用一次模型,约 1~4 分钟)…")
            print(ac.backfill(session, args.user_id, settings, days=args.days))

        hot_min = int(getattr(settings, "ai_calibration_hot_share", 100) or 100)
        pairs = ac.heat_pairs(session, args.user_id, "douyin_peer", days=args.days,
                              hot_min=hot_min)
        print(f"标签 = 转发数 ≥ {hot_min}(高热)")
        _table(pairs)
        pick = ac.choose_threshold(
            pairs, min_samples=int(getattr(settings, "ai_calibration_min_samples", 40) or 40))
        print("\n建议:", pick)
        if args.apply:
            res = ac.calibrate(session, args.user_id, "douyin_peer", settings, days=args.days)
            session.commit()
            print("已写入:", res)
        else:
            print("(未写库;确认后加 --apply)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
