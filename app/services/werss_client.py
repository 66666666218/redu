"""WeRSS(开源 rachelos/we-mp-rss)客户端:自建微信公众号订阅源,给监听提供**免费全量文章列表**。

为什么需要它:此前文档里写的"根治办法是部署 wewe-rss"已经失效——wewe-rss 抓取靠的是一个公共
转发服务(`weread.111965.xyz`),该服务随 Deno Deploy Classic 于 2026-07-20 退役而消失
(实测 502 / `DEPLOYMENT_NOT_FOUND`),仓库本身也已归档。WeRSS 是同类且仍在维护的实现。

合同(逐行取自其 main 分支源码,2026-09-27 校准,版本 `core/ver.py`=1.5.3):

- 认证:请求头 `Authorization: AK-SK {access_key}:{secret_key}`(`core/auth.py` 的
  `get_current_user_or_ak`;AK 在管理界面「Access Key 管理」里创建,Secret 只显示一次)。
  不需要 session/JWT;AK 无效时它回落 JWT 校验,最终 401。
- 公众号列表:`GET /api/v1/wx/mps?limit(1..100)&offset&kw` → `data.list[] =
  {id, mp_name, mp_cover, mp_intro, status, created_at}`(`apis/mps.py`)。Feed.id 形如
  `MP_WXS_*`,是我们 `biz` 列该存的值。
- 文章列表:`GET /api/v1/wx/articles?mp_id&limit(1..100)&offset` → `data.list[] =
  {id, mp_id, mp_name, title, url, pic_url, description, publish_time, ...}`,
  **按 `publish_time` 降序**(`apis/article.py` 的 `order_by(Article.publish_time.desc())`),
  该视图不含正文(`ArticleBase`)。
- 手动刷新:`POST /api/v1/wx/mps/update/{mp_id}?start_page&end_page`,**同步**抓取,
  自带 60s 节流(过快返回业务码 40402)。
- 统一响应封装:`{"code": 0, "message": "success", "data": ...}`;`code != 0` 即业务失败。
  注意它有个别错误分支挂在 HTTP 201 上,所以判定成功与否**只看 `code`**,不看状态码。

与 `reader_platform_client.ReaderPlatformClient` 保持同一方法名与返回结构,`_platform_client`
可以按配置返回任意一家,调用方(监听 ⓪ 分支、同步、加号解析)不用改。
"""
from __future__ import annotations

import threading
import time

import requests

from app.services.reader_platform_client import PlatformAuthError, PlatformError
from app.utils import get_logger

logger = get_logger(__name__)

_API_BASE = "/api/v1/wx"


class WerssError(PlatformError):
    """WeRSS 请求失败(继承 PlatformError:调用方本来就按这个类型兜底降级)。"""


class WerssAuthError(WerssError, PlatformAuthError):
    """WeRSS 的 Access Key 无效/过期,或其上游账号(公众号后台/微信读书)授权失效。"""


class WerssClient:
    """WeRSS 最小客户端;`_request` 可注入供测试。"""

    def __init__(self, base_url: str, access_key: str, secret_key: str, timeout: int = 30,
                 min_gap: float = 1.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()
        self._min_gap = min_gap

    # ---- 传输 ----
    def _headers(self) -> dict:
        return {"Accept": "application/json",
                "Authorization": f"AK-SK {self.access_key}:{self.secret_key}"}

    def _request(self, method: str, path: str, *, params: dict | None = None) -> dict:
        if not self.base_url:
            raise WerssError("WeRSS 地址未配置")
        with self._lock:
            gap = time.time() - self._last
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last = time.time()
        try:
            resp = requests.request(method, self.base_url + _API_BASE + path, params=params,
                                    timeout=self.timeout, headers=self._headers())
        except requests.RequestException as exc:
            raise WerssError(f"WeRSS 请求失败:{exc}") from exc
        if resp.status_code in (401, 403):
            raise WerssAuthError(f"WeRSS 认证失败 HTTP {resp.status_code}(Access Key 无效或已过期?)")
        if resp.status_code >= 500:
            raise WerssError(f"WeRSS 返回 HTTP {resp.status_code}:{resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WerssError(f"WeRSS 响应非 JSON(HTTP {resp.status_code})") from exc
        if not isinstance(payload, dict):
            raise WerssError("WeRSS 响应结构异常(顶层不是对象)")
        code = payload.get("code")
        if code not in (0, None):
            message = str(payload.get("message") or "")
            # 上游账号(公众号后台/微信读书)授权失效只体现在文案里,没有稳定的业务码可判
            if any(k in message for k in ("授权", "登录", "token", "Token")):
                raise WerssAuthError(f"WeRSS 业务错误({code}):{message}")
            raise WerssError(f"WeRSS 业务错误({code}):{message}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {"list": data}

    # ---- 业务端点 ----
    def list_feeds(self, kw: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        """订阅列表 → [{id, mp_name}](id 即文章接口要用的 mp_id,也是 `biz` 列的值)。"""
        data = self._request("GET", "/mps", params={"limit": str(limit), "offset": str(offset),
                                                    "kw": kw})
        out = []
        for raw in data.get("list") or []:
            if isinstance(raw, dict) and raw.get("id"):
                out.append({"id": str(raw["id"]).strip(),
                            "mp_name": str(raw.get("mp_name") or "").strip()})
        return out

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list[dict]:
        """某号文章列表(发布时间降序)。归一化为 ReaderPlatformClient 的同一结构。"""
        limit = max(1, min(int(limit), 100))  # 上游 le=100,超了会整请求被拒
        page = max(1, int(page))
        data = self._request("GET", "/articles",
                             params={"mp_id": mp_id, "limit": str(limit),
                                     "offset": str((page - 1) * limit)})
        items = []
        for raw in data.get("list") or []:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not title or not url:
                continue  # 没有标题或链接的条目上不了卡片,也不会参与去重
            items.append({
                "id": str(raw.get("id") or url),
                "title": title,
                "url": url,
                "summary": str(raw.get("description") or raw.get("digest") or "").strip(),
                "publish_at_raw": raw.get("publish_time") or raw.get("create_time"),
            })
        return items

    def refresh_mp(self, mp_id: str, end_page: int = 1) -> bool:
        """触发 WeRSS 立刻去上游抓一次(同步接口,自带 60s 节流)。

        返回 False 表示"这次没刷成"(被节流/上游失败),调用方不应当失败处理——
        WeRSS 自己的定时任务迟早会补上。默认不在监听里调用:一次同步抓取会让
        WeRSS 逐页打上游,81 个号串起来就是给别人做DDoS了。
        """
        try:
            self._request("POST", f"/mps/update/{mp_id}", params={"start_page": "0",
                                                                  "end_page": str(end_page)})
            return True
        except WerssError as exc:
            logger.info("WeRSS 手动刷新未执行 mp=%s:%s", mp_id, exc)
            return False

    def resolve_mp(self, article_url: str) -> dict:
        """WeRSS 没有"文章链接 → 公众号"的解析接口,故不支持。

        它的订阅是在自己的管理界面里加的;我们这边用 `match_biz_from_werss` 按
        公众号名称把订阅 id 回填进 `biz` 列。保留这个方法是为了让 `add_benchmark`
        拿到一句能看懂的话,而不是 AttributeError。
        """
        raise WerssError("WeRSS 不支持按文章链接解析公众号,请在 WeRSS 后台添加订阅后"
                         "运行 scripts/werss_backfill_biz.py 按名称回填 biz")
