"""微信读书"读书平台"客户端(wewe-rss v2 兼容,免费全量文章列表)。

xg.djxx.club 同款架构:后端不直连 weread.qq.com,而是调用一个 wewe-rss v2 实例
(开源、可 Docker 私有部署;平台内部持有微信读书会话池)。合同(经其源码校准):

- 认证: `Authorization: Bearer {token}` + `X-Weread-Token: {token}` + `xid: {vid}`
  (token 为含 vid 的 JWT,三段式)
- 文章链接 → 公众号解析: POST /api/v2/platform/wxs2mp  {"url": ...}
  → {mp_id | id | biz, name/nickname, article_title}
- 公众号文章列表(分页): GET /api/v2/platform/mps/{mp_id}/articles?page=&limit=
  → {items|articles|list|data: [{id/url/title/summary/date_published...}]}
- 扫码绑定: GET /api/v2/login/platform → {uuid, scanUrl};GET /api/v2/login/platform/{uuid}
  → {message, vid, token, username}(长轮询)

上游错误以 message 文本区分:WeReadError401=账号失效 / 429=请求频繁(当日小黑屋)/ 400=参数。
"""
from __future__ import annotations

import threading
import time

import requests

from app.utils import get_logger

logger = get_logger(__name__)


class PlatformError(Exception):
    """读书平台请求失败。"""


class PlatformAuthError(PlatformError):
    """平台凭据无效/上游微信读书账号失效(WeReadError401)。"""


def _extract_items(data: object) -> list[dict]:
    """防御式取列表:items/articles/list 或 data 嵌套。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "articles", "list"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    nested = data.get("data")
    if nested is not data:
        return _extract_items(nested)
    return []


class ReaderPlatformClient:
    """读书平台最小客户端;`_request` 可注入供测试。"""

    def __init__(self, base_url: str, token: str, vid: str = "", timeout: int = 30,
                 min_gap: float = 1.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.vid = str(vid or "").strip()
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()
        self._min_gap = min_gap

    # ---- 传输 ----
    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.vid:
            headers["xid"] = self.vid
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Weread-Token"] = self.token
        return headers

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None) -> dict:
        if not self.base_url:
            raise PlatformError("读书平台地址未配置")
        with self._lock:
            gap = time.time() - self._last
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last = time.time()
        try:
            resp = requests.request(method, self.base_url + path, params=params,
                                    json=json_body, timeout=self.timeout, headers=self._headers())
        except requests.RequestException as exc:
            raise PlatformError(f"读书平台请求失败:{exc}") from exc
        if resp.status_code in (401, 403):
            raise PlatformAuthError(f"读书平台认证失败 HTTP {resp.status_code}(token/vid 无效?)")
        if resp.status_code >= 400:
            message = resp.text[:200]
            if "WeReadError401" in message:
                raise PlatformAuthError(f"上游微信读书账号失效:{message}")
            raise PlatformError(f"读书平台返回 HTTP {resp.status_code}:{message}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PlatformError("读书平台响应非 JSON") from exc
        # 上游业务错误可能藏在 200 的 message 里
        message = str((payload or {}).get("message") or "")
        if "WeReadError401" in message:
            raise PlatformAuthError(f"上游微信读书账号失效:{message}")
        return payload if isinstance(payload, dict) else {"data": payload}

    # ---- 业务端点 ----
    def resolve_mp(self, article_url: str) -> dict:
        """文章链接 → {mp_id(biz), name, article_title}。"""
        obj = self._request("POST", "/api/v2/platform/wxs2mp", json_body={"url": article_url})
        payload = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        if isinstance(payload, list):
            payload = next((x for x in payload if isinstance(x, dict)), {})
        mp_id = str((payload or {}).get("mp_id") or (payload or {}).get("id") or (payload or {}).get("biz") or "").strip()
        if not mp_id:
            raise PlatformError("读书平台未返回公众号 ID(biz)")
        return {
            "mp_id": mp_id,
            "name": str((payload or {}).get("name") or (payload or {}).get("nickname") or "").strip(),
            "article_title": str((payload or {}).get("article_title") or "").strip(),
        }

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list[dict]:
        """公众号文章列表(分页,免费全量)。返回归一化条目 [{title,url,summary,publish_at_raw,id}]。"""
        obj = self._request("GET", f"/api/v2/platform/mps/{mp_id}/articles",
                            params={"page": str(page), "limit": str(limit)})
        items = []
        for raw in _extract_items(obj):
            url = str(raw.get("url") or raw.get("mpUrl") or raw.get("articleUrl") or raw.get("link") or "").strip()
            source_id = str(raw.get("id") or raw.get("articleId") or raw.get("itemId") or "").strip()
            if not url and source_id:
                url = f"https://mp.weixin.qq.com/s/{source_id}"
            if not url:
                continue
            items.append({
                "id": source_id or url,
                "title": str(raw.get("title") or raw.get("name") or "未命名文章").strip(),
                "url": url,
                "summary": str(raw.get("summary") or raw.get("digest") or raw.get("desc") or "").strip(),
                "publish_at_raw": raw.get("date_published") or raw.get("publishTime") or raw.get("createTime"),
            })
        return items

    # ---- 扫码绑定(可选:对接 wewe-rss 登录) ----
    def login_url(self) -> dict:
        obj = self._request("GET", "/api/v2/login/platform")
        payload = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        return {"uuid": str((payload or {}).get("uuid") or ""),
                "scan_url": str((payload or {}).get("scanUrl") or (payload or {}).get("scan_url") or "")}

    def login_result(self, uuid: str) -> dict:
        obj = self._request("GET", f"/api/v2/login/platform/{uuid}")
        payload = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        return payload or {}
