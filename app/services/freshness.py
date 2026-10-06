"""内容**新鲜度**的单一事实源(2026-10-06)。

## 为什么要有这个文件
用户口径:「**2026 年 10 月份之前的不要再保存进来了**」—— 这句话是对**所有内容源**说的。
但实现的时候它只落在了**抖音线索**一条链上(`douyin_leads._min_publish_ts`),
**公众号那条链一处都没有**:2026-10-06 审计实测,10-01 之后仍有 **34 篇 9 月的文章**
照旧入库,另有 52 篇(近 3 天)连发布时间都是空的。

**一个规则在几条链上各写一遍,结果必然是"修了一条、漏了一条"** —— 本项目已经因为
同一个坑在 `quark_kouling` / `pan_discovery` / `resource_presence` 三条链上各踩一次;
新鲜度不能再走老路。所以判定逻辑收在这里,**各链只负责调用**。

## 两道闸的分工
  · `min_publish_ts()` —— 从配置读出**截止时间戳**(秒);没配或解析失败返回 0(不过滤)。
  · `is_too_old()`    —— 拿一条内容的发布时间比一下。**无法判定时返回 False(不挡)**,
    因为"没时间"不等于"很老";但调用方**必须把这类数量报出来**(见 `count_unknown` 的用法),
    否则规则会在这里**静默漏** —— 那正是本仓最忌讳的那种失败。
"""
from __future__ import annotations

import time
from datetime import datetime, date

from app.utils import get_logger

logger = get_logger(__name__)


def min_publish_ts(settings=None, specific_key: str = "") -> int:
    """读新鲜度截止时间(秒级时间戳);**没配或解析失败返回 0 = 不过滤**。

    `specific_key` 是"某条链专属的键"(如 `douyin_leads_min_publish_date`);
    **它留空则回落到通用口径 `content_min_publish_date`** —— 用户那条规则是对所有源说的,
    只认专属键的话,没配专属键的链会悄悄不过滤。

    ⚠️ **写坏了要吭声**:解析失败记一条 warning,别静默当成"不过滤" ——
    那等于用户的规则**悄悄失效**了。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    raw = ""
    if specific_key:
        raw = str(getattr(settings, specific_key, "") or "").strip()
    if not raw:
        raw = str(getattr(settings, "content_min_publish_date", "") or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.strptime(raw, "%Y-%m-%d").timestamp())
    except ValueError:
        logger.warning("新鲜度配置解析失败(%r / key=%r),本轮**不过滤**", raw, specific_key)
        return 0


def parsed_publish_ts(value) -> int | None:
    """把各种形态的发布时间归一成秒级时间戳;**判不出返回 None**(不是 0)。

    实测见过的形态:库里的 `datetime`、列表源给的 ISO 串、以及 epoch 秒/毫秒。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day).timestamp())
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return int(v / 1000) if v > 1e11 else int(v)      # >1e11 当毫秒看
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return int(datetime.strptime(s[:len(fmt) + 4], fmt).timestamp())
        except ValueError:
            continue
    return None


def is_too_old(value, min_ts: int) -> bool:
    """这条内容的发布时间**早于截止**吗?

    · `min_ts <= 0`      → False(没配规则 = 不过滤);
    · 发布时间**判不出** → False(**不挡**,但调用方要单独统计并报出来);
    · 判得出且更早     → True(该挡)。

    ⚠️ "判不出"与"不挡"是同一条分支,但**语义不同**。别让前者悄悄混进后者:
    公众号那条链实测 30% 的条目没有发布时间,若只是"不挡",规则就漏了三成。
    """
    if min_ts <= 0:
        return False
    ts = parsed_publish_ts(value)
    if ts is None:
        return False
    return ts < min_ts


def now_ts() -> int:
    """当前秒级时间戳(单独抽出来,方便测试打桩)。"""
    return int(time.time())
