"""跨平台资源热度(2026-10-02):抓**资源名**,网盘链从**资源库**里匹配。

**用户口径**:"小红书可以只抓取资源名称,然后网盘链接可以从资源库里面匹配,别的也可以按照这个模式来进行"。

**这条绕开了"平台上没有链"的死结**:小红书/快手/贴吧的内容里既没有口令、也没有盘链
(实测),但**平台上有没有链根本不重要** —— 只要**有人在推同一个资源**,而这个资源我们
**库里已经有链**(公众号/知乎/迅雷进来的),直接拿库里的用就行。

链路:

    资源库的资源名  →  挨个平台搜  →  统计"这个资源在那个平台有多少内容"
                    →  **回资源库匹配出可用链**(原链 + 我方链)  →  推卡片

**为什么搜索词一定匹配得回库**:词就是**从资源库取的**(`_keywords_from_library`)——
它本来就出自库里的文章标题,所以必然能检索回去(群组资源名不保证,所以只作热度参考)。

⚠️ **成本**:每个平台各开一次浏览器(一次几分钟),所以**平台数 × 关键词数都要克制**。
"""
from __future__ import annotations

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

# 平台 → 飞书板块(卡片推到哪儿;未配回落主群)
PLATFORMS = {
    "xiaohongshu": {"section": "xiaohongshu", "label": "小红书"},
    "kuaishou": {"section": "kuaishou", "label": "快手"},
    "tieba": {"section": "tieba", "label": "贴吧"},
    "douyin": {"section": "douhot", "label": "抖音"},
    "weibo": {"section": "weibo", "label": "微博"},
    "bilibili": {"section": "bilibili", "label": "B站"},
}


def platforms_of(settings) -> list[str]:
    """要探的平台(逗号分隔配置);不认识的名字忽略并告警,别让笔误停掉整轮。"""
    raw = str(getattr(settings, "presence_platforms", "") or "").split(",")
    out: list[str] = []
    for name in (x.strip() for x in raw):
        if not name:
            continue
        if name not in PLATFORMS:
            logger.warning("热度平台 `%s` 不认识,已忽略(可选:%s)", name, "/".join(PLATFORMS))
            continue
        if name not in out:
            out.append(name)
    return out


def library_names(session, user_id: int, top: int = 5) -> list[str]:
    """本轮要探的**资源名**:取自资源库(所以必然能匹配回去)。"""
    from app.services.cross_accounts import _keywords_from_library

    return _keywords_from_library(session, user_id, top)


def _library_link(session, user_id: int, name: str) -> dict:
    """**回资源库匹配**这个资源名 → 可用的链。

    返回 `{"pan_url", "my_link", "pan_type", "source", "titles"}`;匹配不到返回空 dict。
    公众号那条优先(带"多少号同发"的验证强度),发现链只补缺口 —— 与资源库同一套口径。
    """
    from app.services.resource_library import search_resources

    for r in search_resources(session, user_id, name, limit=5):
        return {"pan_url": r["pan_url"], "my_link": r["my_link"],
                "pan_type": r["pan_type"], "source": r["source"],
                "titles": r["titles"]}
    return {}


def probe(session, user_id: int, settings=None) -> dict:
    """按资源名探各平台 → 附库内链。返回 `{"status", "platforms", "items"}`。

    `items` 形如 `[{"name", "platform", "label", "count", "samples": [...], "link": {...}}]`,
    **只保留有内容命中的**(没命中的说明这个资源在该平台没人做,不必推)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services import mediacrawler_source as mc

    names = library_names(session, user_id, int(getattr(settings, "presence_names", 3) or 3))
    plats = platforms_of(settings)
    if not names or not plats:
        return {"status": "empty", "platforms": 0, "items": []}
    ok, why = mc.available()
    if not ok:
        logger.info("跨平台热度跳过:MediaCrawler 不可用(%s)", why)
        return {"status": "no_tool", "platforms": 0, "items": []}

    items: list[dict] = []
    for plat in plats:
        rows = mc.crawl(plat, names)                     # 一次吃整个词表,别逐词开浏览器
        if not rows:
            continue                                     # 该平台没登录/没结果 → 跳过,不影响其余
        by_name: dict[str, list[dict]] = {}
        for r in rows:
            kw = str(r.get("keyword") or "").strip()
            if kw in names:
                by_name.setdefault(kw, []).append(r)
        for name, hits in by_name.items():
            items.append({"name": name, "platform": plat,
                          "label": PLATFORMS[plat]["label"], "count": len(hits),
                          "samples": [(h.get("snippet") or "")[:60] for h in hits[:2]],
                          "link": _library_link(session, user_id, name)})
        logger.info("跨平台热度:%s 命中资源 %d 个", plat, len(by_name))
    return {"status": "ok", "platforms": len(plats), "items": items}


def push_items(items: list[dict], settings) -> bool:
    """推飞书(**各平台自己的群**,未配回落主群)。版式与其他推送一致:四列网格。

    列 = 资源 / 平台 / 内容数 / **可用链**(库里匹配出来的,点开即用)。
    """
    if not items:
        return False
    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient, webhook_for

    brand = (getattr(settings, "brand_name", "") or "").strip()
    sent_any = False
    for plat in sorted({it["platform"] for it in items}):
        group = [it for it in items if it["platform"] == plat]
        hook = webhook_for(settings, PLATFORMS.get(plat, {}).get("section", ""))
        if not hook:
            continue
        label = PLATFORMS.get(plat, {}).get("label", plat)
        elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
            f"{label}上也在推的资源 **{len(group)}** 个(链接取自我方资源库,点开即用):"}},
            _col_set_row([("**资源**", 5), ("**平台**", 3), ("**内容数**", 2),
                          ("**可用链**", 4)], grey=True)]
        for it in group:
            link = it.get("link") or {}
            url = link.get("my_link") or link.get("pan_url") or ""
            cell = f"[▶ 打开]({_md_safe(url)})" if url else "—(库内暂无链)"
            elements.append(_col_set_row([
                (_md_safe(strip_others(it["name"])), 5),
                (_md_safe(it["label"]), 3),
                (str(it["count"]), 2),
                (cell, 4)]))
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "wathet", "title": {"tag": "plain_text",
                                                           "content": f"🔎 {brand + ' · ' if brand else ''}"
                                                                      f"{label}资源热度 · {len(group)} 个"}},
                "elements": elements}
        try:
            if FeishuClient(hook, getattr(settings, "feishu_secret", "")).send_card(card):
                sent_any = True
        except Exception:  # noqa: BLE001 - 单平台推送失败不影响其余
            logger.exception("跨平台热度推送失败(%s)", plat)
    return sent_any


def presence_tick(settings=None) -> int:
    """定时:按资源名探各平台 → 匹配库内链 → 推卡片。返回推送的资源条目数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "presence_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            from app.services.tenant_base import _record_run

            try:
                out = probe(db, uid, settings=settings)
                total += len(out.get("items") or [])
                if out.get("items"):
                    push_items(out["items"], settings)
                _record_run(db, uid, "resource_presence", "success",
                            f"平台{out.get('platforms', 0)} 命中{len(out.get('items') or [])}")
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("跨平台热度失败 user=%s", uid)
                _record_run(db, uid, "resource_presence", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
