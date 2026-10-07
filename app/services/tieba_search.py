# -*- coding: utf-8 -*-
"""贴吧:**纯协议**搜索(2026-10-08)。

链路拆解与自修指南见 `doc/贴吧纯协议-链路拆解.md`。

## 一句话
用 `aiotieba`(**已经是本项目的依赖** —— 一直在用它取点赞数):
`search_global` 搜出帖子 → `get_posts` 取**首楼全文** → 从全文里抽盘链。
**全程匿名**:不开浏览器、不要 BDUSS、不要登录档案。

## ★ 关键:盘链**不在**搜索返回里
`search_global` 给的 `content` 是**截断的摘要**(实测 75~85 字,常常刚好在链接前断掉)
⇒ 直接拿它抽盘链,一个词只能抽出 **0 条**(2026-10-08 A/B 实测)。
而**首楼全文**里 6/6 都有带提取码的 `pan.baidu.com/s/…?pwd=`。

⚠️ 全文在楼层对象的 **`contents`** 字段(富文本片段),**不是 `content`** ——
后者恒为 `None`。我第一版读 `content`,拿到"首楼 0 字",差点把结论写成"匿名取不到全文"。

## 代价
每个帖子**多一次请求**(取首楼)。所以有 `tieba_detail_limit` 封顶(默认 30/轮)——
拿多少条链基本就等于取多少个首楼,而命中率接近 100%。

## ⚠️ 两条纪律
1. **"搜不到" 与 "被挡" 必须分开**:接口报错/超时一律抛 `TiebaSearchError`,
   **绝不返回空列表** —— 否则"贴吧今天挂了"会被读成"今天没人发资源"。
2. 逐词之间留间隔。匿名接口没那么脆,但没必要贴着上限跑。
"""
from __future__ import annotations

import time
import urllib.parse

from app.utils import get_logger
from app.utils.asyncrun import run_async

logger = get_logger(__name__)

PLATFORM = "tieba"
#: 单次搜索条数(接口上限 50,>50 服务端会退化成 10 条)
PAGE_SIZE = 20
#: 逐词之间的间隔(秒)。保守取值:匿名接口没那么脆,但没必要贴着跑。
_GAP = 2.0
#: 每轮最多取多少个帖子的首楼(每个一次请求)。0 = 读设置。
DEFAULT_DETAIL_LIMIT = 30


class TiebaSearchError(Exception):
    """贴吧搜索硬失败。**别把它吞成空列表** —— 见模块 docstring 的纪律 1。"""

    def __init__(self, msg: str, kind: str = "api") -> None:
        super().__init__(msg)
        self.kind = kind


async def _search_async(keyword: str, pages: int, rn: int) -> list:
    """一次搜索(可翻页)。返回 aiotieba 的 `SearchGlobal` 对象列表。"""
    import aiotieba

    out: list = []
    async with aiotieba.Client() as client:
        for pn in range(1, max(1, pages) + 1):
            res = await client.search_global(keyword, pn=pn, rn=rn)
            objs = list(res)
            out.extend(objs)
            logger.info("贴吧纯协议:「%s」第 %d 页 +%d 条", keyword[:20], pn, len(objs))
            if not objs or not getattr(res, "has_more", False):
                break
    return out


def _pan_links(text: str) -> list[str]:
    """从一段文本里抽网盘链 —— **先反转义再抽**。

    ⚠️ 首楼正文里的链接常常是 `tiebaclient://…?url=https%3A%2F%2Fpan.baidu.com%2Fs%2F…`
    这种**编码过**的深链,不 unquote 就抽不出来。
    """
    from app.services.wechat._text import _extract_pan_urls

    if not text:
        return []
    hits = _extract_pan_urls("", text)
    if hits:
        return hits
    try:
        return _extract_pan_urls("", urllib.parse.unquote(text))
    except Exception:  # noqa: BLE001 - 反转义失败就当没抽到,别把整轮拖垮
        return []


def _row(obj: object, keyword: str, detail: dict | None) -> dict | None:
    """`SearchGlobal`(+ 首楼详情)→ 统一记录形状(与 `mediacrawler_source._parse_record` 对齐)。"""
    tid = str(getattr(obj, "tid", "") or "").strip()
    if not tid:
        return None
    title = str(getattr(obj, "title", "") or "").strip()
    excerpt = str(getattr(obj, "content", "") or "").strip()
    full = str((detail or {}).get("text") or "")
    # 抽链**只看全文**(摘要里通常没有);摘要保留给展示
    links = _pan_links(full) or _pan_links(excerpt)
    name = (str(getattr(obj, "author_name", "") or "").strip()
            or str(getattr(obj, "author_show_name", "") or "").strip())
    # `post_num` = **回复数**(aiotieba 字段说明);口径上等同评论数,不冒充转发。
    replies = int(getattr(obj, "post_num", 0) or 0)
    metrics: dict[str, int] = {}
    if replies > 0:
        metrics["comment_count"] = replies
    agree = (detail or {}).get("agree")
    if isinstance(agree, int) and agree > 0:
        metrics["liked_count"] = agree          # ★ 白拿的:取全文那一次顺带回来的
    return {"uid": str(getattr(obj, "author_id", "") or ""),
            "name": name[:64],
            "url": f"https://tieba.baidu.com/p/{tid}",
            "snippet": " ".join(dict.fromkeys(p for p in (title, excerpt) if p))[:255],
            "pan_link": (links[0] if links else "")[:500],
            # ⚠️ 贴吧**没有转发数**;列成 0 是为兼容老列(int 没有 None 这个取值)
            "share_count": 0,
            "metrics": metrics,                 # 交给 conversion 算曝光
            "keyword": keyword,
            # ★ 比 MediaCrawler 那条**多**给的:发帖时间(新鲜度的真值)
            "publish_at": int(getattr(obj, "create_time", 0) or 0),
            "forum_name": str(getattr(obj, "forum_name", "") or ""),
            "tid": tid}


def search(keywords: list[str], pages: int = 1, rn: int = PAGE_SIZE,
           detail_limit: int = 0, with_detail: bool = True) -> list[dict]:
    """按关键词搜贴吧。返回与 `mediacrawler_source.crawl` 同一形状的记录列表。

    `with_detail=False` **跳过"取首楼全文"那一步**(每个帖子省一次请求)。
    ⚠️ 两种调用方的需求不同,别一刀切:
      · `pan_discovery` 要**盘链** ⇒ 必须带详情(链只在首楼全文里);
      · `resource_presence` 只关心"这个名字在贴吧出现了几次" ⇒ **不要详情**,
        省掉 N 次请求(它连 `pan_link` 都不读)。
      另:后者可以调大 `rn`(上限 50)拿更多条 —— 它的判据是**条数**。

    ⚠️ **硬失败会抛 `TiebaSearchError`,不返回空列表**(见模块 docstring 的纪律 1)。
    """
    from app.services.tieba_metrics import DEFAULT_LIMIT, fetch_posts_detail

    kws: list[str] = []
    for k in (keywords or []):
        if k is None:
            continue
        s = str(k).strip()
        if s:
            kws.append(s)
    if not kws:
        return []
    if detail_limit <= 0:
        from config.settings import get_settings

        detail_limit = int(getattr(get_settings(), "tieba_detail_limit",
                                   DEFAULT_DETAIL_LIMIT) or DEFAULT_DETAIL_LIMIT)

    cands: list[tuple[object, str]] = []
    seen: set[str] = set()
    for i, kw in enumerate(kws):
        if i:
            time.sleep(_GAP)
        try:
            objs = run_async(_search_async(kw, pages, rn))
        except ImportError as exc:                      # 依赖没装 —— 说清怎么装
            raise TiebaSearchError(
                "未安装 aiotieba(`pip install aiotieba`)", kind="dependency") from exc
        except Exception as exc:  # noqa: BLE001 - 统一成我们的错误类型,别让上层猜
            raise TiebaSearchError(
                f"贴吧搜索失败({type(exc).__name__}):{str(exc)[:140]}", kind="api") from exc
        for obj in objs:
            tid = str(getattr(obj, "tid", "") or "")
            if tid and tid not in seen:
                seen.add(tid)
                cands.append((obj, kw))

    # ★ 取首楼全文:**盘链只在这里**(搜索给的摘要是截断的)。
    #   `fetch_posts_detail` **不抛**(旁路),拿不到就退回"只有摘要"——已在 `_row` 里处理。
    details: dict = {}
    if with_detail:
        tids = [c[0].tid for c in cands[:detail_limit]]
        details = fetch_posts_detail(tids, limit=DEFAULT_LIMIT * 8) if tids else {}
        logger.info("贴吧纯协议:%d 个候选,取到 %d 条首楼全文", len(cands), len(details))
    else:
        logger.info("贴吧纯协议:%d 个候选(**按需跳过首楼详情**)", len(cands))

    rows: list[dict] = []
    for obj, kw in cands:
        row = _row(obj, kw, details.get(str(getattr(obj, "tid", "") or "")))
        if row:
            rows.append(row)
    n_pan = sum(1 for r in rows if r["pan_link"])
    logger.info("贴吧纯协议:共 %d 条,其中带盘链 %d 条", len(rows), n_pan)
    return rows
