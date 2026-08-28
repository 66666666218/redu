"""数据清洗与过滤(见 doc/dev.md §5.3)。

职责:剔除广告/低频词,规范化,去重,产出去噪后的候选词列表。
纯函数,无网络依赖,核心逻辑可单测。
"""
from __future__ import annotations

from config.settings import Settings
from app.models import HotItem

# 常见置顶/广告词黑名单,命中即整条过滤。
DEFAULT_BLACKLIST = {
    "置顶",
    "广告",
    "推广",
    "热榜",
    "话题",
}


def clean(items: list[HotItem], settings: Settings) -> list[HotItem]:
    """清洗并截取候选词。

    规则(见 doc/dev.md §5.3):
    1. 过滤 `heat < MIN_HEAT` 的低热度词。
    2. 过滤标题命中黑名单的词。
    3. 按标题去重,保留首次出现。
    4. 按热度降序,截取前 `TOP_N` 个。

    参数:
        items: 采集到的热搜条目。
        settings: 配置(提供 `min_heat`、`top_n`)。
    """
    seen: set[str] = set()
    cleaned: list[HotItem] = []

    for item in items:
        if item.heat < settings.min_heat:
            continue
        if any(word in item.title for word in DEFAULT_BLACKLIST):
            continue
        if item.title in seen:
            continue
        seen.add(item.title)
        cleaned.append(item)

    cleaned.sort(key=lambda it: it.heat, reverse=True)
    return cleaned[: settings.top_n]
