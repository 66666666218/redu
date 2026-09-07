"""搜狗微信搜索(免费候选号发现源):按关键词搜公众号文章,结果自带公众号名。

- 端点: GET https://weixin.sogou.com/weixin?type=2(文章)&query=<词>&page=<n>
- 解析: `txt-box` 块 → 标题(uigs="article_title_*")/摘要(txt-info)/
        公众号名(all-time-y2)/发布时间(timeConvert('<epoch>'))
- 免账号免 Cookie;结果链接是 /link?url= 的二次跳转,本项目 v1 不解析
  (发现场景只需"公众号名 + 标题",标题可直接在微信读书内搜索关注)。
- 反爬: 高频会命中验证码页(antispider),内置 2.5s 串行间隔;命中验证码时
  返回 `blocked=True`,调用方应跳过本轮剩余词而不是重试。
"""
from __future__ import annotations

import html as html_mod
import re
import threading
import time
from datetime import datetime

import requests

from app.utils import get_logger

logger = get_logger(__name__)

BASE = "https://weixin.sogou.com/weixin"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
_BLOCK_MARKS = ("antispider", "seccodeimage", "请输入验证码")
_MIN_GAP = 2.5
_lock = threading.Lock()
_last = 0.0


def _clean(fragment: str) -> str:
    """去红高亮注释/标签 → HTML 反转义 → 压缩空白。"""
    text = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s{2,}", " ", html_mod.unescape(text)).strip()


def search_articles(keyword: str, page: int = 1, timeout: int = 15) -> dict:
    """搜狗微信文章搜索:关键词 → [{name, title, digest, published_at}];blocked=命中验证码。"""
    global _last
    with _lock:
        gap = time.time() - _last
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        _last = time.time()
    try:
        resp = requests.get(BASE, params={"type": 2, "query": keyword, "page": page},
                            timeout=timeout,
                            headers={"User-Agent": _UA, "Referer": "https://weixin.sogou.com/",
                                     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    except requests.RequestException as exc:
        logger.warning("搜狗微信搜索请求失败 %s:%s", keyword, exc)
        return {"items": [], "blocked": False}
    text = resp.text or ""
    if resp.status_code != 200 or any(mark in text for mark in _BLOCK_MARKS):
        logger.warning("搜狗微信搜索命中验证码/异常(HTTP %s),跳过关键词 %s", resp.status_code, keyword)
        return {"items": [], "blocked": True}

    items: list[dict] = []
    for block in re.split(r'<div class="txt-box">', text)[1:]:
        title_m = re.search(r'<h3>.*?uigs="article_title_\d+"[^>]*>(.*?)</a>', block, re.S)
        name_m = re.search(r'<span class="all-time-y2">([^<]+)</span>', block)
        if not title_m or not name_m:
            continue
        title = _clean(title_m.group(1))
        name = _clean(name_m.group(1))
        if not title or not name:
            continue
        digest_m = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S)
        ts_m = re.search(r"timeConvert\('(\d+)'\)", block)
        items.append({
            "name": name[:128],
            "title": title[:500],
            "digest": _clean(digest_m.group(1))[:300] if digest_m else "",
            "published_at": datetime.fromtimestamp(int(ts_m.group(1))) if ts_m else None,
        })
    return {"items": items, "blocked": False}
