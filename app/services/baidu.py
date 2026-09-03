"""百度热搜采集(见 doc/dev.md §5.2b)。

请求 top.baidu.com 的公开热搜榜接口(无需登录),返回 `list[BaiduItem]`。
字段对齐 `WeiboHotItem` 的 title/heat/rank/url,便于复用判涨与智能体。
"""
from __future__ import annotations

import requests

from config.settings import Settings
from app.utils import get_logger, retry

logger = get_logger(__name__)

BAIDU_TOP_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)


class BaiduItem:
    """百度热搜条目。"""

    def __init__(self, title: str, heat: int, rank: int, url: str = "") -> None:
        self.title = title
        self.heat = heat
        self.rank = rank
        self.url = url


def _to_int(v: object) -> int:
    """把热值转 int;兼容 '12,345' / '6千' / '1.2万' 等格式。"""
    s = str(v or "").strip().replace(",", "")
    mul = 1
    if s.endswith("万"):
        mul, s = 10000, s[:-1]
    elif s.endswith("千"):
        mul, s = 1000, s[:-1]
    try:
        return int(float(s) * mul)
    except (ValueError, TypeError):
        return 0


def _collect(node: object, out: list[BaiduItem], seen: set[str]) -> None:
    """递归找热搜条目:含 `word` + `hotTag`/`url` 的节点即成一条。保持出现顺序=排名。"""
    if isinstance(node, dict):
        word = (node.get("word") or node.get("query") or "").strip()
        if word and word not in seen and ("hotTag" in node or "url" in node or "hotScore" in node):
            seen.add(word)
            out.append(BaiduItem(title=word, heat=_to_int(node.get("hotTag") or node.get("hotScore")),
                                 rank=len(out) + 1, url=node.get("url", "")))
            return
        for v in node.values():
            _collect(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out, seen)


@retry(attempts=3, base_delay=1.5, exceptions=(requests.RequestException,))
def fetch_hot(settings: Settings, session: requests.Session | None = None) -> list[BaiduItem]:
    """抓取百度热搜榜(公开接口,无需登录态),返回条目列表(按返回顺序即排名)。

    top.baidu.com 返回结构是深嵌套的(多级 content),用递归提取 word+hotTag。
    """
    owned = session is None
    session = session or requests.Session()
    headers = {"User-Agent": _UA, "Accept": "application/json", "Referer": "https://top.baidu.com/board?tab=realtime"}
    try:
        resp = session.get(BAIDU_TOP_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise requests.RequestException(f"百度热搜请求失败:{exc}") from exc
    finally:
        if owned:
            session.close()

    items: list[BaiduItem] = []
    seen: set[str] = set()
    _collect(data, items, seen)
    if not items:
        logger.warning("百度热搜未解析到条目(接口可能改版)")
    return items[:30]
