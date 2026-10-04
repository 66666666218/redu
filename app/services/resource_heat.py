"""资源热度:同一资源被**多个发布者**发布时,按**聚合**判断(2026-10-04 用户口径)。

用户原话:「同一个资源会多个人发布……可以根据多人发布的情况以及数据来结合判断,
**而不是随机一个文章或者作品来判断**」。

聚合口径(全文见 `doc/全自动化与转化率口径-方案-2026-10-04.md` §二·五):

    资源身份   有链 → `pan_url`(现成) / 无链 → 规范化资源名
    发布者     `DISTINCT author`
    样本数 N   |发布者|                              ← **必须带出来**
    曝光总量   Σ(每个发布者取最大指标)                ← **同一人多次发布取最大,不求和**
    预估拉新   走 `conversion` 单一事实源

⚠️ **三条纪律**(都有测试钉住):
  ⒜ **同一发布者多次发布取"最大"不求和** —— 同一个人反复发同一资源,受众高度重叠,
     求和会把它刷成"爆款";
  ⒝ **`N` 必须带出来**:`N=1` 要明写"**单样本,可信度低**",**不许和 N=5 算成同一件事**;
  ⒞ 聚合的是"**同一个资源**",不是"同一篇文章" —— 这正是用户点名要避免的。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import WechatArticle, WechatPanLink
from app.utils import get_logger

logger = get_logger(__name__)

# ≥ 这个发布者数才算"被反复验证"(与 `resource_library.resonance_resources` 的
# `min_accounts` 同一含义 —— 那个是筛选门槛,这里是**可信度标注**的依据)。
CONFIDENCE_MIN_PUBLISHERS = 2


def label_confidence(publishers: int) -> tuple[str, bool]:
    """发布者数 → `(可信度标注, 是否够可信)`。

    ⚠️ **`N=1` 要说出来** —— 单篇的热度可能只是那个号自己的粉丝结构,
    与"5 个号都在发"完全不是一回事。
    """
    if publishers >= CONFIDENCE_MIN_PUBLISHERS:
        return f"{publishers} 个发布者(被反复验证)", True
    if publishers == 1:
        return "单样本(可信度低)", False
    return "无有效样本", False


def build_heat(pan_url: str, publishers: int, exposure: int,
               platform: str = "wechat", extras: dict | None = None) -> dict[str, Any]:
    """**纯函数**:把聚合结果变成带预估拉新与可信度的一条资源热度。

    曝光→预估这一步**必须**走 `conversion`(别在这儿再写一遍系数,那是"两处真相"的开始)。
    """
    from app.services import conversion

    est = conversion.estimate(platform, {"read_num": exposure} if platform == "wechat"
                              else {"share_count": exposure})
    conf, ok = label_confidence(publishers)
    out: dict[str, Any] = {
        "pan_url": pan_url, "publishers": publishers, "exposure": exposure,
        "confidence": conf, "trusted": ok,
        "estimate": est["estimate"], "estimate_detail": conversion.describe(est),
        "heat": conversion.heat_level(est["estimate"]),
        # ⚠️ 曝光是**聚合出来的** ⇒ 把口径写进结果,别让下游以为它是单篇的数字
        "exposure_note": "跨发布者求和(同一发布者多次发布取其最大值)",
    }
    if extras:
        out.update(extras)
    return out


def wechat_resource_heat(session: Session, user_id: int, days: int = 7,
                         limit: int = 50) -> list[dict[str, Any]]:
    """公众号侧:**按 `pan_url` 聚合**的资源热度榜(近 `days` 天)。

    两级聚合(缺一不可):
      内层 `GROUP BY (pan_url, author)` 取**每个发布者的最大阅读数**
      —— 这一步就是"同一人多次发布取最大"的落地;
      外层 `GROUP BY pan_url` 求 `COUNT(*)`(= 发布者数)与 `SUM(...)`(= 曝光总量)。

    ⚠️ 只看**有盘链**的文章:没有链的连不成"同一个资源"(那要走名字身份,见 §二·五)。
    """
    since = datetime.now() - timedelta(days=days)
    inner = (
        select(WechatPanLink.pan_url.label("pan_url"),
               WechatArticle.author.label("author"),
               func.max(WechatArticle.read_num).label("max_read"))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id,
               WechatArticle.created_at >= since,
               WechatArticle.author != "")
        .group_by(WechatPanLink.pan_url, WechatArticle.author)
        .subquery()
    )
    outer = (
        select(inner.c.pan_url,
               func.count().label("publishers"),      # 子查询每行 = 一个(资源,发布者)
               func.sum(inner.c.max_read).label("exposure"))
        .group_by(inner.c.pan_url)
        .order_by(func.sum(inner.c.max_read).desc())
        .limit(limit)
    )
    out: list[dict[str, Any]] = []
    for pan_url, publishers, exposure in session.execute(outer).all():
        out.append(build_heat(str(pan_url), int(publishers or 0), int(exposure or 0)))
    return out


def summary_line(heats: list[dict[str, Any]]) -> str:
    """一句话概括(管理群汇报用):几个资源、几个够可信、预估合计多少。

    ⚠️ 合计只把**够可信**的算进去 —— 单样本混进去会让总数虚高。
    """
    if not heats:
        return "近 7 天没有可聚合的资源(需要文章里带盘链)"
    trusted = [h for h in heats if h["trusted"]]
    total = sum(int(h["estimate"] or 0) for h in trusted)
    return (f"资源 {len(heats)} 条,其中**被反复验证的 {len(trusted)} 条**;"
            f"这几条预估拉新合计 {total}(口径:跨发布者聚合曝光 × 平台系数)")
