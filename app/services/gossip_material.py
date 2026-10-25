"""素材自动采集:把一个**热点标题**变成"能喂给模型的一段素材"(2026-10-25,测试版)。

## 为什么需要它

`compose_gossip_doc()` 的底线是「**只许整理素材、不许凭记忆写**」。没有素材就写不了,
而人工复制报道是这条链最费人的一步。这个模块把"标题 → 素材"自动化。

## 每个平台的实测状态(2026-10-25)

| 平台 | 入口 | 状态 |
|---|---|---|
| 微博 | `weibo_search.search`(**必须传 session** 才读得到加密库里的登录态) | ✅ 每个词 13 条,正文较完整 |
| 抖音 | `douyin_cdp_source.search` | ⚠️ 时好时坏:18:xx 能出 16 条,19:2x 起返回**「验证码中间页」**(会被显式报出来) |
| 百度 | 资讯页抓取 | ❌ 未接(HTML 结构不稳,抓出来噪声大) |

## 两条纪律

1. **每条素材都带来源标签**(`【微博】@作者:`)—— 模型据此写「信息来源」,读者也能自己核。
2. **一个平台失败不许静默**:返回 `fails`,`compose_gossip_doc` 那边只拿到空串会拒跑,
   而调用方(CLI)必须把失败原因打出来。
"""
from __future__ import annotations

from app.utils import get_logger

logger = get_logger(__name__)

#: 喂给模型的素材上限(与 `compose_gossip_doc` 的截断一致,避免"抓了一堆却白抓")
MAX_CHARS = 12000


def _weibo(title: str, session, user_id: int, limit: int) -> tuple[list[str], str]:
    """微博 → `(["【微博】@作者: 正文", ...], 失败原因)`。"""
    from app.services import weibo_search

    try:
        rows = weibo_search.search([title], session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - 失败原因要带回去
        return [], f"{type(exc).__name__}: {str(exc)[:100]}"
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        text = str(r.get("snippet") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(f"【微博】@{str(r.get('name') or '匿名').strip()[:20]}: {text}")
        if len(out) >= limit:
            break
    return out, ""


def _douyin(title: str, session, user_id: int, limit: int) -> tuple[list[str], str]:
    """抖音(CDP)→ `(["【抖音】作者: 文案", ...], 失败原因)`。当前多半会撞验证码。"""
    from app.services import douyin_cdp_source

    try:
        rows = douyin_cdp_source.search([title], session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - 验证码/风控都要如实带回去
        return [], f"{type(exc).__name__}: {str(exc)[:100]}"
    out: list[str] = []
    for r in rows[:limit]:
        text = str(r.get("snippet") or "").strip()
        if not text:
            continue
        out.append(f"【抖音】{str(r.get('name') or '匿名').strip()[:20]}: {text}")
    return out, ""


_BAIDU_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
#: 移动版百度是**服务端渲染**,正文能直接读;但页面里混着**结构化字段残渣**和**评论区**,
#: 不剔掉会把 `"}]…isEncoding` 这种垃圾当素材喂给模型。
_BAIDU_NOISE = ('"}]', "isEncoding", "not_struct", "isSingleLine", "[网易", "网友]",
                "百度一下", "- 百度", "点击即刻体验", "排序方式", "发布时间",
                #: 评论区留言被当成"资讯"喂进去,资料里就会出现一堆"网友猜测"(实测 2026-10-25)
                "评论区", "网友留言", "有态度网友")


def _baidu_news(title: str, limit: int) -> tuple[list[str], str]:
    """百度资讯(移动版)→ `(["【百度资讯】正文", ...], 失败原因)`。

    ⚠️ 为什么用 **m.baidu.com** 而不是 PC 版:PC 版结果是**客户端渲染**,
    HTML 里只有哈希类名(`news-title_1YtI1`),抓不到正文;移动版是服务端渲染。
    """
    import re

    import requests

    try:
        r = requests.get("https://m.baidu.com/s", params={"word": title, "tn": "news"},
                         headers={"User-Agent": _BAIDU_UA}, timeout=15)
        if r.status_code >= 400:
            return [], f"HTTP {r.status_code}"
        html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)
        #: 先把**块级标签**换成换行,否则整页会粘成一行、所有条目没法逐条取
        html = re.sub(r"(?i)</(div|p|li|h\d|section|article)>|<br\s*/?>", "\n", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
    except Exception as exc:  # noqa: BLE001 - 失败原因要带回去
        return [], f"{type(exc).__name__}: {str(exc)[:100]}"

    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        ln = " ".join(raw.split())
        if not (18 <= len(ln) <= 300):
            continue
        if any(n in ln for n in _BAIDU_NOISE):
            continue
        #: 百度把命中词包进 `<em>`,剥标签后会在中文之间留下空格 ⇒ 收掉
        ln = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", ln).lstrip(".。 ")
        #: `A |B |C` 这种是**相关搜索词**堆叠,不是正文,别当素材
        if ln.count("|") >= 2 or len(ln) < 18:
            continue
        key = ln[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append(f"【百度资讯】{ln}")
        if len(out) >= limit:
            break
    return out, ("" if out else "页面里没提取到可用正文")


def gather(title: str, *, session=None, user_id: int = 1, limit: int = 8,
           use_douyin: bool = True, use_baidu: bool = True) -> dict:
    """按标题采素材 → `{"text", "counts", "fails"}`。

    `text` 为空 ⇒ 调用方**必须拒绝生成**(没有素材就不许让模型编)。
    """
    title = str(title or "").strip()
    if not title:
        return {"text": "", "counts": {}, "fails": {"输入": "标题为空"}}
    own = session is None
    if session is None:
        from app.db import get_session_local

        session = get_session_local()()
    blocks: list[str] = []
    counts: dict[str, int] = {}
    fails: dict[str, str] = {}
    try:
        #: **报道类素材放最前** —— 模型写「事件概览」时优先看到它,而不是微博网友发言
        if use_baidu:
            got, why = _baidu_news(title, limit)
            counts["百度资讯"] = len(got)
            blocks.extend(got)
            if why:
                fails["百度资讯"] = why
        got, why = _weibo(title, session, user_id, limit)
        counts["微博"] = len(got)
        blocks.extend(got)
        if why:
            fails["微博"] = why
        if use_douyin:
            got, why = _douyin(title, session, user_id, limit)
            counts["抖音"] = len(got)
            blocks.extend(got)
            if why:
                fails["抖音"] = why
    finally:
        if own:
            session.close()

    text = "\n\n".join(blocks)[:MAX_CHARS]
    logger.info("素材采集:%s → %s(失败:%s)", title[:20], counts, list(fails))
    return {"text": text, "counts": counts, "fails": fails}
