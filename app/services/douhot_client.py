"""抖音热点宝(douhot.douyin.com)直连客户端 —— 数据访问层(见 doc/dev.md §5.9)。

**为什么不用浏览器**:实测(`scripts/probe_douhot_direct.py`、`scripts/probe_douhot_apis.py`)
榜单接口只校验登录 Cookie,不校验 `a_bogus`/`X-Bogus`/`_signature`/`msToken` ——
把这些查询参数全部剥掉仍返回真实数据,且改 `page_num`/`date_window` 数据随之变化
(说明是服务端实算,不是重放缓存)。故无需 Playwright 驱动无头 Chromium,requests 直连即可。

接口约定(实测):
- 响应封装 `{"code": 0, "data": {...}}`;Cookie 失效为 `{"code": 8, "data": "用户未登录"}`。
- 内容词 `page_size` 服务端硬顶 24(传更大也只回 24),要更多条目须翻页;
  搜索榜/视频榜/话题榜的 `page_size` 可直接放大到 50。

本模块只负责**取回原始条目列表**,字段解析归 `app/services/douhot.py`(业务层)。
"""
from __future__ import annotations

import json
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import Settings
from app.utils import get_logger, get_proxies

logger = get_logger(__name__)

BASE = "https://douhot.douyin.com"
TREND_PAGE = f"{BASE}/square/trend?active_tab=hotword_all"
HOTSPOT_PAGE = f"{BASE}/square/hotspot?active_tab=hotspot_all"

HOT_WORD_API = "/douhot/v1/dashboard/hot_word/query_list"
HOT_SEARCH_API = "/douhot/v1/dashboard/hot_search/query_list"
SUBSCRIBE_API = "/douhot/v1/dashboard/subscribe/query_list"
VIDEO_API = "/douhot/v1/material/video_billboard"
CHALLENGE_API = "/douhot/v1/material/challenge_billboard"

WORD_PAGE_SIZE = 24   # 内容词单页上限(服务端硬顶)
MAX_PAGES = 20        # 翻页上限,防止接口异常时死循环
AUTH_CODE = 8         # 未登录/Cookie 失效

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
    "Origin": BASE,
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8", "Microsoft Edge";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class DouhotError(Exception):
    """热点宝请求/解析失败。"""


class DouhotAuthError(DouhotError):
    """Cookie 失效或未登录(code=8),需用户重新配置 Cookie。"""


class DouhotClient:
    """热点宝榜单最小客户端:登录 Cookie + 浏览器头,不需要任何签名参数。"""

    def __init__(
        self,
        cookie: str,
        settings: Settings | None = None,
        timeout: float = 20.0,
        use_proxy: bool = False,
    ) -> None:
        """`use_proxy` 显式开启才走代理:热点宝直连即可,默认不经代理池,
        免得代理故障(如提取额度到期)把本可成功的采集拖垮。"""
        if not cookie or not cookie.strip():
            raise DouhotAuthError("未配置抖音(热点宝) Cookie")
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self.session.headers["Cookie"] = cookie.strip()
        # 只对网络抖动/5xx 退避重试;Cookie 失效是 HTTP 200 + code=8,不会被重试
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=0.8,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(("GET", "POST")),
            )
        )
        self.session.mount("https://", adapter)
        self.timeout = timeout
        self.proxies = get_proxies(settings) if (use_proxy and settings) else None

    # ---- 传输层 ----------------------------------------------------------

    def _call(self, method: str, path: str, referer: str, body: dict | None = None) -> dict:
        """发一次请求并拆封装,返回 `data` 字典(非字典时返回空字典)。"""
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body else None
        try:
            resp = self.session.request(
                method,
                f"{BASE}{path}",
                headers={"Referer": referer},
                data=payload,
                proxies=self.proxies,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            obj = resp.json()
        except requests.RequestException as exc:
            raise DouhotError(f"热点宝请求失败({path}):{exc}") from exc
        except ValueError as exc:  # 非 JSON:多半被风控页拦了
            raise DouhotError(f"热点宝响应非 JSON({path})") from exc

        code = obj.get("code", obj.get("status_code"))
        data = obj.get("data")
        if code == AUTH_CODE:
            raise DouhotAuthError(f"热点宝 Cookie 已失效:{data}")
        if code != 0:
            raise DouhotError(f"热点宝接口出错({path}):code={code} {str(data)[:80]}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _items(data: dict, key: str) -> list[dict]:
        """取出 data[key] 里的条目(字段可能为 null,如无订阅时的 subscribe_list)。"""
        items = data.get(key)
        if not isinstance(items, list):
            return []
        return [it for it in items if isinstance(it, dict)]

    @staticmethod
    def _paged(fetch_page: Callable[[int], list[dict]], limit: int) -> list[dict]:
        """逐页累积到 limit 条;某页为空(到底/异常)即停。"""
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            if len(out) >= limit:
                break
            batch = fetch_page(page)
            if not batch:
                break
            out.extend(batch)
        return out[:limit]

    # ---- 榜单接口 --------------------------------------------------------

    def hot_words(self, limit: int = WORD_PAGE_SIZE, date_window: int = 24, tab_type: int = 1) -> list[dict]:
        """内容词榜(飙升词);limit > 24 时自动翻页。"""
        return self._paged(
            lambda page: self._items(
                self._call(
                    "POST",
                    HOT_WORD_API,
                    TREND_PAGE,
                    {
                        "page_num": page,
                        "page_size": WORD_PAGE_SIZE,
                        "tab_type": tab_type,
                        "keyword": "",
                        "date_window": date_window,
                    },
                ),
                "word_list",
            ),
            limit,
        )

    def hot_search(self, limit: int = 20, date_window: int = 1, sub_type: int = 3001) -> list[dict]:
        """搜索榜(key_word + search_score)。"""
        data = self._call(
            "POST",
            HOT_SEARCH_API,
            TREND_PAGE,
            {"page_num": 1, "page_size": limit, "sub_type": sub_type, "date_window": date_window},
        )
        return self._items(data, "search_list")

    def video_billboard(self, limit: int = 20, date_window: int = 24, sub_type: int = 1001) -> list[dict]:
        """视频榜(item_title + play_cnt);服务端会过滤,实际条数常少于 limit。"""
        data = self._call(
            "POST",
            VIDEO_API,
            HOTSPOT_PAGE,
            {"sub_type": sub_type, "date_window": date_window, "page": 1, "page_size": limit, "tag_version": "v2"},
        )
        return self._items(data, "objs")

    def challenge_billboard(self, limit: int = 20, date_window: int = 24, sub_type: int = 2001) -> list[dict]:
        """话题榜(challenge_name + score)。"""
        data = self._call(
            "POST",
            CHALLENGE_API,
            HOTSPOT_PAGE,
            {"sub_type": sub_type, "date_window": date_window, "page": 1, "page_size": limit, "tag_version": "v2"},
        )
        return self._items(data, "objs")

    def subscribe(self) -> list[dict]:
        """我的订阅(无订阅时服务端返回 subscribe_list=null)。"""
        return self._items(self._call("GET", SUBSCRIBE_API, HOTSPOT_PAGE), "subscribe_list")
