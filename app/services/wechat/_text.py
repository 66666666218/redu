"""盘链识别/正文抓取/质量评估/微信读书响应解析(纯函数,零内部依赖)。"""

from app.db.models import (FeishuAlert, User, WechatArticle, WechatBenchmark, WechatCandidate,
                           WechatPanLink, WechatRewrite, WechatTrafficSample)

from app.services.quark_transfer import QuarkAuthError, QuarkError, QuarkTransfer, extract_quark_urls

from datetime import datetime, timedelta

import html as html_mod

import re

import requests



PAN_PATTERNS = {
    "夸克网盘": re.compile(r"pan\.quark\.cn/s/[0-9a-zA-Z]+"),
    "百度网盘": re.compile(r"pan\.baidu\.com/s/[0-9a-zA-Z_\-]+"),
    "UC网盘": re.compile(r"drive\.uc\.cn/s/[0-9a-zA-Z]+"),
    "迅雷云盘": re.compile(r"pan\.xunlei\.com/s/[0-9a-zA-Z]+"),
}
_MY_LINK_RE = re.compile(r"https?://pan\.quark\.cn/s/[0-9A-Za-z]+|https?://pan\.baidu\.com/s/[0-9A-Za-z_\-]+")
TITLE_HINTS = ("夸克", "百度网盘", "百度云", "UC网盘", "UC盘", "迅雷", "阿里云盘",
               "网盘", "资源", "全套", "合集", "分享", "链接", "更新",
               "入口", "地址", "下载", "获取", "自取", "领取", "复制", "保存", "直达",
               "素材", "模板", "线稿", "电子版", "答案", "真题", "教程", "壁纸", "表情包",
               "pdf", "PDF")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
from app.utils import get_logger

logger = get_logger(__name__)
from app.services import wechat_monitor as _root  # 兼容 monkeypatch:可替换名经门面运行时查找

def detect_pan_types(text: str) -> list[str]:
    """返回文本涉及的网盘类型:优先分享链接特征(确认级),否则退回盘名关键词(标题疑似级)。"""
    if not text:
        return []
    hits = [name for name, pat in PAN_PATTERNS.items() if pat.search(text)]
    if hits:
        return hits
    mapping = (("夸克网盘", ("夸克",)), ("百度网盘", ("百度网盘", "百度云")),
               ("UC网盘", ("UC网盘", "UC盘")), ("迅雷云盘", ("迅雷云盘", "迅雷")))
    return [name for name, kws in mapping if any(k in text for k in kws)]
def title_hits(title: str) -> bool:
    """标题粗筛:是否值得取正文确认。"""
    return any(h in (title or "") for h in TITLE_HINTS)
def _public_get(url: str, timeout: int):
    """GET 用户外链并逐跳重校验公网性,堵住重定向 SSRF。

    `assert_public_url` 只校验首个 URL;而 `requests` 默认 `allow_redirects=True`,
    公开域可用 302 把服务器引到 `http://169.254.169.254/` 等内网。故关掉自动重定向,
    手动逐跳再验。跳内网时抛 UnsafeUrlError,调用方按"拒绝外链"处理。
    """
    from urllib.parse import urljoin

    from app.utils.net import assert_public_url

    current = url
    for _ in range(6):  # 最多跟随 5 跳,防重定向环
        assert_public_url(current)
        resp = requests.get(current, timeout=timeout, headers={"User-Agent": _UA},
                            allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            current = urljoin(current, resp.headers["Location"])
            continue
        return resp
    from app.utils.net import UnsafeUrlError
    raise UnsafeUrlError("重定向次数过多")
def fetch_article_content(url: str, timeout: int = 15) -> str:
    """免费自抓微信文章正文(纯文本,含正文超链接与「阅读原文」的目标 URL)。

    命中风控("环境异常"验证页)或拿不到正文容器时返回空串——调用方应容忍空结果,
    (2026-09-29 起 dajiala 收费链已整体摘除,免费源不可用即如实记失败。)
    """
    if not url:
        return ""
    from app.utils.net import UnsafeUrlError

    try:
        resp = _public_get(url, timeout=timeout)
        text = resp.text or ""
    except (requests.RequestException, UnsafeUrlError) as exc:
        logger.warning("拒绝抓取非公网外链:%s", exc)
        return ""
    if resp.status_code != 200 or "环境异常" in text:
        return ""
    body = _article_text_with_links(text)
    if not body:
        logger.info("正文容器缺失(疑似风控壳页),不入库整页脚本:%s", url[:80])
    return body
def _article_text_with_links(page_html: str, limit: int = 20000) -> str:
    """正文文本 + 「阅读原文」外链;容器缺失返回空串。"""
    from app.utils.html_text import article_body, original_link_url

    body = article_body(page_html, limit=limit)
    if not body:
        return ""
    orig = original_link_url(page_html)
    if orig and orig not in body:
        body = (body + " " + orig).strip()[:limit]
    return body
def extract_article_meta(url: str, timeout: int = 15) -> dict:
    """免费解析文章页元信息:{biz, name, title}(与 xg 同款正则);失败/风控页返回 {}。"""
    from app.utils.net import UnsafeUrlError

    try:
        resp = _public_get(url, timeout=timeout)
        text = resp.text or ""
    except (requests.RequestException, UnsafeUrlError) as exc:
        logger.warning("拒绝解析非公网外链:%s", exc)
        return {}
    if resp.status_code != 200 or "环境异常" in text:
        return {}

    def _match(patterns: list[str]) -> str:
        for pat in patterns:
            m = re.search(pat, text, re.I | re.S)
            if m:
                return html_mod.unescape(m.group(1)).strip()
        return ""

    out = {}
    biz = _match([r'var\s+biz\s*=\s*"([^"]+)"', r'biz:\s*"([^"]+)"', r'__biz=([^&"\s]+)'])
    name = _match([r'id="js_name"[^>]*>\s*([^<]+?)\s*</a>', r'var\s+nickname\s*=\s*"([^"]+)"'])
    title = _match([r'id="activity-name"[^>]*>\s*(?:<span[^>]*>)?\s*([^<]+?)\s*(?:</span>)?\s*</h1>',
                    r'var\s+msg_title\s*=\s*"([^"]+)"'])
    if biz:
        out["biz"] = biz
    if name:
        out["name"] = name
    if title:
        out["title"] = title
    return out
_BAIT_PATTERNS = [
    re.compile(r"(?:加|加我|添加|私信|联系)(?:微信|微信好友|我|助手|客服)"),
    re.compile(r"(?:付费|收费|会员|VIP|开通|解锁)[后以]?[再才]?获取"),
    re.compile(r"(?:进群|入群|加群)获取"),
    re.compile(r"(?:扫码|扫描)(?:二维码|关注)[后以]?[获取领取]"),
    re.compile(r"(?:原价|限时|特价|优惠)[¥￥\d]"),
]
_QUALITY_PAN = 3       # 有实际盘链
_QUALITY_RECENT = 1    # 近3天发布
_QUALITY_MULTI = 2     # 多号同发
def assess_quality(content: str, pan_urls: list[str], read_num: int,
                   resonance_cnt: int = 0, days_old: int = 0) -> dict:
    """内容质量评估:盘链确认 / 虚假宣传 / 引流话术 检测。

    返回 {has_pan: bool, is_bait: bool, bait_signals: [...], quality_score: int}。
    quality_score: 0~10,≥6 为高质量(值得跟进),≤2 为低质量(广告/虚假)。
    """
    score = 0
    has_pan = bool(pan_urls)
    bait_signals: list[str] = []

    # ① 盘链确认(+3)
    if has_pan:
        score += _QUALITY_PAN

    # ② 时效(+1)
    if days_old <= 3:
        score += _QUALITY_RECENT

    # ③ 多号同发(+2)
    if resonance_cnt >= 2:
        score += _QUALITY_MULTI

    # ④ 虚假宣传检测:引流话术但无实际盘链
    if not has_pan:
        for pat in _BAIT_PATTERNS:
            m = pat.search(content or "")
            if m:
                bait_signals.append(m.group(0)[:20])
                score = max(0, score - 1)
    # ⑤ 阅读量加分
    if read_num >= 500:
        score += 2
    elif read_num >= 100:
        score += 1

    return {
        "has_pan": has_pan,
        "is_bait": bool(bait_signals) and not has_pan,
        "bait_signals": bait_signals,
        "quality_score": min(score, 10),
    }
def _deep_find(node: object, key: str):  # noqa: ANN201
    """递归找第一个命中键的值(响应字段层级未完全实测,统一防御式取数)。"""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None
_URL_KEYS = ("content_url", "url", "link", "surl")
_TIME_KEYS = ("send_time", "timestamp", "publish_time", "datetime")
def _parse_time(value: object) -> datetime | None:
    """发文字段容错解析:epoch 秒(数字/数字串)或 ISO 字符串。"""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        s = str(value).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s))
        # 带 Z/偏移的 ISO 串是 UTC 墙钟,而全库时间戳统一为"服务器本地 naive"
        # (datetime.now());必须先 astimezone() 转本地再抹 tzinfo,否则会把 UTC
        # 当本地存,发布时段×阅读、近 N 天过滤整体偏移一个时区。naive 串 astimezone()
        # 按本地解释、值不变,安全。
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (ValueError, OSError, TypeError):
        return None
def _extract_articles(node: object, url_keys: tuple[str, ...] = _URL_KEYS) -> list[dict]:
    """防御式抽取文章条目:递归找"带 title + 链接字段"的字典,保持原顺序。"""
    found: list[dict] = []

    def walk(n: object) -> None:
        if isinstance(n, dict):
            title = str(n.get("title") or n.get("Title") or "").strip()
            link = ""
            for k in url_keys:
                v = n.get(k) or n.get(k.capitalize())
                if v:
                    link = str(v).strip()
                    break
            if title and link:
                ts = None
                for k in _TIME_KEYS:
                    if n.get(k) is not None:
                        ts = _parse_time(n[k])
                        if ts:
                            break
                found.append({"title": title, "url": link, "publish_at": ts})
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found
def _extract_pan_urls(title: str, content: str) -> list[str]:
    """标题+正文里的夸克/百度分享链(单一事实源:入库时抽一次,历史回填也用同一套)。"""
    from app.services.baidupan_transfer import extract_baidu_urls

    blob = f"{title or ''} {content or ''}"
    return extract_quark_urls(blob) + extract_baidu_urls(blob)
