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


def gather(title: str, *, session=None, user_id: int = 1, limit: int = 8,
           use_douyin: bool = True) -> dict:
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
