"""分辨微信读书 `/web/mp/articles` 的 -2041 到底是"账号被限权"还是"调用上下文不对"。

为什么要它:同日多篇漏推的唯一免费解就是这条列表接口。本仓库 2026-09-18 的实测结论是
"账号级永久限权(换任意新 Cookie 依旧 -2041)"(`doc/operations.md` §9.2),但当时**所有**
测试都用同一套请求头(首页 `Referer: https://weread.qq.com/`)——换 Cookie 不换上下文,
证不了是账号问题。GitHub 上有项目指出同一接口在阅读器页上下文里正常、在首页上下文里必返
-2041(`Pengyf04/weread-mp-fetcher` 的 HOW-IT-WORKS),也有项目纯 requests + 首页 Referer
就能列(`rachelos/we-mp-rss` PR#462)。所以这条判断只能在**线上那枚活 Cookie**上分辨。

用法(在**有活 Cookie 的机器**上跑,零副作用:只发 3~4 个只读 GET,不写库、不换 Cookie、不打轮换接口):

    python scripts/probe_weread_list.py --user 1 [--book-id MP_WXS_xxx]

三种上下文依次是:首页 Referer(现在的写法)/ 不带 Origin+Referer / 阅读器页
(`https://weread.qq.com/web/mp/reader/<weread_book_id>`,自动拼,`--reader-url` 可换一种形态试)。
只打印 errCode 和条数,绝不输出 Cookie。判定:
- 三种上下文全失败 → 确实是账号级限权,漏推只能走付费(dajiala)或现成的公众号后台身份;
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
from app.services.weread_client import BASE, _AUTH_CODES, _UA  # noqa: E402
from config.settings import get_settings  # noqa: E402


# 会话/登录态错误码:这些说明"根本没走到上下文判定",探测结果不成立,不能拿来下结论。
# -2012/-2010 是本仓库既有的 WereadAuthError 口径,-2013 是按游客处理(缺 cookie jar)。
_SESSION_CODES = (*_AUTH_CODES, -2013)


def _call(cookie: str, book_id: str, referer: str | None,
          offset: int = 0) -> tuple[str, int | None]:
    """返回 (一句结论, errCode 或 None)。结论里不含任何凭据。"""
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
        return f"请求失败:{type(exc).__name__}", None
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}", None
    try:
        payload = resp.json()
    except ValueError:
        return "响应非 JSON(大概率被踢到验证页)", None
    code = payload.get("errCode", payload.get("errcode", 0))
    if code:
        code = int(code)
        return (f"errCode={code} errmsg={payload.get('errmsg') or payload.get('errlog') or ''}",
                code)
    reviews = payload.get("reviews") or []
    subs = sum(len(r.get("subReviews") or []) for r in reviews)
    return f"OK:群发 {len(reviews)} 组 / 展开 {subs} 篇", None


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 mp/articles 列表接口在哪种上下文下可用")
    parser.add_argument("--user", type=int, default=1, help="租户 id(取其微信读书 Cookie)")
    parser.add_argument("--book-id", default="", help="要试的 weread_book_id(默认取该租户第一个)")
    parser.add_argument("--reader-url", default="",
                        help="覆盖阅读器页 Referer(默认按 weread_book_id 自动拼 "
                             "https://weread.qq.com/web/mp/reader/<bookId>)")
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
    # 阅读器页 URL 可由 weread_book_id 直接拼出(实测 /web/mp/reader/MP_WXS_xxx 是回 200 的 SPA 壳),
    # 所以不需要人去浏览器里复制;--reader-url 只用于试别的上下文形态。
    reader = args.reader_url or f"{BASE}/web/mp/reader/{book_id}"
    variants = [("首页 Referer(现在的写法)", f"{BASE}/"),
                ("不带 Origin/Referer", None),
                (f"阅读器页 {reader[len(BASE):][:44]}", reader)]
    results: dict[str, str] = {}
    codes: list[int | None] = []
    for label, referer in variants:
        out, code = _call(cookie, book_id, referer)
        print(f"  [{label}] → {out}")
        results[label] = out
        codes.append(code)
    # offset 翻页只在至少一种上下文可用时才有意义(全量补采靠它)
    ok = next((k for k, v in results.items() if v.startswith("OK")), None)
    if ok:
        deeper, _ = _call(cookie, book_id, dict(variants)[ok], offset=20)
        print(f"  翻页试探({ok} offset=20)→ {deeper}")
        print("结论:列表接口可用 → 把 weread_client._headers 改成上面那条上下文,免费全量列表即可复活")
        return 0
    if all(c in _SESSION_CODES for c in codes):
        # 服务端在鉴权阶段就把会话打回,压根没到"哪种上下文"那一步——这次探测没有结论可言
        print(f"结论:本轮**不作数**(全部回的是登录态错误码 {sorted(set(codes))}=-2012/-2010/-2013,"
              "服务端在鉴权阶段就拒了,与 Referer 无关)。"
              "要分辨 -2041 的成因,必须在**微信读书 Cookie 还活着**的机器上跑;"
              "线上 Cookie 一失效(本机就是这种),这条探测只能等续期后再做。")
        return 2
    print("结论:三种上下文(首页/无 Referer/阅读器页)都拿不到列表 → 按账号级限权处理,"
          "免费全量列表这条路到此为止,根治只能换凭据(dajiala 付费,或有现成公众号身份走 WeRSS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
