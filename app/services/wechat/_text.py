"""盘链识别/正文抓取/质量评估/微信读书响应解析(纯函数,零内部依赖)。"""


from app.services.quark_transfer import extract_quark_urls
from app.utils.ua import CHROME_WINDOWS  # 统一 UA(见该模块注释)

from datetime import datetime

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
_UA = CHROME_WINDOWS
from app.utils import get_logger
from app.utils.timeutil import to_dt  # 时间解析单源

logger = get_logger(__name__)

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


def _parse_time(value: object) -> datetime | None:
    """发文字段容错解析:**统一到 `app.utils.timeutil.to_dt`**(2026-10-05 收敛)。

    这个逻辑原来在本仓有**两份**(另一份在 `wechat_analyzer`),语义还不一样 ——
    调用方看不出区别,直到同一篇文走两条路解析出不同的时间。现在单源。
    保留本名只为兼容既有调用方与测试。
    """
    return to_dt(value)


def _extract_pan_urls(title: str, content: str) -> list[str]:
    """标题+正文里的夸克/百度分享链(单一事实源:入库时抽一次,历史回填也用同一套)。"""
    from app.services.baidupan_transfer import extract_baidu_urls

    blob = f"{title or ''} {content or ''}"
    return extract_quark_urls(blob) + extract_baidu_urls(blob)


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
