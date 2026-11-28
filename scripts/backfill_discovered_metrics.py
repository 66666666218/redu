"""存量**待办链**的互动指标回填(2026-11-28):按原帖 URL 把 `metrics` 补上。

## 为什么要它

`discovered_pan_links.metrics` 是 2026-11-28 才加的列 —— 此前**抓到即丢**,而
「盘满直推」的卡片是从这张表重建的 ⇒ 存量 `pending/failed` 行在卡片上「对标数据」
恒空(实测 11-26 那批 7 条里 6 条为空)。

日常作业只补「**本轮又搜到**」的那几条,而存量待办**多半已经搜不到了**(几个月前的链)
⇒ 靠日常作业**永远等不到**。这正是本仓说的"存量死账":判据在,但没有**入口**去碰这些行
(与 `backfill_benchmark_ids.py` 同一类问题)。

## 只做贴吧(其它平台不在这里)

`source_url` 是 `https://tieba.baidu.com/p/<tid>` ⇒ **一次 `get_posts` 拿全**
(全帖指标与首楼全文同一次返回,见 `tieba_metrics`),**匿名**即可。
微博/知乎要按单帖走另一套接口,不在本脚本范围(它们下次被搜到时会自己补上)。

## 用法

    python scripts/backfill_discovered_metrics.py                 # dry-run:只报数
    python scripts/backfill_discovered_metrics.py --apply
    python scripts/backfill_discovered_metrics.py --apply --limit 100 --batch 30

⚠️ **必须分批**:`fetch_posts_detail` 一次调用有 30 秒硬超时(见 `tieba_metrics`),
338 条一次跑完必然超时、全部白跑。默认 `--batch 30`(实测 ~0.46s/帖 ⇒ 约 14 秒/批),
批间 `--sleep 1.0` 节流 —— 这是贴吧的匿名接口,连着猛打才是风控的来路。
⚠️ 拿不到指标的行**保持空串不动**(空 = 还不知道;写 0 = 把"没给"说成"没人看")。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import DiscoveredPanLink  # noqa: E402


def _empty_metric_rows(session, user_id: int, limit: int) -> list[DiscoveredPanLink]:
    """待办里**指标还是空**的那些行(`pending`/`failed` —— 只有它们会被重试/直推)。"""
    return list(session.scalars(
        select(DiscoveredPanLink).where(
            DiscoveredPanLink.user_id == user_id,
            DiscoveredPanLink.status.in_(("pending", "failed")),
            (DiscoveredPanLink.metrics.is_(None)) | (DiscoveredPanLink.metrics == ""),
        ).order_by(DiscoveredPanLink.id.asc()).limit(limit)).all())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 discovered_pan_links.metrics(贴吧源)")
    ap.add_argument("--user-id", type=int, default=1)
    ap.add_argument("--limit", type=int, default=500, help="本次最多处理多少行")
    ap.add_argument("--batch", type=int, default=30,
                    help="每批取多少条详情(>60 会撞上 fetch_posts_detail 的 30 秒超时)")
    ap.add_argument("--sleep", type=float, default=1.0, help="批间停顿(秒)")
    ap.add_argument("--apply", action="store_true", help="真写库(默认只报告)")
    args = ap.parse_args(argv)

    from app.services.pan_discovery import _tid_of, metrics_json
    from app.services.tieba_metrics import detail_to_metrics, fetch_posts_detail

    db = get_session_local()()
    try:
        rows = _empty_metric_rows(db, args.user_id, args.limit)
        #: 只认贴吧原帖 —— 其它源没有"一次请求拿全指标"的入口,硬补会变成瞎猜
        targets = [(r, _tid_of(r.source_url)) for r in rows]
        targets = [(r, t) for r, t in targets if t]
        print(f"待办里指标为空 {len(rows)} 行,其中贴吧原帖 {len(targets)} 行")
        if not targets:
            return 0
        if not args.apply:
            print("dry-run:没写库。加 --apply 执行。")
            for r, t in targets[:5]:
                print(f"  例: tid={t} status={r.status} title={(r.title or '')[:24]}")
            return 0

        filled = fetched = 0
        for start in range(0, len(targets), args.batch):
            chunk = targets[start:start + args.batch]
            tids = [t for _r, t in chunk]
            details = fetch_posts_detail(tids, limit=len(tids))
            fetched += len(details)
            for row, tid in chunk:
                mj = metrics_json(detail_to_metrics(details.get(tid)))
                if not mj:
                    continue          #: 取不到就留空 —— 空 = 还不知道
                row.metrics = mj
                filled += 1
            db.commit()               #: 逐批提交:中途断了已补的不会白跑
            print(f"  [{start + len(chunk)}/{len(targets)}] 本批拿到详情 "
                  f"{len(details)} 条,累计写入 {filled} 行")
            if start + args.batch < len(targets):
                time.sleep(max(0.0, args.sleep))
        print(f"完成:处理 {len(targets)} 行,取到详情 {fetched} 条,写入指标 {filled} 行,"
              f"仍为空 {len(targets) - filled} 行(多为死帖/无指标)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
