# -*- coding: utf-8 -*-
"""资源库(2026-10-01):对标号全历史盘链的检索、共振榜与画像。

**为什么**:选题建议只匹配近 72h 对标文章,大量历史资源被浪费——
回灌实证"同一盘链被 13 个号同发"(花少2人格测试)这类就是金矿,却沉在历史里。
本模块把 `wechat_pan_links` + `wechat_articles` 变成可检索的资源库:

- `search_resources`   关键词(热点词/品类词)检索资源,带验证强度(多少号发过);
- `resonance_resources` 高共振资源榜(同链被 ≥N 号同发 = 需求被反复验证);
- `resource_profile`    单资源画像(多少号/时间线/我方链是否已转存可直接复用)。

全部数据来自已采集的对标信息——**不碰第三方资源聚合**(规避版权风险面)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import WechatArticle, WechatPanLink
from app.utils import get_logger

logger = get_logger(__name__)


def _pan_kind(url: str) -> str:
    if "pan.quark.cn" in (url or ""):
        return "夸克"
    if "pan.baidu.com" in (url or ""):
        return "百度"
    if "drive.uc.cn" in (url or ""):
        return "UC"
    if "pan.xunlei.com" in (url or ""):
        return "迅雷"
    return "其他"


def _my_link_of(session: Session, user_id: int, pan_url: str) -> str:
    """该链在本租户是否已有我方转存链(可直接复用)。"""
    blobs = session.scalars(select(WechatArticle.my_pan_urls).join(
        WechatPanLink, WechatPanLink.article_id == WechatArticle.id).where(
        WechatPanLink.pan_url == pan_url, WechatArticle.user_id == user_id,
        WechatArticle.my_pan_urls.isnot(None), WechatArticle.my_pan_urls != ""
    ).limit(3)).all()
    for blob in blobs:
        for line in (blob or "").splitlines():
            line = line.strip()
            if line.startswith("https://"):
                return line
    return ""


def _rows_to_resources(session: Session, user_id: int, rows, with_my: bool = True) -> list[dict]:
    """聚合行 → 资源条目(统一结构;titles 取样例)。"""
    out = []
    for pan_url, accounts, first_seen, last_seen in rows:
        sample = session.scalars(select(WechatArticle.title).join(
            WechatPanLink, WechatPanLink.article_id == WechatArticle.id).where(
            WechatPanLink.pan_url == pan_url, WechatPanLink.user_id == user_id,
        ).order_by(WechatArticle.created_at.desc()).limit(2)).all()
        out.append({
            "pan_url": pan_url, "pan_type": _pan_kind(pan_url),
            "accounts": int(accounts or 0),
            "titles": [str(t or "")[:60] for t in sample],
            "first_seen": first_seen.isoformat(sep=" ", timespec="seconds") if first_seen else "",
            "last_seen": last_seen.isoformat(sep=" ", timespec="seconds") if last_seen else "",
            "my_link": _my_link_of(session, user_id, pan_url) if with_my else "",
        })
    return out


def search_resources(session: Session, user_id: int, query: str,
                     days: int = 90, limit: int = 20) -> list[dict]:
    """关键词检索资源库:匹配文章标题,聚合到盘链级,按验证强度(号数)排序。

    命中规则:标题包含查询词(中文无分隔,直接子串;两字以下不检索防噪声)。
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    rows = session.execute(
        select(WechatPanLink.pan_url,
               func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id,
               WechatArticle.created_at >= cutoff,
               WechatArticle.title.contains(q))
        .group_by(WechatPanLink.pan_url)
        .order_by(func.count(func.distinct(WechatArticle.author)).desc())
        .limit(limit)).all()
    return _rows_to_resources(session, user_id, rows)


def resonance_resources(session: Session, user_id: int, days: int = 30,
                        min_accounts: int = 2, limit: int = 15) -> list[dict]:
    """高共振资源榜:同链被 ≥min_accounts 个号同发——需求被反复验证的金矿。"""
    cutoff = datetime.now() - timedelta(days=days)
    rows = session.execute(
        select(WechatPanLink.pan_url,
               func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id, WechatArticle.created_at >= cutoff)
        .group_by(WechatPanLink.pan_url)
        .having(func.count(func.distinct(WechatArticle.author)) >= min_accounts)
        .order_by(func.count(func.distinct(WechatArticle.author)).desc())
        .limit(limit)).all()
    return _rows_to_resources(session, user_id, rows)


def resource_profile(session: Session, user_id: int, pan_url: str) -> dict | None:
    """单资源画像:号数/时间线/我方链。未知链返回 None。"""
    row = session.execute(
        select(func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatPanLink, WechatPanLink.article_id == WechatArticle.id)
        .where(WechatPanLink.user_id == user_id, WechatPanLink.pan_url == pan_url)).one()
    if not row or not row[0]:
        return None
    res = _rows_to_resources(session, user_id, [(pan_url, row[0], row[1], row[2])])
    return res[0] if res else None


def library_summary(session: Session, user_id: int, days: int = 90) -> dict:
    """资源库概览(CLI/前端展示用):总链数/多号验证数。

    `days` 是**同一个时间窗口**,两个数字必须同口径:此前 `cutoff` 算了却没用,
    `total_links` 统计的是**全历史**、而 `multi_account` 是近 days 天 —— 前端同屏
    显示两个不同尺度的数字,看着像"总链 500 条,其中多号验证只有 12 条",其实是拿
    90 天的分子比全历史的分母(2026-10-01 审查发现)。
    """
    cutoff = datetime.now() - timedelta(days=days)
    total = session.scalar(select(func.count(func.distinct(WechatPanLink.pan_url))).where(
        WechatPanLink.user_id == user_id,
        WechatPanLink.created_at >= cutoff)) or 0
    verified = len(resonance_resources(session, user_id, days=days, min_accounts=2, limit=10000))
    return {"total_links": int(total), "multi_account": int(verified), "days": days}


def detect_viral_resources(session: Session, user_id: int, hours: int = 24,
                           min_accounts: int = 3, limit: int = 5) -> list[dict]:
    """资源级爆款检测(2026-10-01):近 N 小时被 ≥min_accounts 个号**新同发**的盘链。

    与资源库(全历史沉淀)的区别:只看**近期新增**——多号突然同发同一资源是爆款苗头
    (回灌实证:花少2人格测试被 13 个号同发),人还没反应过来时跟进去,转存扩散最强。
    返回按号数降序的资源列表(含我方链状态)。
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = session.execute(
        select(WechatPanLink.pan_url,
               func.count(func.distinct(WechatArticle.author)),
               func.min(WechatArticle.created_at), func.max(WechatArticle.created_at))
        .join(WechatArticle, WechatArticle.id == WechatPanLink.article_id)
        .where(WechatPanLink.user_id == user_id, WechatArticle.created_at >= cutoff)
        .group_by(WechatPanLink.pan_url)
        .having(func.count(func.distinct(WechatArticle.author)) >= min_accounts)
        .order_by(func.count(func.distinct(WechatArticle.author)).desc())
        .limit(limit)).all()
    return _rows_to_resources(session, user_id, rows)


def push_viral_alerts(session: Session, user_id: int, settings=None) -> int:
    """爆款资源预警 → 飞书(每小时 tick 调用;每链 48h 冷却;已转存/未转存分别提示)。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "")
    if not hook:
        return 0
    sent = 0
    for r in detect_viral_resources(session, user_id):
        if not feishu_alert_gate(session, user_id, "viral_res",
                                 f"viral:{r['pan_url']}", 48, "爆款资源预警"):
            continue
        title = r["titles"][0] if r["titles"] else "(无标题)"
        lines = [f"🔥 爆款资源在疯传(近24h 已被 {r['accounts']} 个号同发)",
                 f"📄 {title}",
                 f"🧭 {r['pan_type']} · 最近 {r['last_seen'][:10]}"]
        if r["my_link"]:
            lines.append("📦 我方链接(已转存,点开即用):")
            lines.append(r["my_link"])
        else:
            lines.append("⏳ 尚未转存——建议尽快转存跟上,这种多号同发的需求扩散最强")
        if FeishuClient(hook, settings.feishu_secret).send(chr(10).join(lines)):
            sent += 1
    return sent
