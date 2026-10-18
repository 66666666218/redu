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

## 失败长什么样(2026-10-13 实测,别再靠猜)
| 情形 | 现象 |
|---|---|
| **cookie 已失效** | `HTTP 200` + `{"ok": -100, "url": "https://passport.weibo.com/sso/signin?…"}` |
| **完全不带 cookie** | `HTTP 432`(空体) |
| 登录态是否有效(权威判据) | `GET https://m.weibo.cn/api/config` → `data.login`(true/false) |

⇒ `ok=-100` **要靠响应里的 `url` 才认得出**是"登录态失效"还是别的;所以本模块
**不再写「登录态或风控问题」这种并列话**(那会让告警只能靠关键词猜方向)。

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
from app.utils.timeutil import to_dt

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
    """微博搜索硬失败(没登录 / 被拦 / 网络)。**必须冒泡**,不能当成"没搜到"。

    ★ `kind` 是给**上层告警**用的**结构化判据**(与 `tieba_search.TiebaSearchError.kind` 同一套)。
    ⚠️ 为什么非要它(2026-10-13 的真实事故):告警原来靠**关键词**判"要不要叫人去重登" ——
    只要错误串里出现"登录"两个字就给扫码建议。而旧文案写的是「登录态**或**风控问题」,
    恰好含"登录"⇒ 一条**措辞含糊**的错误把人引向了一个**错的修法**(微博不走 MediaCrawler)。
    ⇒ 判据换成枚举值:**别再让文案措辞决定告警说什么**。
      取值:`login_expired`(要人去换凭据) / `risk`(被挡,该等) / `network`(抖动,重试即可)。
    """

    def __init__(self, message: str = "", kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


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
                               "修法:把登录后的 cookie(含 `SUB=`)存进 cookie 库或 `.env`",
                               kind="login_expired")
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
                raise WeiboSearchError(f"请求失败:{type(exc).__name__}: {str(exc)[:80]}",
                                       kind="network") from exc
            if r.status_code == 432:
                # 实测:完全不带 cookie 时回 432(空体)—— 是"没登录/被挡",不是"没热度"
                raise WeiboSearchError("HTTP 432(未登录或被挡)", kind="login_expired")
            if r.status_code != 200:
                raise WeiboSearchError(f"HTTP {r.status_code} —— 多半是被限",
                                       kind="risk")
            try:
                d = r.json()
            except ValueError as exc:
                # ⚠️ 返回非 JSON 通常是**被挡了**(风控页),不是"没搜到"
                raise WeiboSearchError(f"响应不是 JSON(可能被挡):{r.text[:80]}",
                                       kind="risk") from exc
            if d.get("ok") != 1:
                # ★★★ **把"登录态失效"与"风控"分开,并给出证据**(2026-10-13 实测)。
                # `ok=-100` 的响应体里带一个 `url` 字段,实测它的值就是
                # `https://passport.weibo.com/sso/signin?...` —— 即**接口把我们重定向到登录页**
                # ⇒ 这是**登录态失效**,和"被风控"(HTTP 432 / 非 JSON 风控页)不是一回事,
                # 处置也不同:前者要人去重登,后者该等。
                # ⚠️ 旧文案是「登录态**或**风控问题」,两件事并列 ⇒ 既没给证据、
                #    又让告警只能靠"串里有没有『登录』两个字"去猜(见 `resource_presence`)。
                #    今天这条告警就是这么来的:猜对了方向、却给出了错的修法。
                url = str(d.get("url") or "")
                if "passport.weibo.com" in url or "signin" in url:
                    raise WeiboSearchError(
                        "微博**登录态已失效** —— 接口把我们重定向到登录页"
                        f"({url[:70]})。修法:重新登录 `https://m.weibo.cn` 后更新"
                        "**我们 cookie 库**里 weibo 那一份(一键:`python tools/weibo_export_cookie.py`)",
                        kind="login_expired")
                msg = str(d.get("msg") or "").strip()
                raise WeiboSearchError(
                    f"接口返回 ok={d.get('ok')} —— **疑似风控**(响应里没有跳转到登录页)"
                    + (f":{msg[:60]}" if msg else ""), kind="risk")
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
                    uid = str((m.get("user") or {}).get("id") or "")
                    # ★★ **原帖链接必须给出来**(2026-10-16 用户报「对标作品 显示
                    #   `—(源未给链接)`」)。原来这里**写死 `url: ""`** —— 而帖子 id 就在手上。
                    #   卡片的「对标作品」列靠它可点,空串就等于**那一列永远点不动**。
                    #   优先 `mblogid`(微博的标准短 id,组成 `weibo.com/<uid>/<mblogid>`);
                    #   拿不到就退到 `m.weibo.cn/detail/<mid>`(数字 id 的移动端详情页,一样能开)。
                    mblogid = str(m.get("mblogid") or "")
                    mid = str(m.get("mid") or m.get("id") or "")
                    post_url = (f"https://weibo.com/{uid}/{mblogid}" if (uid and mblogid)
                                else (f"https://m.weibo.cn/detail/{mid}" if mid else ""))
                    # ★ **互动数单独带出来**(用户口径:5 要素里要有「对标数据」)。
                    #   ⚠️ 实测**上游大多给 0**(「网盘资源」10 条全 0;「夸克网盘」9 条里只有 3 条有)
                    #   —— 那是上游的事,不是我们没接;为 0 就**不放进 `metrics`**
                    #   (与 `cross_accounts._weibo_metrics` 同一条口径:填 0 会被读成"没人看")。
                    metrics = {k: v for k, v in (("share_count", hot),
                                                 ("comment_count", int(m.get("comments_count") or 0)),
                                                 ("liked_count", int(m.get("attitudes_count") or 0)))
                               if v > 0}
                    #: ★ **发帖时间**(2026-10-18):发现链的「只搬新的」判据要它
                    #: (`discovered_pan_links.publish_at`)—— 接口的 `mblog.created_at` 一直都在
                    #: (`Sun Oct 18 10:51:12 +0800 2026`),而这里**从来没带出来**
                    #: ⇒ 微博那条链的发布时间 **42% 为空**、闸门形同虚设。
                    #: ⚠️ 解析走单一出处 `to_dt`(它认得 epoch / ISO / 微博这种 RFC 风格)。
                    _pub = to_dt(m.get("created_at"))
                    rows.append({"uid": uid,
                                 "name": who[:60], "url": post_url,
                                 # 转发数是"这条有多少人在传"的代理 —— 与其它平台一致
                                 "snippet": f"{txt[:200]}(转{hot})" if hot else txt[:200],
                                 "pan_link": "", "metrics": metrics, "keyword": kw,
                                 "publish_at": int(_pub.timestamp()) if _pub else 0})
            logger.info("微博纯协议:「%s」第 %d 页 +%d 条", kw[:24], page, len(rows) - n_before)
            if not cards:
                break                      # 没卡片 = 翻到底了
    return rows
