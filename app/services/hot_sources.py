# -*- coding: utf-8 -*-
"""热榜源契约与实现(2026-10-01,v2.2.0「命门自持」架构)。

**架构原则(用户要求:命门不掌握在别人手里)**:
    ① 契约层(自持):本模块定义统一热榜源接口,所有源走同一契约——换实现不动调用方;
    ② 实现层(双路线):**能直连的自研**(B站/豆瓣等公开 API,零第三方)、
       **难直连的走自部署 newsnow 容器**(127.0.0.1:4444,数据源在我们机器上,
       newsnow 停维也不影响容器运行;它只是"抓取实现"之一,不是命门);
    ③ 资产层:调用方把所有条目写入**我们自己的库**——历史沉淀与第三方无关;
    ④ 保险层:newsnow 源码快照归档(scripts/snapshot_newsnow.sh),自研抓取器永不依赖第三方。

实测(2026-10-01):B站排行 API / 豆瓣电影 JSON 直连 200;知乎热榜 401(需登录态)——归 newsnow 长尾。
"""
from __future__ import annotations

import json

from curl_cffi import requests as creq

from app.utils import get_logger

logger = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class HotSourceError(Exception):
    """热榜源异常(网络/解析/上游变更)。"""


class HotSource:
    """热榜源契约:实现 `fetch()` 返回 [{title, url, extra}]。

    - 自研源类直接打平台公开接口(零第三方项目依赖);
    - NewsnowSource 打自部署容器(长尾平台覆盖)。
    新增源:实现 fetch 后注册进 `SOURCES`。
    """

    id: str = ""

    def fetch(self, limit: int = 30) -> list[dict]:
        raise NotImplementedError


class BilibiliSource(HotSource):
    """B站全站排行榜(官方公开 API,无需鉴权;2026-10-01 实测 200)。

    对"漫剧/影视"方向价值高:动画区/影视区热门的二创素材指向明确。
    """

    id = "bilibili"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://api.bilibili.com/x/web-interface/ranking/v2",
                         params={"rid": "0", "type": "all"},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
            data = r.json().get("data") or {}
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"B站排行请求失败:{type(exc).__name__}") from exc
        out = []
        for i, it in enumerate((data.get("list") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "rank": i, "title": title,
                "url": str(it.get("short_link_v2") or f"https://www.bilibili.com/video/{it.get('bvid','')}"),
                "extra": f"{it.get('tname') or ''} · 播放{it.get('stat', {}).get('view', 0)}",
            })
        return out


class DoubanSource(HotSource):
    """豆瓣热门电影(公开 JSON;2026-10-01 实测 200)。

    对"影视/漫剧"方向的选题前瞻:豆瓣热度常领先视频平台 1~2 周。
    """

    id = "douban"

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get("https://movie.douban.com/j/search_subjects",
                         params={"type": "movie", "tag": "热门", "page_limit": str(min(limit, 50))},
                         impersonate="chrome", timeout=15,
                         headers={"User-Agent": _UA, "Referer": "https://movie.douban.com/"})
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"豆瓣热门请求失败:{type(exc).__name__}") from exc
        out = []
        for i, it in enumerate((data.get("subjects") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            out.append({"rank": i, "title": title, "url": str(it.get("url") or ""),
                        "extra": f"评分{it.get('rate') or '?'}"})
        return out


class NewsnowSource(HotSource):
    """自部署 newsnow 容器源(长尾平台:知乎/微博/虎嗅/掘金 等 40+)。

    数据取自**我们自己的容器**(127.0.0.1:4444),不依赖任何外部托管服务;
    newsnow 项目若停维,容器照常运行(仅平台清单不再更新——届时以自研源补齐核心平台)。
    """

    def __init__(self, platform_id: str, base_url: str = "http://127.0.0.1:4444") -> None:
        self.id = platform_id
        self.base_url = base_url.rstrip("/")

    def fetch(self, limit: int = 30) -> list[dict]:
        try:
            r = creq.get(f"{self.base_url}/api/s", params={"id": self.id},
                         impersonate="chrome", timeout=20)
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HotSourceError(f"newsnow[{self.id}] 请求失败:{type(exc).__name__}") from exc
        if body.get("status") != "success":
            raise HotSourceError(f"newsnow[{self.id}] 返回异常:{json.dumps(body)[:120]}")
        out = []
        for i, it in enumerate((body.get("items") or [])[:limit], 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            extra = it.get("extra") or {}
            out.append({
                "rank": i, "title": title,
                "url": str(it.get("url") or it.get("mobileUrl") or ""),
                "extra": str(extra.get("info") or extra.get("hover") or "")[:80]
                if isinstance(extra, dict) else str(extra)[:80],
            })
        return out


# 源注册表:自研优先(零第三方),newsnow 补长尾。上层只依赖契约,换实现零改动。
SOURCES: dict[str, HotSource] = {
    # ---- 自研直连(命门自持) ----
    "bilibili": BilibiliSource(),
    "douban": DoubanSource(),
    # ---- newsnow 长尾(自部署容器;知乎 401 等无法直连的平台走这里) ----
    "zhihu": NewsnowSource("zhihu"),
    "weibo": NewsnowSource("weibo"),
    "kuaishou": NewsnowSource("kuaishou"),
    "iqiyi": NewsnowSource("iqiyi"),
    "36kr": NewsnowSource("36kr"),
    "juejin": NewsnowSource("juejin"),
    "ithome": NewsnowSource("ithome"),
    "hupu": NewsnowSource("hupu"),
    "dongqiudi": NewsnowSource("dongqiudi"),
    # 抖音:newsnow 侧 id 无效(实测),我们已有 douhot 采集通道,不重复
}


def fetch_hot(source_id: str, limit: int = 30) -> list[dict]:
    """统一入口:取某源热榜;未知源抛 HotSourceError。"""
    src = SOURCES.get(source_id)
    if src is None:
        raise HotSourceError(f"未知热榜源:{source_id}(可选:{','.join(sorted(SOURCES))})")
    return src.fetch(limit=limit)
