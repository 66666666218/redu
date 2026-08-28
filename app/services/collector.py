"""微博热搜采集(见 doc/dev.md §5.2)。

请求微博热搜 Ajax 接口,返回 `list[HotItem]`。
必须携带 Cookie,并经过代理与随机 UA;对外部调用做退避重试。
"""
from __future__ import annotations

import random
from datetime import datetime

import requests

from config.settings import Settings
from app.models import HotItem
from app.utils import get_logger, get_proxies, retry

logger = get_logger(__name__)

HOT_SEARCH_URL = "https://weibo.com/ajax/side/hotSearch"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
]


class CollectionError(Exception):
    """采集失败(超时 / 5xx / 响应异常)。"""


class AuthError(CollectionError):
    """微博 Cookie 失效或未登录,需要人工更新登录态。"""


def _rand_ua() -> str:
    return random.choice(_USER_AGENTS)


@retry(attempts=3, base_delay=1.0, exceptions=(requests.RequestException,))
def _get_json(
    session: requests.Session, url: str, headers: dict[str, str], timeout: int = 15
) -> dict:
    resp = session.get(url, headers=headers, timeout=timeout)
    if resp.status_code == 403 or resp.status_code == 401:
        raise AuthError(f"微博接口返回 {resp.status_code},Cookie 可能已失效")
    if resp.status_code >= 500:
        raise CollectionError(f"微博接口 5xx:{resp.status_code}")
    if resp.status_code != 200:
        raise CollectionError(f"微博接口异常状态码:{resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:  # 非 JSON 响应
        raise CollectionError("微博接口返回非 JSON 数据") from exc


def fetch_hot_search(settings: Settings, session: requests.Session | None = None) -> list[HotItem]:
    """抓取微博热搜榜,返回条目列表。

    参数:
        settings: 配置(提供 Cookie / 代理)。
        session: 可注入的 requests.Session(便于测试);默认新建并按配置挂载代理。

    异常:
        `AuthError`: Cookie 失效。
        `CollectionError`: 其它采集失败。
    """
    owned = session is None
    if owned:
        session = requests.Session()
        proxies = get_proxies(settings)
        if proxies:
            session.proxies.update(proxies)

    headers = {
        "User-Agent": _rand_ua(),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://weibo.com/",
    }
    if settings.weibo_cookie:
        headers["Cookie"] = settings.weibo_cookie

    try:
        data = _get_json(session, HOT_SEARCH_URL, headers)
    finally:
        if owned:
            session.close()

    realtime = data.get("data", {}).get("realtime", [])
    items: list[HotItem] = []
    for idx, item in enumerate(realtime, start=1):
        title = item.get("word") or item.get("note") or ""
        if not title:
            continue
        heat = int(item.get("num") or item.get("raw_hot") or 0)
        items.append(
            HotItem(
                rank=idx,
                title=title.strip(),
                heat=heat,
                category=item.get("category"),
                url=item.get("url"),
                tag=item.get("label_name") or item.get("word_scheme"),
                captured_at=datetime.now(),
            )
        )

    logger.info("微博热搜采集完成,共 %s 条", len(items))
    return items
