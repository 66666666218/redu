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
    """该链在本租户是否已有我方转存链(可直接复用)。

    两处都查:公众号链存 `WechatArticle.my_pan_urls`,**公开平台发现的**存
    `discovered_pan_links.our_url`(2026-10-02 并入)。
    """
    blobs = session.scalars(select(WechatArticle.my_pan_urls).join(
        WechatPanLink, WechatPanLink.article_id == WechatArticle.id).where(
        WechatPanLink.pan_url == pan_url, WechatArticle.user_id == user_id,
        WechatArticle.my_pan_urls.isnot(None), WechatArticle.my_pan_urls != ""
    ).limit(3)).all()
    for blob in blobs:
        for line in (blob or "").splitlines():
            link = _clean_link(line)
            if link:
                return link
    return _discovered_my_link(session, user_id, pan_url)


def _clean_link(line: str) -> str:
    """从一行里抠出**干净的 URL**。

    ⚠️ 历史数据常带尾巴:实测 `WechatArticle.my_pan_urls` 里存过
    `https://pan.quark.cn/s/xxx (自分享)` —— 整行拿去当链接**点不开**
    (2026-10-02 跨平台热度卡片里发现)。所以在**取用端**统一抠一次。
    """
    import re

    m = re.match(r"(https?://[^\s()（）【】、,，]+)", (line or "").strip())
    return m.group(1) if m else ""


def _discovered_my_link(session: Session, user_id: int, pan_url: str) -> str:
    """公开平台(知乎)发现的那条链,我方是否已转存。"""
    from app.db.models import DiscoveredPanLink

    return str(session.scalar(select(DiscoveredPanLink.our_url).where(
        DiscoveredPanLink.user_id == user_id, DiscoveredPanLink.origin_url == pan_url,
        DiscoveredPanLink.our_url != "").limit(1)) or "")


def _discovered_resources(session: Session, user_id: int, query: str = "",
                          days: int = 90, limit: int = 20) -> list[dict]:
    """**公开平台发现的**盘链(知乎等),结构对齐 `_rows_to_resources`。

    2026-10-02 并入资源库:知乎回答里直接贴的夸克/百度链,和公众号文章里的链**是同一类东西**
    (键都是"别人的原链"),所以按 `pan_url` 天然能合在一起看。
    `accounts` 记 1(一个来源),`source` 标出来源平台 —— 别和公众号的"多少号同发"混为一谈。
    """
    from app.db.models import DiscoveredPanLink

    stmt = select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id,
        DiscoveredPanLink.found_at >= datetime.now() - timedelta(days=days))
    if query:
        stmt = stmt.where(DiscoveredPanLink.title.contains(query))
    rows = session.scalars(stmt.order_by(DiscoveredPanLink.found_at.desc()).limit(limit)).all()
    label = {"zhihu": "知乎"}
    out = []
    for r in rows:
        ts = r.found_at.isoformat(sep=" ", timespec="seconds") if r.found_at else ""
        out.append({"pan_url": r.origin_url, "pan_type": _pan_kind(r.origin_url),
                    "accounts": 1, "titles": [str(r.title or "")[:60]] if r.title else [],
                    "first_seen": ts, "last_seen": ts, "my_link": str(r.our_url or ""),
                    "source": label.get(r.platform, r.platform or "发现"),
                    "author": str(r.author or "")})
    return out


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
            "source": "公众号",
        })
    return out


def _xunlei_resources(session: Session, user_id: int, query: str = "",
                      days: int = 90, limit: int = 20) -> list[dict]:
    """**我们自己迅雷盘里**的资源(口令转存 + 扫盘),结构对齐 `_rows_to_resources`。

    ⚠️ **这条出口此前根本不存在**(2026-10-03 全项目审查发现):`xunlei_resources` 有
    **两处写入**(`xunlei_kouling` 口令转存 / `xunlei_sync` 扫盘)、**零处读取**(除健康检查
    取个时间),而 `resource_library` 只合并公众号链与公开平台发现链 —— 于是
    **抖音口令搬进来的资源、扫盘扫出来的资源,进了库谁也看不见、presence 也匹配不到**,
    等于白搬。

    ⚠️ **形状差别要说清**:公众号/知乎那两条里 `pan_url` 是"**别人的原链**"、`my_link` 是
    "我们的";而这里是**我们自己盘里的东西**,没有"别人的原链"可记 —— 所以 `pan_url` 与
    `my_link` **都是我们那条分享链**,`source` 标 `迅雷盘` 让界面能一眼区分"这条已经是我们的了"。
    没有 `share_url` 的行**跳过**(库的用途是"给出可用链",没链的放进来只会干扰)。
    """
    from app.db.models import XunleiResource

    stmt = select(XunleiResource).where(XunleiResource.user_id == user_id,
                                       XunleiResource.share_url != "")
    if query:
        stmt = stmt.where(XunleiResource.name.contains(query))
    if days:
        stmt = stmt.where(XunleiResource.synced_at >= datetime.now() - timedelta(days=days))
    rows = session.scalars(stmt.order_by(XunleiResource.synced_at.desc()).limit(limit)).all()
    out: list[dict] = []
    for r in rows:
        url = str(r.share_url or "")
        ts = r.synced_at.isoformat(sep=" ", timespec="seconds") if r.synced_at else ""
        out.append({"pan_url": url, "pan_type": _pan_kind(url),
                    "accounts": 1,
                    "titles": [str(r.name or "")[:60]] if r.name else [],
                    "first_seen": ts, "last_seen": ts,
                    "my_link": url,                  # 本来就是我们自己的链
                    "source": "迅雷盘"})
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
    ours = _rows_to_resources(session, user_id, rows)
    # **并入公开平台发现的链**(2026-10-02):同一条链可能既被公众号发过、又被知乎贴过 ——
    # 以**公众号那条为准**(它带"多少号同发"这个更强的信号),只补公众号没有的。
    seen = {r["pan_url"] for r in ours}
    extra = [d for d in _discovered_resources(session, user_id, q, days, limit)
             if d["pan_url"] not in seen]
    # **并入我们自己迅雷盘里的资源**(2026-10-03):口令转存 + 扫盘进来的那些此前**完全没有出口**,
    # 补上以后 presence「名字型」也能匹配到它们(否则"搬进来了但用不上")。
    seen |= {d["pan_url"] for d in extra}
    mine = [x for x in _xunlei_resources(session, user_id, q, days, limit)
            if x["pan_url"] not in seen]
    return (ours + extra + mine)[:limit]


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
        # 公众号里没有 → 看是不是**公开平台发现的**那条链
        found = _discovered_resources(session, user_id, limit=200)
        for d in found:
            if d["pan_url"] == pan_url:
                return d
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
    discovered = len(_discovered_resources(session, user_id, days=days, limit=10000))
    return {"total_links": int(total), "multi_account": int(verified),
            "discovered": discovered, "days": days}


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
