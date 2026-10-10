# -*- coding: utf-8 -*-
"""给**已发出的夸克分享链**补「宣传简介」(2026-10-10)。

## 为什么要它
抽了 25 条我方夸克分享,**24 条里没有「简介.doc」** —— 而简介目录 `/监听宣传` 配置正常、
`copy_into` 也工作正常。⇒ 建链时那一步没落地,而失败只 `logger.warning`、日志又不落盘,
**事后查不出来**。已发出的链就一直缺着。

## 关键事实(实测出来的,决定了本脚本能不能这么简单)
**夸克的分享是「活的」** —— 往分享目录(`first_fid`)里复制进 `简介.doc`,**分享立刻跟着变**。
⇒ **不用重建链、不换链接、不影响已经发出去的链。**

## 所以本脚本只做一件事
对每条我方分享:看它 `first_fid` 目录里有没有简介文件 → **没有就复制进去**。
**只增不删**,不改分享、不动别的目录。

## 用法
    python scripts/quark_intro_backfill.py --dry-run      # 默认:只统计,不落地
    python scripts/quark_intro_backfill.py --yes --max 20 # 真做,先来 20 条
    python scripts/quark_intro_backfill.py --yes          # 全做(几千条,慢)

⚠️ 每条要 2~3 次 API(`列目录` + `复制` + 轮询),几千条会跑很久 ⇒ 建议 `--max` 分批。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

#: 我方分享列表的分页上限(每页 50;2945 条约 59 页)
SHARE_PAGE_CAP = 200
#: 简介文件的**名字判据**(与 quark_dup 的保护名单同口径:剥掉扩展名也比一次)
INTRO_MARKS = ("简介", "宣传")


def _has_intro(names: list[str]) -> bool:
    return any(any(m in n for m in INTRO_MARKS) for n in names)


def main() -> int:
    from app.db import get_session_local
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkTransfer
    from config.settings import get_settings

    yes = "--yes" in sys.argv
    cap = None
    scan = None          # 只"看"多少条(dry-run 抽样用 —— 每条要 1~2 次 API,全扫要一小时)
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--max" and i + 1 < len(args):
            cap = int(args[i + 1])
        elif a == "--scan" and i + 1 < len(args):
            scan = int(args[i + 1])

    s = get_settings()
    db = get_session_local()()
    try:
        ck = get_cookie(db, 1, "quark") or getattr(s, "quark_cookie", "") or ""
    finally:
        db.close()
    qt = QuarkTransfer(ck, fid_store=getattr(s, "quark_fid_store", "") or None)

    intro_dir = (getattr(s, "pan_intro_quark_dir", "") or "").strip()
    if not intro_dir:
        print("❌ 没配 `pan_intro_quark_dir` —— 不知道简介在哪,退出")
        return 2
    src_fid = qt.resolve_named_dir(intro_dir)
    promo = [x for x in qt._list_dir(src_fid) if not x.get("dir")] if src_fid else []
    if not promo:
        print(f"❌ 简介目录 `{intro_dir}` 不存在或是空的 —— 退出")
        return 2
    print(f"简介目录 `{intro_dir}`: {[x.get('file_name') for x in promo]}")
    print(f"模式:{'真做(--yes)' if yes else '只统计(dry-run,不落地)'}"
          f"{f' · 上限 {cap} 条' if cap else ''}\n")

    # 拉我方分享列表
    shares: list[dict] = []
    for page in range(1, SHARE_PAGE_CAP + 1):
        d = qt._request("GET", "/1/clouddrive/share/mypage/detail", api="https://drive-pc.quark.cn",
                        params={"share_id": "", "_page": page, "_size": 50,
                                "_order": "created_at:desc"})
        lst = list((d.get("data") or {}).get("list") or [])
        shares.extend(lst)
        if len(lst) < 50:
            break
    print(f"我方分享共 {len(shares)} 条")

    ok = skip = fixed = fail = 0
    for idx, a in enumerate(shares):
        if scan is not None and idx >= scan:
            print(f"\n(只看前 {scan} 条,停下)")
            break
        if cap is not None and fixed >= cap:
            print(f"\n(到上限 {cap} 条,停下)")
            break
        fid = str(a.get("first_fid") or "")
        if not fid:
            fail += 1
            continue
        try:
            names = [str(x.get("file_name") or "") for x in qt._list_dir(fid)]
            if _has_intro(names):
                skip += 1
                continue
            # ⚠️ 只对**目录型**的内容补:first_fid 是个文件时无处可放(不硬塞)
            if not names:
                print(f"  ⚠️ {a.get('title')!r} 的 first_fid 是空的,跳过")
                fail += 1
                continue
            if not yes:
                print(f"  [dry] {str(a.get('title'))[:34]:36} 缺简介,内含 {len(names)} 项")
                ok += 1
                continue
            ids = qt.copy_into(promo, fid)
            if ids:
                fixed += 1
                print(f"  ✓ {str(a.get('title'))[:34]:36} 已补({len(ids)} 个)")
            else:
                fail += 1
                print(f"  × {str(a.get('title'))[:34]:36} 复制没落地")
        except Exception as exc:                    # noqa: BLE001 - 单条失败不中断
            fail += 1
            print(f"  × {str(a.get('title'))[:34]:36} {type(exc).__name__}: {str(exc)[:60]}")

    print(f"\n统计:有简介(跳过) {skip} · {'待补(dry)' if not yes else '已补'} {ok or fixed} · 失败 {fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
