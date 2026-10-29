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


def _resolve(name: str) -> list[dict]:
    from app.services.douyin_peer_scan import resolve_user

    return resolve_user(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="管理抖音对标号(按账号看新作品)")
    ap.add_argument("--name", help="账号昵称(会做一次抖音用户搜索)")
    ap.add_argument("--uid", help="直接给 sec_uid(MS4wLjABAAAA…)")
    ap.add_argument("--label", default="", help="配合 --uid 使用的显示名")
    ap.add_argument("--list", action="store_true", help="列出已配置的对标号")
    ap.add_argument("--remove", metavar="SEC_UID", help="停用(不是删除)一个号")
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args(argv)

    if args.list:
        return _list(args.user_id)

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
