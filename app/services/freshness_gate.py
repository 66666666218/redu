# -*- coding: utf-8 -*-
"""**推送新鲜度闸门**(2026-10-29)。

用户口径:「**全部都需要新鲜度检查,所有板块不要都跟知乎一样占比这么少**」。

## 现状(实测,12 天)

- 抖音线索**采集时**就有 7 天窗口;而其它板块**推送时没有任何时间检查**;
- `pan_discovery` 的"太老不搬"闸门 `pan_discovery_max_age_days` **默认是 0(关着)**;
- `discovered_pan_links` 里知乎资源**发布→发现平均 4.81 天、7 天内只占 6.5%** ——
  供给本身就旧,光过滤会让这个板直接空掉。

## 所以这一层只做两件事

1. **推送前统一按发布时间裁**(`PUSH_FRESH_DAYS`,默认 7 天);
2. **把"新鲜占比"显式报出来**(卡片/运行记录):`新鲜 8/12(无时间 3)`。
   —— 不报出来的话,"这个板全是旧货"与"这个板没内容"长得一模一样。

⚠️ **判不了时间的条目默认放行**(`PUSH_FRESH_ALLOW_UNKNOWN`):很多源就是不给时间,
拿"不知道"当"老"会把整条板堵死(与 `pan_discovery._too_old` 的口径一致)。
⚠️ 它**不改采集/转存**的闸门 —— 只决定"给客户看什么",不动库里的存量。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _as_dt(published):
    """`int/float/str/datetime` → `datetime`;认不出返回 `None`(不是异常)。

    ⚠️ **`0` 一律当"不知道"**(2026-10-29):各源用 `publish_at=0` 表示"没给时间",
    照 `fromtimestamp(0)` 解释会变成 **1970 年** ⇒ 每一条都被判成"太旧"。
    """
    if published is None or published == "" or published == 0 or published == "0":
        return None
    if isinstance(published, datetime):
        return published
    if isinstance(published, (int, float)):
        try:
            return datetime.fromtimestamp(float(published))
        except (OverflowError, OSError, ValueError):
            return None
    try:
        from app.services.hot_time import parse_published

        dt = parse_published(str(published))
        if isinstance(dt, datetime):
            return dt
    except Exception:  # noqa: BLE001 - 只用于展示/过滤,解析失败按"不知道"处理
        pass
    return None


def is_stale(published, settings=None, days: int | None = None) -> bool:
    """这条**明确知道很旧**吗?(不知道时间 ⇒ False,见模块说明)"""
    if settings is None and days is None:
        from config.settings import get_settings

        settings = get_settings()
    if days is None:
        days = int(getattr(settings, "push_fresh_days", 7) or 0)
    if days <= 0:
        return False
    dt = _as_dt(published)
    if dt is None:
        return False
    return dt < datetime.now() - timedelta(days=days)


def split_fresh(items: list[dict], settings=None, *, key: str = "publish_at",
                days: int | None = None) -> tuple[list[dict], int, int]:
    """按 `key` 裁一遍 → `(保留, 太旧条数, 无时间条数)`。

    `days` 优先取参数,其次 `settings.push_fresh_days`;`0` = 关。
    ⚠️ 无时间的条目**保留**并单独计数 —— 让调用方把它写进卡片,别悄悄当成"新鲜"。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if days is None:
        days = int(getattr(settings, "push_fresh_days", 7) or 0)
    if days <= 0 or not items:
        return list(items or []), 0, 0
    allow_unknown = bool(getattr(settings, "push_fresh_allow_unknown", True))
    kept: list[dict] = []
    stale = unknown = 0
    cutoff = datetime.now() - timedelta(days=days)
    for it in items:
        dt = _as_dt(it.get(key))
        if dt is None:
            unknown += 1
            if allow_unknown:
                kept.append(it)
            continue
        if dt < cutoff:
            stale += 1
            continue
        kept.append(it)
    return kept, stale, unknown


def summary(kept: int, stale: int, unknown: int) -> str:
    """给卡片/运行记录的一句话(只在**真的裁过**时才有内容)。"""
    if stale <= 0 and unknown <= 0:
        return ""
    total = kept + stale
    bits = [f"新鲜 {kept}/{total}"]
    if unknown:
        bits.append(f"无时间 {unknown}")
    return " · ".join(bits)
