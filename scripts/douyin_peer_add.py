#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一个抖音**对标号**加进监控(2026-10-29)。

用法:
    python scripts/douyin_peer_add.py --name 抖音小游戏
    python scripts/douyin_peer_add.py --uid MS4wLjABAAAA...
    python scripts/douyin_peer_add.py --list
    python scripts/douyin_peer_add.py --remove <sec_uid>

⚠️ **同名号很多**(实测「小游戏推广」有 3 个不同 sec_uid,主页内容完全不同)——
    所以按名字解析时**只认完全同名**;有歧义就把候选全部打出来让你用 `--uid` 指定,
    **绝不替你猜**(猜错=监控了一个不相干的号,而且你看不出来)。

账号存在 `cross_platform_accounts`(`platform="douyin"`,`uid` = sec_uid),
扫描作业见 `app/services/douyin_peer_scan.py`。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLATFORM = "douyin"


def _list(user_id: int) -> int:
    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import CrossPlatformAccount

    session = get_session_local()()
    try:
        rows = session.scalars(select(CrossPlatformAccount).where(
            CrossPlatformAccount.user_id == user_id,
            CrossPlatformAccount.platform == PLATFORM).order_by(CrossPlatformAccount.id)).all()
        if not rows:
            print("库里还没有抖音对标号。用 --name / --uid 加一个。")
            return 0
        for r in rows:
            print(f"[{r.status}] {r.name or '—'} | {r.uid} | 上次扫描 {r.last_scan_at or '从未'}"
                  f" | 可见作品 {r.video_count}")
        return 0
    finally:
        session.close()


def _upsert(user_id: int, uid: str, name: str) -> str:
    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import CrossPlatformAccount

    session = get_session_local()()
    try:
        row = session.scalar(select(CrossPlatformAccount).where(
            CrossPlatformAccount.user_id == user_id,
            CrossPlatformAccount.platform == PLATFORM,
            CrossPlatformAccount.uid == uid))
        if row is None:
            session.add(CrossPlatformAccount(
                user_id=user_id, platform=PLATFORM, uid=uid, name=name or "",
                url=f"https://www.douyin.com/user/{uid}",
                hit_keyword="manual", snippet="", status="active"))
            session.commit()
            return "新增"
        row.status = "active"
        if name:
            row.name = name
        session.commit()
        return "已存在(重新启用)"
    finally:
        session.close()


def _discover(args) -> int:
    """从关键词结果里找候选号:**默认只打印(dry-run)**,加 `--apply` 才入库。

    ⚠️ 为什么默认不入库:同名号很多、且名字对不代表内容对(见 `discover_candidates`
    的两道闸门)。让人先看一眼候选,比自动把号塞进去稳妥。
    """
    from sqlalchemy import select

    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import CrossPlatformAccount
    from app.services.douyin_peer_scan import discover_candidates

    print(f"发现中:关键词「{args.discover}」 赛道「{args.niche or '不限'}」 —— "
          f"要开几次浏览器,约 2~4 分钟…")
    try:
        cands = discover_candidates(args.discover, get_settings(), niche=args.niche,
                                    limit=args.limit, min_share=args.min_share,
                                    require_pan=args.require_pan,
                                    user_id=args.user_id)
    except Exception as exc:  # noqa: BLE001 - 命令行工具:把原因说清楚,别抛栈
        print(f"发现失败:{exc}")
        print("⚠️ 若是「验证码中间页 / 未登录」,先别连打 —— 那是风控,不是「没有这个号」。")
        return 1
    if not cands:
        print("没有通过校验的候选。可能原因:结果里没有作者名 / 没有完全同名的号 / "
              "主页作品太少 / 赛道占比不够。放宽 --min-share 或换关键词再试。")
        return 0

    session = get_session_local()()
    try:
        have = {u for u in session.scalars(select(CrossPlatformAccount.uid).where(
            CrossPlatformAccount.user_id == args.user_id,
            CrossPlatformAccount.platform == PLATFORM)).all()}
    finally:
        session.close()

    print(f"\n{'名字':<18} {'sec_uid':<46} {'作品':>4} {'赛道占比':>8} {'带盘':>4}  示例")
    for c in cands:
        flag = "[已存在]" if c["uid"] in have else "[新]"
        print(f"{c['name'][:16]:<18} {c['uid']:<46} {c['works']:>4} {c['share']:>8.0%} "
              f"{c.get('pans', 0):>4}  {flag} {' / '.join(c['samples'][:1])}")

    if not args.apply:
        hint = (f"python scripts/douyin_peer_add.py --discover {args.discover}"
                + (f" --niche {args.niche}" if args.niche else "")
                + " --apply")
        print("\n以上是 **dry-run**,没有写库。确认没问题后加 --apply 再跑一次:"
              f"\n  {hint}")
        return 0

    n = 0
    for c in cands:
        if c["uid"] in have:
            continue
        print(f"{_upsert(args.user_id, c['uid'], c['name'])}: {c['name']} | {c['uid']}")
        n += 1
    print(f"\n入库 {n} 个(已存在的跳过)。下一轮扫描会自动给它们建基线。")
    return 0


def _resolve(name: str) -> list[dict]:
    from app.services.douyin_peer_scan import resolve_user

    return resolve_user(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="管理抖音对标号(按账号看新作品)")
    ap.add_argument("--name", help="账号昵称(会做一次抖音用户搜索)")
    ap.add_argument("--uid", help="直接给 sec_uid(MS4wLjABAAAA…)")
    ap.add_argument("--label", default="", help="配合 --uid 使用的显示名")
    ap.add_argument("--discover", metavar="关键词",
                    help="从关键词搜索结果里找候选号(默认只打印,dry-run)")
    ap.add_argument("--niche", default="",
                    help="配合 --discover:候选号主页里该赛道的占比门槛(如 小游戏)")
    ap.add_argument("--limit", type=int, default=8, help="配合 --discover:最多看几个候选")
    ap.add_argument("--min-share", type=float, default=0.5,
                    help="配合 --discover:赛道占比下限(默认 0.5)")
    ap.add_argument("--require-pan", action="store_true",
                    help="配合 --discover:只收**主页里带网盘**的号(直链/口令);"
                         "小游戏这类赛道建议加上")
    ap.add_argument("--apply", action="store_true",
                    help="配合 --discover:真的入库(不给就只打印)")
    ap.add_argument("--list", action="store_true", help="列出已配置的对标号")
    ap.add_argument("--remove", metavar="SEC_UID", help="停用(不是删除)一个号")
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args(argv)

    if args.list:
        return _list(args.user_id)

    if args.discover:
        return _discover(args)

    if args.remove:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import CrossPlatformAccount

        session = get_session_local()()
        try:
            row = session.scalar(select(CrossPlatformAccount).where(
                CrossPlatformAccount.user_id == args.user_id,
                CrossPlatformAccount.platform == PLATFORM,
                CrossPlatformAccount.uid == args.remove))
            if row is None:
                print(f"没找到 {args.remove}")
                return 1
            row.status = "dismissed"
            session.commit()
            print(f"已停用:{row.name or row.uid}")
            return 0
        finally:
            session.close()

    if args.uid:
        print(f"{_upsert(args.user_id, args.uid, args.label)}: {args.label or args.uid}")
        return 0

    if not args.name:
        ap.print_help()
        return 1

    candidates = _resolve(args.name)
    exact = [u for u in candidates if u["name"] == args.name]
    if len(exact) == 1:
        pick = exact[0]
    elif len(candidates) == 1:
        pick = candidates[0]
    else:
        #: ⚠️ 有歧义就**打印候选、停下** —— 见模块说明(猜错的代价是监控了一个不相干的号)。
        print(f"「{args.name}」匹配到 {len(candidates)} 个号,**没有完全同名的**,不替你猜:")
        for u in candidates[:12]:
            print(f"  {u['name']} | {u['uid']}")
        print("\n请用 --uid <sec_uid> --label <显示名> 指定一个。")
        return 2

    print(f"{_upsert(args.user_id, pick['uid'], pick['name'])}: {pick['name']} | {pick['uid']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
