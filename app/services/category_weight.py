"""品类权重:用**结算回来的实测数据**自动调选题权重(2026-10-04 用户口径)。

数据源:`hotspot_agent.settle_suggestions` 聚合出的 `by_category`
(每品类的 `repost_gain` 盘链扩散 + `reads_gain` 阅读数×30% 的预估拉新量)。

**它做什么**:把"哪类真赚"折算成一个**倍数**,乘进选题机会分
(`hotspot_agent._opportunity` 的公式),让验证过的品类更容易被推到前面。

⚠️ **三条护栏**(本仓对"自动调权重"本来就存疑 —— `DouyinLead.share_count` 的 docstring
   写着"在拿到几周真实偏差之前**不用它自动调权重**",所以这里宁可保守):

  ⒜ **样本不够就不动**:某品类结算样本 < `MIN_SAMPLES` 时**不出现**在结果里(调用方按 1.0 处理)。
     **绝不拿 1 条样本去调全盘。**
  ⒝ **倍数有上下限**(`LO..HI`):一次调太猛会让整个选题被单一品类带偏。
  ⒞ **无数据 = 空 dict**:公式与今天**完全一致**(向后兼容,不需要迁移)。

⚠️ 口径混用要说清:`repost_gain` 是"资源配置被疯转"(全网扩散),
`reads_gain` 是"预估触达"。**两者不是一回事**,所以这里用**两条独立的依据**
(`reposts` / `reads`)算两个倍数,谁都没数据就退回 1.0 —— 而不是把它们加在一起
(加在一起等于把两个量纲不同的数相加,那是本仓反复讲过的"数字会骗人")。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import HotspotSuggestion
from app.utils import get_logger

logger = get_logger(__name__)

MIN_SAMPLES = 3        # 少于这么多条结算样本,该品类**不参与调权**
LO, HI = 0.5, 2.0      # 倍数上下限


def clamp(ratio: float) -> float:
    """把倍数夹进 `[LO, HI]`(并挡掉 NaN/负数这类脏值)。"""
    try:
        v = float(ratio)
    except (TypeError, ValueError):
        return 1.0
    if v != v or v <= 0:          # NaN / 非正数
        return 1.0
    return max(LO, min(HI, v))


def ratio_of(value: float, baseline: float) -> float:
    """相对倍数 = 该品类均值 / 全盘均值。**全盘均值为 0 时返回 1.0**(没法比,别乱调)。"""
    if baseline <= 0:
        return 1.0
    return clamp(value / baseline)


def multipliers(db: Session, user_id: int, days: int = 60) -> dict[str, dict]:
    """每品类的权重倍数。**样本不够的品类不出现**(调用方按 1.0 处理)。

    返回 `{品类: {"reads": 倍数, "reposts": 倍数, "n": 样本数}}`。

    `n` 要带出去 —— 让下游/界面能说清"这个倍数是从几条样本学来的"。
    """
    since = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(HotspotSuggestion.category,
               func.count(HotspotSuggestion.id),
               func.sum(HotspotSuggestion.reads_gain),
               func.sum(HotspotSuggestion.repost_gain))
        .where(HotspotSuggestion.user_id == user_id,
               HotspotSuggestion.settled_at.is_not(None),
               HotspotSuggestion.settled_at >= since)
        .group_by(HotspotSuggestion.category)).all()
    cats = [(str(c or "未分类"), int(n or 0), float(rg or 0), float(rp or 0))
            for c, n, rg, rp in rows if int(n or 0) >= MIN_SAMPLES]
    if not cats:
        return {}
    # 全盘均值(只拿够样本的品类算 —— 否则 1 条样本的品类会把基准拉歪)
    tot_n = sum(n for _, n, _, _ in cats) or 1
    base_reads = sum(rg for _, _, rg, _ in cats) / tot_n
    base_reposts = sum(rp for _, _, _, rp in cats) / tot_n
    out: dict[str, dict] = {}
    for cat, n, rg, rp in cats:
        out[cat] = {"reads": ratio_of(rg / n, base_reads),
                    "reposts": ratio_of(rp / n, base_reposts),
                    "n": n}
    return out


def multiplier_for(mults: dict[str, dict], category: str) -> float:
    """取某品类的综合倍数 = 两条依据的**几何平均**(谁缺就用谁;都缺 → 1.0)。

    ⚠️ 用几何平均而不是算术平均:倍数天然是乘性的,算术平均会让"一个 2.0 一个 0.5"
    被算成 1.25(其实是"抵消"),几何平均才给出 1.0 这个正确结论。
    """
    m = mults.get(str(category or ""))
    if not m:
        return 1.0
    vals = [float(m.get(k) or 1.0) for k in ("reads", "reposts")]
    vals = [v for v in vals if v > 0]
    if not vals:
        return 1.0
    prod = 1.0
    for v in vals:
        prod *= v
    return clamp(prod ** (1.0 / len(vals)))
