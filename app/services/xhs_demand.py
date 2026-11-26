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

import re

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


#: 小红书搜索卡上**只有月日**的写法(`11-12` / `10-05`),年份要自己补。
_RE_MD = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _ts_from_card_time(text: str) -> int:
    """把搜索卡上的发布时间文案转成 **Unix 秒**;读不懂返回 `0`(由调用方计数显形)。

    ⚠️ **这是 2026-11-26 订正的核心**:此前取 `noteCard.time` / `lastUpdateTime`,
    实测这两个字段**恒为 `null`**(`noteCard` 只有
    `type/displayTitle/user/interactInfo/cover/imageList/cornerTagInfo` 七个键)
    ⇒ `published` 永远是 0 ⇒ 发布时间闸**一次都没生效过**。

    真实时间在 `noteCard.cornerTagInfo` 里 `type == "publish_time"` 那条的 `text`。
    实测(78 条样本)出现的形状与占比:

    | 形状 | 实例 | 解析 |
    |---|---|---|
    | 相对 | `2分钟前` / `4小时前` / `3天前` | `parse_published_coarse` |
    | 昨天带时刻 | `昨天 21:11` | `parse_published`(`_RE_TODAY`) |
    | **只有月日** | `11-12` / `10-05` / `05-31` | 本函数补年 |
    | 绝对日期 | `2025-08-21` | `parse_published` |

    `MM-DD` 补年规则:按当年算,若因此落到**未来** ⇒ 退回上一年
    (小红书不会给未来的发布时间)。判不出的(**空/别的文案**)一律 `0` ——
    调用方会把它记成「无时间N」,**不许猜一个时间出来**。
    """
    s = str(text or "").strip()
    if not s:
        return 0
    from app.services.hot_time import parse_published, parse_published_coarse

    dt = parse_published(s) or parse_published_coarse(s)
    if dt is None:
        m = _RE_MD.match(s)
        if m:
            from datetime import datetime as _dt

            now = _dt.now()
            try:
                dt = _dt(now.year, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return 0
            if dt > now:
                try:
                    dt = dt.replace(year=now.year - 1)
                except ValueError:      # 2-29 落到平年
                    return 0
    if dt is None:
        return 0
    try:
        return int(dt.timestamp())
    except (OSError, OverflowError, ValueError):
        return 0


def search_notes(page, keyword: str, limit: int = 2) -> list[dict]:
    """搜一个词 → 前 `limit` 篇笔记(带 `xsec_token` 与**发布时间**)。

    ★ **`published`(2026-11-24)**:用户投诉「有的大瓜已经是很久之前的了」,而这条链
    此前**连发布时间都没取** ⇒ 无从过滤。取值点按实测可靠性排:
      ① `noteCard.cornerTagInfo` 里 `type == "publish_time"` 的 `text`(**实测唯一有效**);
      ② `noteCard.time` / `noteCard.lastUpdateTime`(实测**恒 null**,留作字段回潮的兼容);
      ③ 拿不到 = `0`(由调用方按"判不了"处理并**计数显形**,不静默当"新鲜")。
    ⚠️ 单位统一在这里化到**秒**:小红书若哪天改回毫秒,原样丢出去会在渲染时间时炸
    (`publish_mark` 有毫秒兜底,但判新旧用的是 `datetime.fromtimestamp`,必须自己转)。
    """
    page.goto(SEARCH_URL.format(kw=keyword), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    rows = page.evaluate("""() => {
      const s = window.__INITIAL_STATE__ || {};
      const feeds = (s.search || {}).feeds || {};
      const arr = feeds._rawValue || feeds.value || [];
      const toSec = (v) => {
        const n = parseInt(v, 10);
        if (!n || n <= 0) return 0;
        return n > 100000000000 ? Math.floor(n / 1000) : n;
      };
      return arr.map(it => {
        const card = it.noteCard || it.note || {};
        const user = card.user || it.user || {};
        const it0 = card.interactInfo || it.interactInfo || {};
        // ★ 发布时间在 cornerTagInfo 里(实测 time/lastUpdateTime 恒 null)
        const tag = (card.cornerTagInfo || it.cornerTagInfo || [])
                    .find(t => t && t.type === 'publish_time');
        return {id: card.noteId || it.id, token: it.xsecToken || card.xsecToken || '',
                title: (card.displayTitle || it.displayTitle || '').slice(0, 80),
                author: user.nickname || user.nickName || '',
                comments: parseInt(it0.commentCount || '0', 10) || 0,
                published: toSec(card.time || it.time || card.lastUpdateTime || it.lastUpdateTime),
                pub_text: (tag && tag.text) || ''};
      }).filter(x => x.id);
    }""")
    for r in rows:
        ts = int(r.get("published") or 0)
        if ts > 10 ** 11:               # 毫秒 → 秒(字段哪天回潮时别把 fromtimestamp 炸掉)
            ts //= 1000
        if ts <= 0:                     # 页面没给时间戳 ⇒ 用卡片文案兜底
            ts = _ts_from_card_time(r.get("pub_text") or "")
        r["published"] = ts
    #: ★ 2026-10-30:搜索结果里**直接带 `commentCount`** ⇒ 按评论数从多到少开,
    #: 别把额度浪费在 0 评论的笔记上(用户给的两条样本,一条 4 条评论、一条**评论为空**)。
    rows.sort(key=lambda x: x.get("comments") or 0, reverse=True)
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


def page_has_group(page) -> bool:
    """这一页有没有**群聊入口**(用户:「小红书大多都会有加入群聊」)。

    ⚠️ 只是**判存在**:群消息在 App 的 IM 里,网页端只有二维码入口,读不到内容。
    """
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001 - 读不到就当没有
        return False
    return ("群聊" in body) or ("加入群" in body) or ("群号" in body)


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
