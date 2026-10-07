# -*- coding: utf-8 -*-
"""微博:**纯协议**搜索(2026-10-07)。

## 为什么换掉 MediaCrawler
微博的内容搜索原来走 MediaCrawler(**要浏览器**)。而移动端有一个**公开且稳定**的接口:

    GET https://m.weibo.cn/api/container/getIndex
        ?containerid=100103type%3D1%26q%3D<关键词>&page_type=searchall&page=N

**不需要任何签名**(不像抖音的 `a_bogus`、小红书的 `x-s`),只要一枚登录 cookie(`SUB`)。
实测(2026-10-07,本机库里的 cookie):HTTP 200、16 张 card、**15 条带正文**,
内容正是我们要的那类(「用 kua克app」「kk链接」「作者合集 大合集」)。

⇒ 省掉一个浏览器:更快、更省内存、**没有风控暴露面**(不开浏览器就没有指纹问题)。

## 三条纪律(与 `xhs_page_source` 同一套)
1. **"没登录/被拦" 与 "真的没搜到" 必须分开**:`ok != 1` / `login` / 空 `data` ⇒
   **抛错**,绝不返回空列表 —— 否则日志会把它说成"微博没热度"(本仓反复栽的假阴性)。
2. **关键词是资源名**(与其余平台同源),搜不到就是没人推,不是故障。
3. **带退避**:逐词之间留间隔,别连发。
"""
from __future__ import annotations

import re
import time

from app.utils import get_logger

logger = get_logger(__name__)

API = "https://m.weibo.cn/api/container/getIndex"
#: 关键词搜索的 containerid(`type=1` 是综合;实测 `type=3` 只回 1 张卡、拿不到正文)
CONTAINER_SEARCH = "100103type=1&q={kw}"
#: 对标号主页的时间线(留档:以后想按号采可以用它)
CONTAINER_USER = "107603{uid}"

_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
_TAG_RE = re.compile(r"<[^>]+>")
#: 逐词之间的间隔(秒)——连发容易被送回空数据
_GAP = 2.0


class WeiboSearchError(Exception):
    """微博搜索硬失败(没登录 / 被拦 / 网络)。**必须冒泡**,不能当成"没搜到"。"""


def _cookie(session=None, user_id: int = 1, settings=None) -> str:
    """登录态:优先**加密库**,回落 `.env`(与 `tenant.run_weibo` 同一套口径)。"""
    if settings is None:
        from config.settings import get_settings
        settings = get_settings()
    ck = ""
    if session is not None:
        try:
            from app.services.cookie_store import get_cookie

            ck = (get_cookie(session, user_id, "weibo") or "").strip()
        except Exception:  # noqa: BLE001 - 读不到就当没有,下面还有 .env 兜底
            logger.debug("读微博 cookie 失败(回落 .env)", exc_info=True)
    return ck or (getattr(settings, "weibo_cookie", "") or "").strip()


def _text_of(mblog: dict) -> str:
    txt = _TAG_RE.sub("", str(mblog.get("text") or "")).strip()
    txt = re.sub(r"\s+", " ", txt)
    return txt


def search(keywords: list[str], settings=None, session=None, user_id: int = 1,
           pages: int = 1) -> list[dict]:
    """按关键词搜微博,返回 `[{uid, name, url, snippet, pan_link, keyword}]`。

    形状与 `mediacrawler_source.crawl` 一致(调用方 `resource_presence.probe` 直接用),
    于是两条路可以互换。
    """
    import requests

    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    ck = _cookie(session, user_id, settings)
    if not ck:
        raise WeiboSearchError("没有微博登录态 —— 没有它会搜出空结果,所以这里直接判失败。"
                               "修法:把登录后的 cookie(含 `SUB=`)存进 cookie 库或 `.env`")
    headers = {"User-Agent": _UA, "Referer": "https://m.weibo.cn/",
               "X-Requested-With": "XMLHttpRequest", "MWeibo-Pwa": "1",
               "Accept": "application/json, text/plain, */*", "Cookie": ck}
    rows: list[dict] = []
    for i, kw in enumerate(kws):
        if i:
            time.sleep(_GAP)
        for page in range(1, max(1, pages) + 1):
            try:
                r = requests.get(API, params={"containerid": CONTAINER_SEARCH.format(kw=kw),
                                              "page_type": "searchall", "page": page},
                                 headers=headers, timeout=20)
            except Exception as exc:  # noqa: BLE001
                raise WeiboSearchError(f"请求失败:{type(exc).__name__}: {str(exc)[:80]}") from exc
            if r.status_code != 200:
                raise WeiboSearchError(f"HTTP {r.status_code} —— 多半是登录态失效或被限")
            try:
                d = r.json()
            except ValueError as exc:
                # ⚠️ 返回非 JSON 通常是**被挡了**(风控页),不是"没搜到"
                raise WeiboSearchError(f"响应不是 JSON(可能被挡):{r.text[:80]}") from exc
            if d.get("ok") != 1:
                raise WeiboSearchError(f"接口返回 ok={d.get('ok')} —— 登录态或风控问题")
            cards = ((d.get("data") or {}).get("cards") or [])
            n_before = len(rows)
            for c in cards:
                for g in (c.get("card_group") or [c]):
                    m = g.get("mblog") or {}
                    txt = _text_of(m)
                    if not txt:
                        continue
                    who = ((m.get("user") or {}).get("screen_name") or "")
                    hot = int(m.get("reposts_count") or 0)
                    rows.append({"uid": str((m.get("user") or {}).get("id") or ""),
                                 "name": who[:60], "url": "",
                                 # 转发数是"这条有多少人在传"的代理 —— 与其它平台一致
                                 "snippet": f"{txt[:200]}(转{hot})" if hot else txt[:200],
                                 "pan_link": "", "keyword": kw})
            logger.info("微博纯协议:「%s」第 %d 页 +%d 条", kw[:24], page, len(rows) - n_before)
            if not cards:
                break                      # 没卡片 = 翻到底了
    return rows
