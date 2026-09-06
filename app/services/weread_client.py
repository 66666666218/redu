"""微信读书(WeRead)免费数据源:公众号最新文章 + 正文(见 doc/dev.md §5.8b)。

生产端点(经 we-mp-rss / weread-mp-fetcher 项目实测验证,2026-09):
- 书架:  GET /web/shelf/sync?userVid=&synckey=0&lectureSynckey=0  → 订阅的公众号(MP_WXS_* bookId)
          ⚠️ userVid 必须传**空字符串**,非空会 -2012「登录超时」
- 最新一篇: GET /api/mp/cover?bookId=MP_WXS_XXX  → {name,title,pic,reviewId,digest}
          旧列表接口 /web/mp/articles 已废弃(恒 -2041),新版只能拿"最新一篇"
- 正文:  GET /web/mp/content?reviewId=MP_WXS_...  → HTML(#js_content)
- reviewId 形如 `MP_WXS_<bookId>_<articleToken>`,末段即 mp.weixin.qq.com/s/ 原文短链
  的 token(token 可能含 `~`,必须原样保留,见 build_mp_url)

鉴权:仅靠 Cookie(完整微信读书登录 Cookie);x-wr-ticket 已弃用。
错误:-2012/-2010 登录失效(→ WereadAuthError,需重新扫码);-2041 接口废弃/被拦截。
限频:内置 2s 串行间隔(社区实测单日 30+ 次密集请求即触发风控,宁慢勿封)。
"""
from __future__ import annotations

import html as html_mod
import re
import threading
import time
from urllib.parse import quote

import requests

from app.utils import get_logger

logger = get_logger(__name__)

BASE = "https://weread.qq.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
_AUTH_CODES = (-2012, -2010)


class WereadError(Exception):
    """微信读书请求失败(带 errCode/errmsg)。"""


class WereadAuthError(WereadError):
    """登录态失效(-2012/-2010):Cookie 过期,需重新扫码。"""


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

    def __init__(self, cookie: str, timeout: int = 15, min_gap: float = 2.0) -> None:
        self.cookie = cookie.strip()
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()
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

    def _get(self, path: str, params: dict | None = None,
             accept: str = "application/json, text/plain, */*") -> dict:
        with self._lock:
            gap = time.time() - self._last
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last = time.time()
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
        code = int(payload.get("errCode", payload.get("errcode", 0)) or 0)
        if code in _AUTH_CODES:
            raise WereadAuthError(f"微信读书登录态失效({code}):{payload.get('errmsg') or '请重新扫码'}")
        if code == -2041:
            raise WereadError(f"微信读书接口不可用/被拦截(-2041):{payload.get('errmsg') or ''}")
        if code not in (0,):
            raise WereadError(f"微信读书错误 code={code}:{payload.get('errmsg') or payload.get('errlog') or ''}")
        return payload

    # ---- 三个业务端点 ----
    def shelf(self) -> list[dict]:
        """书架(=微信读书内关注的公众号):[{book_id, name}, ...],只保留 MP_WXS_* 条目。"""
        # userVid 必须传空字符串(非空会 -2012,社区实测结论)
        data = self._get("/web/shelf/sync", {"userVid": "", "synckey": 0, "lectureSynckey": 0})
        books = []
        for item in (data.get("books") or []):
            book_id = str(item.get("bookId") or "")
            if not book_id.startswith("MP_WXS_"):
                continue
            name = str(item.get("title") or item.get("bookName") or item.get("name") or "").strip()
            books.append({"book_id": book_id, "name": name})
        return books

    def mp_cover(self, book_id: str) -> dict:
        """公众号最新一篇文章(新版唯一列表入口):{name,title,pic,reviewId,digest}。"""
        payload = self._get("/api/mp/cover", {"bookId": book_id})
        if not payload.get("reviewId"):
            raise WereadError("公众号暂无文章(cover 未返回 reviewId)")
        return payload

    def mp_content(self, review_id: str) -> str:
        """文章正文纯文本(微信读书转发的 HTML,#js_content 抽取)。失败返回空串。"""
        try:
            resp = requests.get(f"{BASE}/web/mp/content", params={"reviewId": review_id},
                                timeout=self.timeout,
                                headers=self._headers("text/html,application/xhtml+xml,*/*"))
        except requests.RequestException:
            return ""
        if resp.status_code != 200:
            return ""
        m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', resp.text or "", re.S)
        body = m.group(1) if m else (resp.text or "")
        body = re.sub(r"<[^>]+>", " ", body)
        body = html_mod.unescape(body)
        return re.sub(r"\s{2,}", " ", body).strip()[:100000]

    # ---- 便捷封装 ----
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
