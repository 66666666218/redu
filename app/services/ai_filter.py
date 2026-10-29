# -*- coding: utf-8 -*-
"""**AI 价值筛选**:推送前把「有价值的热点」留下(2026-10-29)。

用户口径:

> 「你群信息有点杂乱啊,需要自己筛选,能不能让 AI 筛选出有价值的热点出来,**每个板块都加上 AI 筛选**」

## 为什么放在"推送前"这一层

各板块的采集/去重/排序都已经有了,乱的是**混进来的低价值条目**:
纯广告、没有具体资源、纯情绪八卦、几个月前的旧帖。规则词表挡不全(本仓在
`niche_fit` 与 `gossip_topics` 上都验证过:关键词分不清"公众人物"和"普通人",
模型分得清)。所以这一层只做一件事:**给每条内容打 0~10 的"能不能变成网盘资源"分**,
按板块配置决定留谁。

## 三条纪律(每条都对着本仓踩过的坑)

1. **失败一律放行,绝不静默丢** —— 模型超时/没配 key/返回垃圾 ⇒ 原样返回并 `source="fallback"`,
   调用方卡片上会看到没有 `AI筛x/y` 标记。宁可吵,不可"看起来筛过了其实全丢了"。
2. **按标题做进程内缓存** —— 热榜类板块 20 分钟一轮、同一标题会反复出现;
   不缓存的话同一句话一天要付几十次钱。
3. **只判前 N 条** —— `ai_filter_max_items` 之外的**原样放行**(它们通常已经被上游排序挤出重点),
   并在日志里写明,别让人以为"筛过了"。

## 用法(每个板块 3 行)

    from app.services import ai_filter
    res = ai_filter.filter_items("douyin", items, settings,
                                 text_of=lambda it: f"{it['title']} 作者:{it.get('author','')}")
    items = res.items
    # 卡片头可写 res.summary() → "AI筛8/20";source=="fallback" 时不写
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.utils import get_logger

logger = get_logger(__name__)

#: `(板块, 文本哈希)` → `{score, keep, why}`。进程级;重启清零可以接受(下一轮重新判一次)。
_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_MAX = 3000


@dataclass
class Result:
    items: list[dict]
    total: int
    kept: int
    #: `llm` = 模型真的筛过;`off` = 没开/没 key/不在名单;`fallback` = 模型失败,**全放行**。
    source: str
    scores: dict[int, int] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)

    def summary(self) -> str:
        """卡片头用的一小段;`off/fallback` 时返回空串(不给自己贴金)。"""
        return f"AI筛{self.kept}/{self.total}" if self.source == "llm" else ""


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _default_text(item: dict) -> str:
    for k in ("title", "name", "word", "resource", "entry_title"):
        v = str(item.get(k) or "").strip()
        if v:
            return v
    return ""


def _sections(settings) -> set[str]:
    raw = str(getattr(settings, "ai_filter_sections", "") or "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def filter_items(section: str, items: list[dict], settings=None, *,
                 text_of=None, extra_context: str = "") -> Result:
    """按 AI 判断筛一遍;失败/没开/不在名单时**原样返回**。

    `text_of(item) -> str` 用来构造喂给模型的文本(**标题优先**,可带作者/热度);
    默认按 `title/name/word/resource/entry_title` 取。
    `extra_context` = **数据先验**(如各赛道历史搬成率,见 `value_prior`),
    直接拼进提示词 —— 用户口径「可以根据之前的数据来进行策略决策」。
    """
    items = list(items or [])
    total = len(items)
    if not items:
        return Result(items, 0, 0, "off")

    if settings is None:
        from config.settings import get_settings

        settings = get_settings()

    allowed = _sections(settings)
    if not bool(getattr(settings, "ai_filter_enabled", True)):
        return Result(items, total, total, "off")
    if allowed and section not in allowed:
        return Result(items, total, total, "off")

    api_key = str(getattr(settings, "deepseek_api_key", "") or "")
    if not api_key:
        #: 没配 key ⇒ **不筛**(也不报错)。这是配置缺失,不是故障;卡片上看不到 `AI筛x/y`。
        return Result(items, total, total, "off")

    text_of = text_of or _default_text
    texts = [str(text_of(it) or "")[:120] for it in items]
    max_items = max(1, int(getattr(settings, "ai_filter_max_items", 40) or 40))

    decisions: dict[int, dict] = {}
    to_judge: list[dict] = []
    pending: dict[int, str] = {}
    for i, text in enumerate(texts):
        if not text:
            continue
        h = _hash(text)
        hit = _CACHE.get((section, h))
        if hit is not None:
            decisions[i] = hit
        elif len(to_judge) < max_items:
            to_judge.append({"i": i, "text": text})
            pending[i] = h
    if total > max_items:
        logger.info("AI筛选[%s]:只判前 %d 条,其余 %d 条原样放行", section, max_items,
                    total - max_items)

    if to_judge:
        from app.services import llm_client

        section_label = section
        try:
            from app.services.board_digest import board_label

            section_label = board_label(section)
        except Exception:  # noqa: BLE001 - 只为提示词好看,取不到不影响筛选
            pass
        got = llm_client.judge_value(
            str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")),
            api_key,
            str(getattr(settings, "deepseek_model", "deepseek-chat")),
            section_label, to_judge,
            timeout=int(getattr(settings, "ai_filter_timeout", 45) or 45),
            extra=extra_context)
        if got is None:
            #: ⚠️ 模型失败 ⇒ **全放行**并说出这件事(纪律 1)。
            logger.warning("AI筛选[%s]失败 ⇒ 本轮**不筛**,%d 条全部照推", section, total)
            return Result(items, total, total, "fallback")
        by_i = {int(d["i"]): d for d in got}
        for i, h in pending.items():
            d = by_i.get(i)
            if d is None:
                continue
            decisions[i] = d
            _CACHE[(section, h)] = d
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.clear()          # 简单粗暴:满了就清,语义上只是"多花一次钱"

    min_score = int(getattr(settings, "ai_filter_min_score", 6) or 0)
    min_keep = max(0, int(getattr(settings, "ai_filter_min_keep", 1) or 0))

    kept_with: list[tuple[int, dict | None, dict, int]] = []
    dropped: list[tuple[int, dict, dict, int]] = []
    for i, it in enumerate(items):
        d = decisions.get(i)
        score = int((d or {}).get("score") or 0)
        if d is None or (d.get("keep") and score >= min_score):
            #: 没判过(空文本/超出 `max_items`)⇒ **放行**,纪律 3。
            kept_with.append((score, d, it, i))
        else:
            dropped.append((score, d, it, i))
    #: 至少留 `min_keep` 条 —— 全被挡光时,按分数补,别让客户收到空卡。
    if len(kept_with) < min_keep:
        dropped.sort(key=lambda x: -x[0])
        kept_with.extend(dropped[:max(0, min_keep - len(kept_with))])
    kept_with.sort(key=lambda x: -x[0])          #: 稳定排序:同分保持上游原序
    kept = [it for _, _, it, _ in kept_with]
    scores = {i: s for s, _, _, i in kept_with if s}
    reasons = {i: str(d.get("why") or "")[:40] for _, d, _, i in kept_with
               if d and d.get("why")}
    logger.info("AI筛选[%s]:留 %d/%d(低于分数线 %d 的挡掉)", section, len(kept), total, min_score)
    return Result(kept, total, len(kept), "llm", scores=scores, reasons=reasons)
