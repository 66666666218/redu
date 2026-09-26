"""分辨微信读书 `/web/mp/articles` 的 -2041 到底是"账号被限权"还是"调用上下文不对"。

为什么要它:同日多篇漏推的唯一免费解就是这条列表接口。本仓库 2026-09-18 的实测结论是
"账号级永久限权(换任意新 Cookie 依旧 -2041)"(`doc/operations.md` §9.2),但当时**所有**
测试都用同一套请求头(首页 `Referer: https://weread.qq.com/`)——换 Cookie 不换上下文,
证不了是账号问题。GitHub 上有项目指出同一接口在阅读器页上下文里正常、在首页上下文里必返
-2041(`Pengyf04/weread-mp-fetcher` 的 HOW-IT-WORKS),也有项目纯 requests + 首页 Referer
就能列(`rachelos/we-mp-rss` PR#462)。所以这条判断只能在**线上那枚活 Cookie**上分辨。

用法(在生产容器里跑,零副作用:只发 2~4 个只读 GET,不写库、不换 Cookie、不打轮换接口):

    python scripts/probe_weread_list.py --user 1 [--book-id MP_WXS_xxx]
    # 带上阅读器页 URL 才算探到 GitHub 说"可用"的那一种上下文:
    python scripts/probe_weread_list.py --user 1 --reader-url https://weread.qq.com/web/reader/xxxx

只打印 errCode 和条数,绝不输出 Cookie。判定:
- 试过的上下文全 -2041 → 先按账号级限权处理,漏推走"公众号后台身份 + WeRSS"或 dajiala 充值;
- 某一种能出文章 → 把 `app/services/weread_client.py` 的 `_headers` 按那个改,免费全量列表当场复活。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")   # 结论里带 → ,GBK 控制台会 UnicodeEncodeError

import requests  # noqa: E402

from app.db.database import get_session_local  # noqa: E402
from app.services import wechat_monitor  # noqa: E402
from app.services.weread_client import BASE, _UA  # noqa: E402
from config.settings import get_settings  # noqa: E402


def _call(cookie: str, book_id: str, referer: str | None, offset: int = 0) -> str:
    """返回一句结论(不含任何凭据):errCode/errmap 或成功时的条数。"""
    headers = {"Cookie": cookie, "User-Agent": _UA,
               "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer is not None:
        headers["Origin"] = BASE
        headers["Referer"] = referer
    try:
        resp = requests.get(f"{BASE}/web/mp/articles",
                            params={"bookId": book_id, "offset": offset, "count": 20},
                            timeout=15, headers=headers)
    except requests.RequestException as exc:
        return f"请求失败:{type(exc).__name__}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return "响应非 JSON(大概率被踢到验证页)"
    code = payload.get("errCode", payload.get("errcode", 0))
    if code:
        return f"errCode={code} errmsg={payload.get('errmsg') or payload.get('errlog') or ''}"
    reviews = payload.get("reviews") or []
    subs = sum(len(r.get("subReviews") or []) for r in reviews)
    return f"OK:群发 {len(reviews)} 组 / 展开 {subs} 篇"


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 mp/articles 列表接口在哪种上下文下可用")
    parser.add_argument("--user", type=int, default=1, help="租户 id(取其微信读书 Cookie)")
    parser.add_argument("--book-id", default="", help="要试的 weread_book_id(默认取该租户第一个)")
    parser.add_argument("--reader-url", default="",
                        help="该号在微信读书的阅读器页完整 URL(浏览器打开后复制);"
                             "不传就只探首页 Referer 与无 Referer 两种上下文")
    args = parser.parse_args()

    settings = get_settings()
    db = get_session_local()()
    try:
        from sqlalchemy import select

        from app.db.models import WechatBenchmark

        book_id = args.book_id
        if not book_id:
            row = db.scalar(select(WechatBenchmark).where(
                WechatBenchmark.user_id == args.user,
                WechatBenchmark.weread_book_id != "").order_by(WechatBenchmark.id))
            if row is None:
                print("该租户没有带 weread_book_id 的对标号,用 --book-id 指定")
                return 1
            book_id, nickname = row.weread_book_id, row.nickname
        else:
            nickname = "(命令行指定)"
        cookie = wechat_monitor._weread_cookie(db, args.user, settings)
        if not cookie:
            print("取不到微信读书 Cookie(weread):线上没配就先别探")
            return 1
    finally:
        db.close()

    print(f"book={book_id} 号={nickname}")
    # 阅读器页上下文只能由操作者提供:shelf() 只回 {book_id, name},拿不到 deepLink,
    # 而我们也没有从 MP_WXS_* 换算阅读器 id 的可靠办法——去微信读书网页打开这个号,复制地址栏。
    variants = [("首页 Referer(现在的写法)", f"{BASE}/"),
                ("不带 Origin/Referer", None)]
    if args.reader_url:
        variants.append((f"阅读器页上下文 {args.reader_url[:48]}", args.reader_url))
    else:
        print("  (未带 --reader-url:没探阅读器页上下文,而 GitHub 说可用的正是这一种——"
              "浏览器打开该号的微信读书阅读页,把完整 URL 传进来再跑一次)")
    results = {}
    for label, referer in variants:
        out = _call(cookie, book_id, referer)
        print(f"  [{label}] → {out}")
        results[label] = out
    # offset 翻页只在至少一种上下文可用时才有意义(全量补采靠它)
    ok = next((k for k, v in results.items() if v.startswith("OK")), None)
    if ok:
        print(f"  翻页试探({ok} offset=20)→ {_call(cookie, book_id, dict(variants)[ok], offset=20)}")
        print("结论:列表接口可用 → 把 weread_client._headers 改成上面那条上下文,免费全量列表即可复活")
    else:
        tried = "、".join(k for k, _ in variants)
        extra = "" if args.reader_url else "(还没探阅读器页,这一轮不能定论为账号级)"
        print(f"结论:已试的 {len(variants)} 种上下文({tried})都失败{extra};"
              f"若阅读器页也试过,就按账号级限权处理,根治走 WeRSS 公众号后台身份或 dajiala 充值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
