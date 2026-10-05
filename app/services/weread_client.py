"""微信读书(WeRead)免费数据源:公众号最新文章 + 正文(见 doc/dev.md §5.8b)。

生产端点(经 we-mp-rss / weread-mp-fetcher 项目实测验证,2026-09):
- 书架:  GET /web/shelf/sync?userVid=&synckey=0&lectureSynckey=0  → 订阅的公众号(MP_WXS_* bookId)
          ⚠️ userVid 必须传**空字符串**,非空会 -2012「登录超时」
- 最新一篇: GET /api/mp/cover?bookId=MP_WXS_XXX  → {name,title,pic,reviewId,digest}
          ⚠️ 返回体里**没有 readNum**(只有 avatar/name/title/pic/reviewId/template/coverBoxInfo)
- 列表:  GET /web/mp/articles → 我们的会话恒 -2041(2026-10-04 实测)
          旧注释写"仅在会话续期后初期可用" —— **那条是错的**:续期 success+verified
          之后**立刻**再拉,书架前 5 个号仍全部 -2041。因为**续期 = 同会话换 skey,
          不是新会话**,它开不出窗口。
          ⚠️ 但**别据此断言"永久关死"**:本仓 09-27 另见过 -2014(新建立的会话),
          两种定性尚未定案(见 doc/operations.md §9.2)。故调用方按"只取 cover 最新一篇"
          处理(见 wechat_monitor);**别再为它编排续期时机**(见 doc/外部接口速查.md §3.2)
- 正文:  GET /web/mp/content?reviewId=MP_WXS_...  → HTML(#js_content)
- reviewId 形如 `MP_WXS_<bookId>_<articleToken>`,末段即 mp.weixin.qq.com/s/ 原文短链
  的 token(token 可能含 `~`,必须原样保留,见 build_mp_url)

鉴权:仅靠 Cookie(完整微信读书登录 Cookie);x-wr-ticket 已弃用。
错误:-2012/-2010 登录失效(→ WereadAuthError;可用 refresh_skey 用 wr_rt 续期);-2041 接口废弃/被拦截;
      -2014 请求频率过高(限流)。⚠️ cover 把错误码包在 HTTP 499 的 `data.errcode` 里而非顶层
      `errCode`,顶层-only 的判定会把限流当成"成功但无文章",整轮静默 new=0(2026-09-22 实测)。
限频:内置 2s 串行间隔(社区实测单日 30+ 次密集请求即触发风控,宁慢勿封)。

Cookie 续期(2026-09 实测):wr_skey 短效且**轮换制**——调 /web/login/renewal 用长效
wr_rt 换新 wr_skey 后,旧 skey 很快失效;因此续期成功后必须把新 Cookie 回写存储
(见 wechat_monitor.refresh_weread_cookie),且 renewal 请求必须把登录 Cookie 注入
requests.Session 的 cookie jar(domain=weread.qq.com),否则服务端按游客处理返回 -2013。
"""
from __future__ import annotations
from app.utils.ua import CHROME_WINDOWS  # 统一 UA(见该模块注释)

import json
import re
import threading
import time
from urllib.parse import quote

import requests

from app.utils import get_logger

logger = get_logger(__name__)

BASE = "https://weread.qq.com"
_UA = CHROME_WINDOWS
_AUTH_CODES = (-2012, -2010)


class WereadError(Exception):
    """微信读书请求失败(带 errCode/errmsg)。"""


class WereadAuthError(WereadError):
    """登录态失效(-2012/-2010):Cookie 过期,需重新扫码。"""


def to_count(value) -> int:
    """流量字段容错:上游偶尔回 "1.2万"/None/空串,裸 int() 会 ValueError 打穿整轮。"""
    s = str(value if value is not None else "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        pass
    m = re.match(r"^([\d.]+)\s*([万亿])", s)
    if m:
        try:
            return int(float(m.group(1)) * (10000 if m.group(2) == "万" else 100000000))
        except ValueError:
            return 0
    return 0


def build_mp_url(original_id: str) -> str:
    """公众号原文短链;token 可能含 `~`(合法字符,quote 须原样保留)。"""
    token = str(original_id or "").strip()
    if not token:
        return ""
    return f"https://mp.weixin.qq.com/s/{quote(token, safe='~')}"


def review_to_url(review_id: str, book_id: str = "") -> str:
    """reviewId(`MP_WXS_<bookId>_<token>`)→ mp.weixin 原文直链。"""
    review_id = str(review_id or "").strip()
    if not review_id:
        return ""
    token = review_id
    if book_id and review_id.startswith(f"{book_id}_"):
        token = review_id[len(book_id) + 1:]
    elif "_" in token:
        token = token.rsplit("_", 1)[-1]
    return build_mp_url(token)


class WereadClient:
    """最小客户端:Cookie 鉴权 + 2s 限速;`_get` 可注入供测试。"""

    # 限速状态放在**类级**:单进程部署(uvicorn 无 --workers + APScheduler 同进程)里
    # 调用方大量临时 `WereadClient(cookie)` 新建实例(见 wechat_monitor),若 `_last/_lock`
    # 是实例级,不同实例各自计时 → 最小间隔形同虚设,突发请求打爆微信读书触发风控。
    # 类级共享后,进程内所有实例串行于同一起跑线。跨进程需 DB 令牌桶,当前无此部署。
    _throttle_lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, cookie: str, timeout: int = 15, min_gap: float = 2.0) -> None:
        self.cookie = cookie.strip()
        self.timeout = timeout
        self._min_gap = min_gap

    def _headers(self, accept: str = "application/json, text/plain, */*") -> dict:
        return {
            "Cookie": self.cookie,
            "User-Agent": _UA,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/",
        }

    def _throttle(self) -> None:
        """类级 2s 限速:所有实例共享同一起跑线。"""
        cls = type(self)
        with cls._throttle_lock:
            gap = time.time() - cls._last_request
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            cls._last_request = time.time()

    def _get(self, path: str, params: dict | None = None,
             accept: str = "application/json, text/plain, */*") -> dict:
        self._throttle()
        try:
            resp = requests.get(f"{BASE}{path}", params=params, timeout=self.timeout,
                                headers=self._headers(accept))
        except requests.RequestException as exc:
            raise WereadError(f"微信读书请求失败:{exc}") from exc
        if resp.status_code in (401, 403):
            raise WereadAuthError(f"微信读书认证失败 HTTP {resp.status_code}(Cookie 过期?)")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WereadError(f"微信读书响应非 JSON(HTTP {resp.status_code})") from exc
        err = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = int(payload.get("errCode", payload.get("errcode", 0)) or 0) or int(
            err.get("errcode", err.get("errCode", 0)) or 0)
        msg = payload.get("errmsg") or payload.get("errlog") or err.get("errmsg") or ""
        if code in _AUTH_CODES:
            raise WereadAuthError(f"微信读书登录态失效({code}):{msg or '请重新扫码'}")
        if code == -2041:
            raise WereadError(f"微信读书接口不可用/被拦截(-2041):{msg}")
        if code not in (0,):
            raise WereadError(f"微信读书错误 code={code}:{msg or payload.get('errlog') or ''}")
        return payload

    # ---- 三个业务端点 ----
    def shelf_entries(self) -> list[dict]:
        """书架原始条目(仅 MP_WXS_*):[{"bookId":..., "title":..., <服务端附带字段>}, ...]。

        与 shelf() 的区别:保留服务端原样字段(可能含 reviewId 类"最新文章"信号),
        供书架粗筛(wechat_monitor._shelf_gate_plan)逐字段探测。原始返回形状尚未在
        线上验证过,调用方必须容得下"字段不存在"——缺信号就退化为不筛,不得猜。
        """
        # userVid 必须传空字符串(非空会 -2012,社区实测结论)
        data = self._get("/web/shelf/sync", {"userVid": "", "synckey": 0, "lectureSynckey": 0})
        return [item for item in (data.get("books") or [])
                if str(item.get("bookId") or "").startswith("MP_WXS_")]

    def shelf(self) -> list[dict]:
        """书架(=微信读书内关注的公众号):[{book_id, name}, ...],只保留 MP_WXS_* 条目。"""
        books = []
        for item in self.shelf_entries():
            name = str(item.get("title") or item.get("bookName") or item.get("name") or "").strip()
            books.append({"book_id": item["bookId"], "name": name})
        return books

    def mp_cover(self, book_id: str) -> dict:
        """公众号最新一篇文章(新版唯一列表入口):{name,title,pic,reviewId,digest}。"""
        payload = self._get("/api/mp/cover", {"bookId": book_id})
        if not payload.get("reviewId"):
            raise WereadError("公众号暂无文章(cover 未返回 reviewId)")
        return payload

    def mp_content(self, review_id: str) -> str:
        """文章正文纯文本(微信读书转发的 HTML,#js_content 抽取),含正文超链接与「阅读原文」目标 URL。

        失败/拿不到正文容器返回空串(旧实现回落到"整页去标签",把十几 KB 的 JS 当正文入库,
        还把被风控伪装成"抓到了")。上限 20000 字符刻意压在 MySQL TEXT 列内。

        限速必须走 `_throttle`:这里曾直接 `requests.get` 绕过类级节流,
        而"同步文章"会对每篇新文章各调一次(上百次),等于把风控留给正文抓取环节。
        """
        self._throttle()
        try:
            resp = requests.get(f"{BASE}/web/mp/content", params={"reviewId": review_id},
                                timeout=self.timeout,
                                headers=self._headers("text/html,application/xhtml+xml,*/*"))
        except requests.RequestException:
            return ""
        if resp.status_code != 200:
            return ""
        from app.utils.html_text import article_body, html_to_text, original_link_url

        page = resp.text or ""
        body = article_body(page)
        if not body:  # 转发页偶有非 js_content 版式:退到"去脚本 + 保留链接"的整页文本
            body = html_to_text(page)
        orig = original_link_url(page)
        if orig and orig not in body:
            body = (body + " " + orig).strip()
        return body[:20000]

    def mp_articles(self, book_id: str, offset: int = 0, count: int = 20) -> dict:
        """公众号历史文章列表(含每篇 精确阅读/点赞)。

        返回原始 {reviews:[{createTime, subReviews:[{review:{mpInfo{title,originalId,
        readNum,likeNum}, reviewId, createTime}}]}], synckey,...},由调用方展平。
        """
        return self._get("/web/mp/articles", {"bookId": book_id, "offset": offset, "count": count})

    @staticmethod
    def flatten_mp_articles(payload: dict) -> list[dict]:
        """把 mp/articles 的 reviews→subReviews 展平成文章列表(同群发多篇文章不丢)。"""
        items: list[dict] = []
        for group in payload.get("reviews") or []:
            group_time = group.get("createTime")
            for sub in group.get("subReviews") or []:
                rev = sub.get("review") or {}
                mp = rev.get("mpInfo") or {}
                title = str(mp.get("title") or "").strip()
                if not title:
                    continue
                items.append({
                    "title": title,
                    "original_id": str(mp.get("originalId") or ""),
                    "review_id": str(rev.get("reviewId") or sub.get("reviewId") or ""),
                    "read_num": to_count(mp.get("readNum")),
                    "like_num": to_count(mp.get("likeNum")),
                    "create_time": to_count(rev.get("createTime") or group_time),
                })
        return items

    # ---- 便捷封装 ----
    def refresh_skey(self, timeout: int = 20) -> str | None:
        """用长效 wr_rt 调 /web/login/renewal 换新短效 wr_skey,返回更新后的完整 Cookie 串。

        Cookie 中无 wr_rt 或续期失败(网络/服务端拒绝/未下发新 skey)返回 None,
        此时只能重新扫码/复制 Cookie。调用方负责把返回的新 Cookie **回写存储**
        (旧 skey 会被轮换失效,不回写等于丢登录态)。

        ⚠️ 2026-09-28 实测:renewal 的 body 校验收紧——旧形态
        `{"rq": "%2Fweb%2Fshelf", "ql": true}` 被服务端以 **-2013 鉴权失败** 拒绝
        (rt 明明有效),改为社区(wxread)现行形态 `{"rq": "%2Fweb%2Fbook%2Fread", "ql": false}`
        即成功;三种形态轮试,成功判定 = **响应 Set-Cookie 里出现 wr_skey**
        (不看 body.succ,服务端成功时 succ 字段可能缺省)。
        """
        if "wr_rt=" not in self.cookie:
            return None
        # wr_rt 形态兼容(2026-09-30 事故修复):renewal 回写时 rt 会做 quote(),
        # 历史存储里可能存在"二次编码"形态(如 web%2540..,服务端要的是 web%40..),
        # 直接注入会被拒,造成"续期一次成功、之后永失败"。注入前先 unquote 还原。
        from urllib.parse import unquote as _unquote
        jar = requests.Session()
        jar.headers.update({
            "User-Agent": CHROME_WINDOWS,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/web/shelf",
            "Content-Type": "application/json;charset=UTF-8",
            "platform-id": "10",  # 微信读书网页版标识;缺失会导致续期后仍 -2012(实测)
            "Cache-Control": "no-cache",
        })
        # 关键:登录 Cookie 必须注入 cookie jar(domain=weread.qq.com),否则按游客处理 -2013
        for kv in self.cookie.split(";"):
            name, _, value = kv.strip().partition("=")
            if name:
                if name == "wr_rt":
                    value = _unquote(value)
                jar.cookies.set(name, value, domain="weread.qq.com", path="/")
        variants = ({"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
                    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
                    {"rq": "%2Fweb%2Fbook%2Fread"})
        resp = None
        for body in variants:
            try:
                resp = jar.post(f"{BASE}/web/login/renewal",
                                data=json.dumps(body, separators=(",", ":")), timeout=timeout)
            except requests.RequestException:
                return None
            if resp.status_code == 200 and "wr_skey" in resp.cookies:
                break  # 该形态成功;Set-Cookie 已并入 jar
            resp = None
        if resp is None:
            # 全部形态被拒:服务端接口/校验又变了(参考本函数 2026-09-28 的教训)
            logger.warning("renewal 三种形态均未换出新 skey(接口或校验可能又变了)")
            return None
        # 以续期响应的完整 Set-Cookie 为基础重建:服务端可能同时轮换多个字段,
        # 只拼 skey/rt 会得到"半新半旧"的不一致 Cookie(实测 shelf -2012)。
        # 旧 Cookie 中未被轮换的字段(设备/昵称等)原样保留。
        jar_map = {c.name: c.value for c in jar.cookies if c.name and c.value}
        parts: list[str] = []
        seen: set[str] = set()
        for kv in self.cookie.split(";"):
            name, _, value = kv.strip().partition("=")
            if not name:
                continue
            if name in jar_map:
                # safe 含 %:rt 服务端下发的就是 %XX 编码形态,quote 不得再动 %(%→%25 即二次编码事故)
                out_v = quote(jar_map[name], safe="~%") if name == "wr_rt" else jar_map[name]
                parts.append(f"{name}={out_v}")
                seen.add(name)
            else:
                parts.append(f"{name}={value}")
        for name, value in jar_map.items():
            if name not in seen:
                out_v = quote(value, safe="~%") if name == "wr_rt" else value
                parts.append(f"{name}={out_v}")
        return "; ".join(parts)

    def latest_article(self, book_id: str) -> dict | None:
        """{title, url, publish_at=None, content} 或 None(暂无文章)。"""
        try:
            cover = self.mp_cover(book_id)
        except WereadError as exc:
            if "暂无文章" in str(exc):
                return None
            raise
        review_id = str(cover.get("reviewId") or "")
        return {
            "title": str(cover.get("title") or "").strip(),
            "url": review_to_url(review_id, book_id),
            "review_id": review_id,
            "digest": str(cover.get("digest") or ""),
            "name": str(cover.get("name") or ""),
        }
