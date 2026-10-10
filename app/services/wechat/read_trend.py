# -*- coding: utf-8 -*-
"""阅读数的**增长判读**(2026-10-10:重新启用 dajiala 时代废弃的那组列)。

## 为什么只用「增速」,不用「绝对值」

阅读数是**累计量** —— 老文章攒得久,绝对值天然更大。按绝对值排 = **系统性偏向老文章**。
这条 confound 今晚刚在抖音线索上吃过一次(`DouyinLead.share_count` 一模一样):
当时的"TOP 榜单"第一名是 67 小时前发的,而一条 4 小时就攒到 1379 转发的**正在起量**的
线索被压到十几名外。⇒ 这里同一条纪律:**判"起量"只看两个可比量,而且**两段不共享分子**:

    采样前速率 = 基线读数 / (基线时刻 − 发布时刻)      ← 立基线那一段的平均(文章自己的历史)
    采样后速率 = 增量     / (现在   − 基线时刻)        ← 观察窗内的(文章现在的表现)

判据是**自参照**的(拿它的"现在"比它的"过去"),不用拍脑袋的全局阈值:

    采样后 ≥ ACCEL × 采样前  → 「🔥加速」  最近比之前快一倍以上
    采样后 ≤ FALL  × 采样前  → 「↘回落」  明显慢下来了
    其余                      → ""

⚠️ **第一版写错了,注释里留了现场**:我原本拿"最近/发布以来平均"比 —— 那**结构上几乎
不可能触发**(两者共用分子,涨得越猛历史平均也一起涨;且观察窗=年龄一半时比值数学上封顶 2)。
详见 `_judge` 的说明。

## 两道防噪(都不是想当然,是这类公式的必然毛病)

1. **观察窗要够长**(默认 ≥2h):两个采样点挨太近,分母趋零 ⇒ 速率被放大成天文数字;
2. **增量要够大**(默认 ≥20):冷门号"最近多了一个阅读"也会算出极高速率 —— 纯噪声。

## 与调用方的关系

本模块**只做判读**,不碰采集、不碰落库。`observe()` 接受**任何带那几个属性的对象**
(ORM 行、或 `SimpleNamespace` 这种轻量替身)—— 这样"新建行"和"回填老行"两条路
**共用同一份判据**,不会出现"两处各写一遍、迟早飘一个"。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

#: 「🔥加速」的倍数门槛:**最近的速度 ≥ 历史平均的这么多倍**
ACCEL = 2.0
#: 「↘回落」的倍数门槛
FALL = 0.3

#: `trend_flag` 的取值(列宽 16,够)
FLAG_ACCEL = "🔥加速"
FLAG_FALL = "↘回落"


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def observe(art: Any, read_num: Any, *, now: datetime | None = None,
            settings=None) -> str:
    """把一次读数**采样**写进 `art`(原地改属性),返回趋势标记。

    **只在"读数真的变了"时才算一次采样** —— 否则每一轮监听都会把 `sample_count`
    和白刷一遍,而"采样次数"这个数的意义正是"观察到几次变化"。
    ⚠️ 读数为 0/负 = **不知道**(该源没给),此时**什么都不动** —— 别把"没数据"
    写成"读数掉到 0"(那会伪造出一个暴跌信号)。
    """
    from config.settings import get_settings

    st = settings or get_settings()
    if not bool(getattr(st, "wechat_read_trend_enabled", True)):
        return str(getattr(art, "trend_flag", "") or "")
    new = _i(read_num)
    if new <= 0:
        return str(getattr(art, "trend_flag", "") or "")      # 不知道 ⇒ 不动
    now = now or datetime.now()
    first = _i(getattr(art, "first_read_num", 0))
    if first <= 0:                                            # 第一次采样 ⇒ 立基线
        art.first_read_num = new
        art.first_read_at = now
        art.traffic_at = now
        art.read_num = new
        art.sample_count = max(1, _i(getattr(art, "sample_count", 0)))
        return ""
    cur = _i(getattr(art, "read_num", 0))
    if new == cur:                                            # 没变 ⇒ 不算一次采样
        return str(getattr(art, "trend_flag", "") or "")
    art.read_num = new                                        # ★ 最新值一定要回写
    art.traffic_at = now
    art.sample_count = _i(getattr(art, "sample_count", 0)) + 1
    art.trend_flag = _judge(art, now, settings=st)
    return art.trend_flag


def _judge(art: Any, now: datetime, *, settings) -> str:
    """按「**采样前后两段的速率**」判一个标记。拿不准就返回空串(**不猜**)。

    ⚠️⚠️ **这里的第一版是错的,记下来免得改回去**:我原本拿
    `rate_recent = 增量/观察窗` 去比 `rate_total = 最新读数/发布以来`,
    想着"最近比历史平均快一倍就是加速"。**它结构上几乎不可能触发**:
    两者**共用分子**(都含最新读数),涨得越猛 `rate_total` 也一起膨胀;
    实测 `500→30000`(明显起量)算出来是 `2950/h vs 1500/h` —— 达不到 2×。
    更要命的是当"观察窗恰好是文章年龄一半"时,这个比值**数学上封顶就是 2**。

    ⇒ 改成**两段互不共享分子的比较**:

        采样前速率 = 基线读数 / (基线时刻 − 发布时刻)      ← 前一段的平均
        采样后速率 = 增量     / (现在   − 基线时刻)        ← 观察窗内的

    这样"涨得猛"才真的能被看出来(实测 `500→30000` ⇒ 前 50/h、后 2950/h ⇒ 加速 ✅),
    "慢下来"也判得对(`500→620` ⇒ 前 50/h、后 12/h ⇒ 回落 ✅)。
    """
    delta = _i(getattr(art, "read_num", 0)) - _i(getattr(art, "first_read_num", 0))
    if delta < int(getattr(settings, "wechat_read_trend_min_delta", 20) or 20):
        return ""                                             # 防噪 ②:增量太小
    base_t = getattr(art, "first_read_at", None)
    pub = getattr(art, "publish_at", None)
    if base_t is None or pub is None:
        return ""                                             # 缺时刻 ⇒ 算不出速率
    span_h = (now - base_t).total_seconds() / 3600.0
    if span_h < float(getattr(settings, "wechat_read_trend_min_span_h", 2.0) or 2.0):
        return ""                                             # 防噪 ①:观察窗太短
    pre_h = (base_t - pub).total_seconds() / 3600.0
    if pre_h < 1.0:
        return ""                # "基线时刻"几乎就是发布时刻 ⇒ 前一段没有可比速率
    rate_pre = _i(getattr(art, "first_read_num", 0)) / pre_h
    rate_recent = delta / span_h
    # ⚠️ 这里原本还有一条"`rate_pre <= 0` ⇒ 直接判加速"的分支。**它是死代码,已删**:
    #    `first_read_num == 0` 时 `observe` 走的是"立基线"那条路(根本不会进到这里),
    #    所以 `rate_pre` 必然 > 0。死代码留着最坏的地方是**看起来像在做一件事**
    #    (本仓的母题:别让"没生效"和"生效了"长得一样)。
    if rate_recent >= ACCEL * rate_pre:
        return FLAG_ACCEL
    if rate_recent <= FALL * rate_pre:
        return FLAG_FALL
    return ""


def rate_recent(art: Any) -> float | None:
    """观察窗内的增速(阅读/小时)。**算不出返回 `None`** —— 调用方按"不知道"显示,
    别拿 0 冒充(0 会被读成"完全不涨")。"""
    base_t = getattr(art, "first_read_at", None)
    last_t = getattr(art, "traffic_at", None)
    if base_t is None or last_t is None:
        return None
    span_h = (last_t - base_t).total_seconds() / 3600.0
    if span_h <= 0:
        return None
    delta = _i(getattr(art, "read_num", 0)) - _i(getattr(art, "first_read_num", 0))
    return delta / span_h
