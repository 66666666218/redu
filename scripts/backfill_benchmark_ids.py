"""存量**哑号**回填:`biz`/`weread_book_id` 都空的对标号,按名字去后台换成 `MP_WXS_*`。

## 为什么要它(2026-10-14 实测)
按名收录那条链此前**依赖 WeRSS 容器**,容器停摆期间每天建 8 个"哑行"(既无 `biz` 也无
`weread_book_id`)⇒ 全库积了 **83 个哑号**。解析能力修好之后(`_source._biz_from_wemp`),
**每天的收录作业只能救回它当天挑中的那几个** —— 而多数哑号**早已不在候选表里**了
(候选被挑过就没有了)⇒ 靠日常作业**永远修不到它们**。这正是本仓说的"存量死账":
判据在,但没有**入口**去碰这些行。

## 用法
    python scripts/backfill_benchmark_ids.py                 # 默认 dry-run:只**报告**能救活多少
    python scripts/backfill_benchmark_ids.py --apply         # 真写库
    python scripts/backfill_benchmark_ids.py --apply --limit 30 --sleep 1.5

⚠️ 每条一次 `searchbiz` 请求(实测**不受 `200013` 限流影响**,与 `appmsgpublish` 是两个端点),
默认 `--sleep 1.5` 节流:宁慢勿快 —— 这是**微信侧**的接口,连着猛打才是我方风控的来路。
⚠️ 判据与 `_source._biz_from_wemp` **共用一处**(不在这里重写):昵称精确比对、重名歧义不猜、
拿不到的**留空**(空 = 还不知道,写错 = 把别人的号挂上去)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import User, WechatBenchmark  # noqa: E402


def _dumb_rows(session, user_id: int) -> list[WechatBenchmark]:
    """`biz` 与 `weread_book_id` **都空**的对标号(实测库里这两种标识是"要么都有、要么都没有")。"""
    return list(session.scalars(select(WechatBenchmark).where(
        WechatBenchmark.user_id == user_id,
        (WechatBenchmark.biz == "") | (WechatBenchmark.biz.is_(None)),
        (WechatBenchmark.weread_book_id == "") | (WechatBenchmark.weread_book_id.is_(None)),
    ).order_by(WechatBenchmark.id)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--apply", action="store_true", help="真写库(默认只报告)")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多处理几条(0 = 不限)")
    ap.add_argument("--sleep", type=float, default=1.5, help="每条之间停多久(秒)")
    args = ap.parse_args()

    # ⚠️ 门面先加载:子模块直接 import 会撞循环依赖(见 `wechat/__init__.py` 的说明)
    from app.services import wechat_monitor as wm  # noqa: F401
    import app.services.wechat._source as src

    db = get_session_local()()
    try:
        users = [args.user] if args.user else [
            uid for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all()]
        for uid in users:
            rows = _dumb_rows(db, uid)
            todo = rows[:args.limit] if args.limit else rows
            print(f"用户 {uid}:哑号 {len(rows)} 个,本轮处理 {len(todo)} 个"
                  f"({'只报告' if not args.apply else '**真写库**'})")
            ok = 0
            why_count: dict[str, int] = {}
            for i, row in enumerate(todo, 1):
                biz, why = src._biz_from_wemp(db, uid, row.nickname)
                if biz:
                    ok += 1
                    print(f"  [{i}/{len(todo)}] ✅ {row.nickname!r} → {biz}")
                    if args.apply:
                        # 只补空位(这一支本来就是"两个都空"才进来的)
                        row.biz = biz[:64]
                        row.weread_book_id = biz[:64]
                        db.commit()
                else:
                    why_count[why[:40] or "(空原因)"] = why_count.get(why[:40] or "(空原因)", 0) + 1
                    print(f"  [{i}/{len(todo)}] ⛔ {row.nickname!r} —— {why[:60]}")
                if i < len(todo):
                    time.sleep(max(0.0, args.sleep))
            print(f"用户 {uid}:可救活 {ok}/{len(todo)}")
            for why, n in sorted(why_count.items(), key=lambda kv: -kv[1]):
                print(f"    没救活的原因 ×{n}:{why}")
    finally:
        db.close()
    if not args.apply:
        print("\n[dry-run] 什么都没写;要真写库加 --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
