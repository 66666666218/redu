# -*- coding: utf-8 -*-
"""用**历史数据**给 AI 筛选提供先验(2026-10-29)。

用户口径:「可以根据之前的数据来进行策略决策」,并反问「现在的数据不够用吗,
已经一个多月的数据了」。

## 实测(2026-10-04 ~ 10-29,1056 条带口令线索、692 条搬成)

| 信号 | 结论 |
|---|---|
| **赛道** | **软件 92.5% / 问卷 90.9% / 资料 86.9% / 其他 76.4% / 影视 56.5% / 小游戏 54.3%** |
| **新鲜度** | 近 7 天搬成 **77.2%**,更早 **61.1%** ⇒ 7 天窗口确实提高转化 |
| 同口令被几条视频推 | **不单调**(1 条 66%、2~7 条 52~67%、8~10 条反而更低)⇒ 只能当提示特征,不能当硬判据 |
| 转发数 | 弱相关(0 条 71.6%、100+ 69.4%) |

⇒ 把**赛道历史搬成率**当先验喂给模型,让它按数据评分而不是凭感觉;
⚠️ 「同资源条数」仍然保留,但**只作为特征**,不写进硬规则。
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: `(user_id, 小时)` → 统计结果。一小时一刷:数据变化慢,而每张卡都查一次没必要。
_CACHE: dict[tuple[int, str], dict[str, dict]] = {}


def niche_rates(session, user_id: int, days: int = 30) -> dict[str, dict]:
    """近 N 天各赛道的**搬成率**:`{赛道: {"n": 条数, "moved": 搬成, "rate": 0~1}}`。

    ⚠️ "搬成" = 线索的 `our_url` 非空(我们真的把它转存成了自己的链)。
    这不是"客户价值"的完美代理,但它是**唯一有全量数据**的结果指标。
    """
    if session is None:
        return {}
    from sqlalchemy import select

    from app.db.models import DouyinLead
    from app.services.category_topics import niche_of

    key = (int(user_id), datetime.now().strftime("%Y-%m-%d %H"))
    if key in _CACHE:
        return _CACHE[key]
    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = session.execute(select(DouyinLead.title, DouyinLead.mark, DouyinLead.our_url).where(
        DouyinLead.user_id == user_id,
        DouyinLead.found_at >= cutoff,
        DouyinLead.mark != "")).all()
    agg: dict[str, list[int]] = {}
    for title, mark, our in rows:
        k = niche_of(f"{title or ''} {mark or ''}")
        a = agg.setdefault(k, [0, 0])
        a[0] += 1
        a[1] += 1 if str(our or "").strip() else 0
    out = {k: {"n": n, "moved": m, "rate": (m / n if n else 0.0)}
           for k, (n, m) in agg.items()}
    _CACHE.clear()               # 只留当前小时,别让旧统计长期占着
    _CACHE[key] = out
    return out


def prompt_note(rates: dict[str, dict], min_n: int = 10) -> str:
    """把统计变成一句喂给模型的话;样本太小的赛道不写(免得模型被噪声带偏)。"""
    items = [(k, v) for k, v in (rates or {}).items() if int(v.get("n") or 0) >= int(min_n)]
    if not items:
        return ""
    items.sort(key=lambda x: -float(x[1].get("rate") or 0))
    body = " ".join(f"{k}{float(v['rate']) * 100:.0f}%" for k, v in items)
    return (f"历史经验(近 30 天真实搬成率,样本≥{min_n}):{body}。"
            "评分时请参考:搬成率高的赛道,同等内容可以给更高分;低的赛道要求更硬的证据(具体资源+多号在发)。")
