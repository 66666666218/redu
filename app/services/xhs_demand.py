"""小红书「求/1型」大瓜扫描 —— **走 CDP 页面路**(2026-10-30)。

## 用户口径

> 「小红书上的热点大瓜应该更好监控」
> 「我没有给你找出小红书的链接,你可以根据抖音的来进行测小红书」

所以:**种子词还是抖音那套参考作品名**,只把平台换成小红书;判据与抖音完全一致
(求/1 密度 + 重复率)。

## 为什么走页面(而不是协议)

小红书对"刚登录就发脚本请求"会**销会话**(`461/300011`,见
`doc/小红书纯协议-链路拆解.md` §10)。页面路是**接管真人浏览器**:
导航由浏览器自己发、签名由页面自己的 JS 算,`navigator.webdriver=False`。

## 两条实测约束(2026-10-30)

1. **笔记页必须带 `xsec_token`**,否则回「当前笔记暂时无法浏览 请打开小红书App扫码查看」;
   token 在搜索结果页的 `window.__INITIAL_STATE__.search.feeds[].xsecToken` 里;
2. 评论是**滚动懒加载**的,要滚几次才有量;选择器 `.comment-item .content` 实测可用
   (两条笔记分别读到 12 / 57 条)。

⚠️ **频率**:每轮只搜 1~2 个种子词、每个词最多开 2 篇笔记 —— 宁可慢,别把号打没。
"""
from __future__ import annotations

from app.utils import get_logger

logger = get_logger(__name__)

SEARCH_URL = ("https://www.xiaohongshu.com/search_result?keyword={kw}"
              "&source=web_search_result_notes")
NOTE_URL = ("https://www.xiaohongshu.com/explore/{note_id}"
            "?xsec_token={token}&xsec_source=pc_search")
#: 实测可用的评论选择器(按优先级)
COMMENT_SEL = ".comment-item .content"


def available(settings=None) -> tuple[bool, str]:
    """CDP 通不通。复用 `xhs_cdp_source.available` —— **口径只有一份**。"""
    from app.services import xhs_cdp_source

    try:
        return xhs_cdp_source.available(settings)
    except Exception as exc:  # noqa: BLE001 - 探活失败就是不可用
        return False, f"CDP 探活异常:{type(exc).__name__}: {str(exc)[:60]}"


def _connect():
    """连上**人开的那个浏览器**;返回 (playwright 对象, browser)。用完必须 close。"""
    from playwright.sync_api import sync_playwright

    from app.services.xhs_cdp_source import endpoint

    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp(endpoint())
    return p, browser


def search_notes(page, keyword: str, limit: int = 2) -> list[dict]:
    """搜一个词 → 前 `limit` 篇笔记(带 `xsec_token`)。"""
    page.goto(SEARCH_URL.format(kw=keyword), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    rows = page.evaluate("""() => {
      const s = window.__INITIAL_STATE__ || {};
      const feeds = (s.search || {}).feeds || {};
      const arr = feeds._rawValue || feeds.value || [];
      return arr.map(it => {
        const card = it.noteCard || it.note || {};
        const user = card.user || it.user || {};
        return {id: card.noteId || it.id, token: it.xsecToken || card.xsecToken || '',
                title: (card.displayTitle || it.displayTitle || '').slice(0, 80),
                author: user.nickname || user.nickName || ''};
      }).filter(x => x.id);
    }""")
    return rows[:max(1, int(limit))]


def note_comments(page, note_id: str, token: str = "", scrolls: int = 3) -> list[str]:
    """打开笔记、滚几次加载评论,返回评论文本列表。"""
    page.goto(NOTE_URL.format(note_id=note_id, token=token),
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    for _ in range(max(1, int(scrolls))):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(1200)
    try:
        return page.eval_on_selector_all(
            COMMENT_SEL, "els => els.map(e => e.innerText.trim()).filter(Boolean)")
    except Exception as exc:  # noqa: BLE001 - 读不到就是 0 条(下面会记进去)
        logger.info("小红书评论读取失败 %s: %s", note_id, str(exc)[:60])
        return []


def stats_from_texts(texts: list[str]) -> dict:
    """同一套判据(与抖音**共用** `demand_watch` 的归一化与匹配),算密度/重复率。"""
    from app.services.demand_watch import _DEMAND_WORDS, _ONES_RE, _norm_comment

    total = ones = demand = 0
    norms: list[str] = []
    for t in (texts or []):
        total += 1
        norm = _norm_comment(t)
        norms.append(norm or str(t).strip())
        if _ONES_RE.match(norm):
            ones += 1
        if any(w in str(t) for w in _DEMAND_WORDS):
            demand += 1
    dup = 0.0
    if total >= 5:
        dup = 1 - (len(set(norms)) / total)
    return {"total": total, "ones": ones, "demand": demand, "dup_ratio": round(dup, 3)}
