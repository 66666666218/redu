# -*- coding: utf-8 -*-
"""**低频**量出微信读书列表接口 `-2041` 的窗口形状(什么时候开、一次能连发几发、按账号还是全局)。

## 为什么需要它(而不是继续手工试)
2026-10-18 已经用掉一整轮探测:requests / curl / 真 Chromium 三种指纹、两个账号、
三种请求上下文(首页 / 无 Referer / 阅读器页)、静默 3 分钟 —— **全 `-2041`**;
但同一天里**两次**真的拿到过带 `readNum` 的 200。
⇒ 它是**间歇**的,而"每 10 分钟手工打一次"既量不出节奏,又可能把窗口推后
(`doc/外部接口速查.md` §3.2.3 的教训)。

## 它做什么
每 `--interval` 分钟,对**每个账号各打一发**(间隔 5 秒),把 `时间 账号=结果` 追加进
`data/_weread_window_probe.log`。**只读、不写库、不碰生产链路**。

## 判据(拿到日志后怎么读)
- 同一时刻 `acct1` 与 `acct2` 一个 OK 一个 -2041 ⇒ **按账号**,加号有用;
- 两个一起 OK / 一起挡 ⇒ **全局**(IP 或环境级),加号无用;
- 全部时间都没 OK ⇒ 窗口比采样周期还稀,把 `--interval` 放大再看。

## 用法
    set WR_COOKIE_1=<账号1整条 cookie>
    set WR_COOKIE_2=<账号2整条 cookie>
    python scripts/probe_weread_window.py --interval 20 --rounds 24

⚠️ 两个 cookie 都只从环境变量读,**不接受命令行参数**(避免出现在进程列表/历史里)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import requests  # noqa: E402

from app.utils.ua import CHROME_WINDOWS  # noqa: E402

BASE = "https://weread.qq.com"
LOG = os.path.join("data", "_weread_window_probe.log")
BOOK = os.environ.get("WR_BOOK", "")


def cookies() -> list[tuple[str, str]]:
    out = []
    for i in (1, 2, 3):
        v = (os.environ.get(f"WR_COOKIE_{i}") or "").strip()
        if v:
            out.append((f"acct{i}", v))
    return out


def one(cookie: str, book: str) -> str:
    try:
        r = requests.get(f"{BASE}/web/mp/articles",
                         params={"bookId": book, "offset": 0, "count": 20},
                         headers={"User-Agent": CHROME_WINDOWS, "Cookie": cookie,
                                  "Accept": "application/json, text/plain, */*",
                                  "Referer": f"{BASE}/"}, timeout=20)
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        return f"ERR:{type(exc).__name__}"
    if "errCode" in d:
        return f"err{d.get('errCode')}"
    n = sum(len(g.get("subReviews") or []) for g in (d.get("reviews") or []))
    reads = sum(1 for g in (d.get("reviews") or [])
                for s in (g.get("subReviews") or [])
                if int(((s.get("review") or {}).get("mpInfo") or {}).get("readNum") or 0) > 0)
    return f"OK:{n}篇/{reads}带读数"


def main() -> int:
    ap = argparse.ArgumentParser(description="低频探测微信读书列表窗口")
    ap.add_argument("--interval", type=int, default=20, help="分钟")
    ap.add_argument("--rounds", type=int, default=24)
    args = ap.parse_args()

    cks = cookies()
    book = BOOK
    if not cks:
        print("❌ 需要环境变量 WR_COOKIE_1 / WR_COOKIE_2(整条 cookie)")
        return 2
    if not book:
        from sqlalchemy import text
        from app.db.database import get_session_local
        db = get_session_local()()
        book = str(db.execute(text(
            "select weread_book_id from wechat_benchmarks "
            "where weread_book_id like 'MP_WXS_%' and active=1 limit 1")).scalar() or "")
        db.close()
    if not book:
        print("❌ 没有可用的 bookId(可用 WR_BOOK 指定)")
        return 2

    print(f"开始:每 {args.interval} 分钟 × {len(cks)} 个账号 × {args.rounds} 轮;book={book}")
    with open(LOG, "a", encoding="utf-8") as fh:
        for n in range(1, args.rounds + 1):
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            parts = []
            for label, ck in cks:
                parts.append(f"{label}={one(ck, book)}")
                time.sleep(5)          # 两发之间留间隔,别当成一次连发
            line = f"{ts} round{n:02d} " + " ".join(parts)
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()
            if n < args.rounds:
                time.sleep(max(60, args.interval * 60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
