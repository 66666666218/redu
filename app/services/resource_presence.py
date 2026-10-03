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

import time

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


# 走**公开 API、不开浏览器**的平台(见 `_crawl_platform`)。B站 的 wbi 签名是公开算法、
# 本地纯 Python 可算,匿名即可搜 —— 而 MediaCrawler 抓 B站 反而起不来(它去找 Chrome)。
API_PLATFORMS = frozenset({"bilibili"})

# B站 风控**按频率**(连发即 -352),所以逐词之间要隔开(与 cross_accounts 同一套口径)
_BILI_GAP = 4.0


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


def _crawl_platform(plat: str, names: list[str]) -> list[dict]:
    """抓一个平台的内容 —— **按平台选路**。

    · `bilibili`:**公开 API**(wbi 签名是公开算法、本地纯 Python 可算,匿名即可,不开浏览器)。
      实测搜「网盘资源」20 条标题正是「【原版】火影忍者720集网盘资源!!未删减版」这类 ——
      而 MediaCrawler 抓 B站 反而起不来(`Chromium distribution 'chrome' is not found`)。
      风控**按频率**(连发即 -352),所以逐词之间要隔开。
    · 其余:MediaCrawler(要浏览器、要登录态)。
    """
    if plat == "bilibili":
        from app.services.cross_accounts import search_bilibili_videos

        out: list[dict] = []
        for i, kw in enumerate(names):
            if i:
                time.sleep(_BILI_GAP)
            out.extend(search_bilibili_videos(kw))
        return out
    from app.services import mediacrawler_source as mc

    return mc.crawl(plat, names)


def probe(session, user_id: int, settings=None, platforms: list[str] | None = None) -> dict:
    """按资源名探各平台 → 附库内链。返回 `{"status", "platforms", "items"}`。

    `items` 形如 `[{"name", "platform", "label", "count", "samples": [...], "link": {...}}]`,
    **只保留有内容命中的**(没命中的说明这个资源在该平台没人做,不必推)。

    `platforms` 传了就用它;不传就读 `presence_platforms`。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services import mediacrawler_source as mc

    names = library_names(session, user_id, int(getattr(settings, "presence_names", 3) or 3))
    plats = platforms if platforms is not None else platforms_of(settings)
    if not names or not plats:
        return {"status": "empty", "platforms": 0, "items": []}
    # MediaCrawler 只在**真的要开浏览器**的平台才需要 —— 全是 B站 时它装没装都无所谓
    ok, why = mc.available() if any(p not in API_PLATFORMS for p in plats) else (True, "ok")
    if not ok:
        logger.info("跨平台热度跳过:MediaCrawler 不可用(%s)", why)
        return {"status": "no_tool", "platforms": 0, "items": []}

    items: list[dict] = []
    from app.services.cross_accounts import SearchSourceError
    from app.services.mediacrawler_source import MediaCrawlerError

    tried = 0
    failed: list[str] = []
    last_err = ""
    for plat in plats:
        tried += 1
        try:
            rows = _crawl_platform(plat, names)          # 一次吃整个词表,别逐词开浏览器
        except (MediaCrawlerError, SearchSourceError) as exc:
            # 单平台硬失败(多半是那个平台没登录)不该拖垮整轮 —— 但要**记名**:
            # ① 全平台都失败 → 冒泡,别让"一个都没开起来"记成 success(空);
            # ② 只有部分失败 → 名字进返回值,由 `presence_tick` 写进运行记录
            #    (否则"小红书成了、快手挂了"查不出来 —— 静默的部分失败)。
            failed.append(plat)
            last_err = f"{plat}: {exc}"
            logger.warning("跨平台热度:%s 抓取失败(%s)", plat, exc)
            continue
        if not rows:
            continue                                     # 该平台没结果 → 跳过,不影响其余
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
    if tried and len(failed) == tried:
        # 一个平台都没开起来 → 这不是"没热度",是链路坏了,必须让上层记 failed
        raise MediaCrawlerError(f"{tried} 个平台全部抓取失败:{last_err}")
    return {"status": "ok", "platforms": len(plats), "items": items, "failed": failed}


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
        # 内容卡:未配专属群的平台回落**客户主群**是对的(同上,管理群只接维护信息)
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


def presence_tick(settings=None, platforms: list[str] | None = None) -> int:
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
                out = probe(db, uid, settings=settings, platforms=platforms)
                total += len(out.get("items") or [])
                if out.get("items"):
                    push_items(out["items"], settings)
                # ⚠️ **把失败平台写进运行记录**:`probe` 只在"全平台都失败"时才抛错,
                # 所以"小红书成了、快手挂了"这种**部分失败**本来查不出来。
                failed = out.get("failed") or []
                note = f"平台{out.get('platforms', 0)} 命中{len(out.get('items') or [])}"
                if failed:
                    note += f" 失败:{','.join(failed)}"
                _record_run(db, uid, "resource_presence", "success", note)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("跨平台热度失败 user=%s", uid)
                _record_run(db, uid, "resource_presence", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
