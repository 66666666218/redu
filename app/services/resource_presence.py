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
    # ⚠️ **小红书改走"页面渲染"这条路**(2026-10-07):MediaCrawler 直连它的搜索接口
    # 回 `461 CAPTCHA`(`code 300011 检测到账号异常`),而上游"集成 xhshow 修好"那条路
    # 要升级整个 MediaCrawler,我们本地是**脱敏教学版**(许可证禁商用),不合适。
    # 页面路是我们在闲鱼上验证过的同一个套路:**让页面自己的 JS 带签名,只读 DOM**。
    # ⚠️ **它抛错必须冒泡**(`XhsPageError` 不是 `MediaCrawlerError`)—— 这里转成
    # `MediaCrawlerError` 让上层按"单平台硬失败"处理(记名 + 推告警),而不是静默跳过。
    if plat == "xiaohongshu":
        from app.services import xhs_page_source
        from app.services.mediacrawler_source import MediaCrawlerError

        try:
            return xhs_page_source.search(names)
        except xhs_page_source.XhsPageError as exc:
            raise MediaCrawlerError(f"xiaohongshu(页面路): {exc}") from exc
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


def browser_platforms_of(settings) -> list[str]:
    """**走浏览器的**平台(MediaCrawler)—— B站不在里面,它有自己的快节奏作业。

    ⚠️ **为什么要拆**(2026-10-07,用户口径「小红书/B站/贴吧能否跟抖音一样两小时一轮」):
    这四个平台的**成本差一个数量级**:
      · **B站**走公开 API(`API_PLATFORMS`),**不开浏览器、无风控**,加密到 2 小时几乎不要钱;
      · 小红书/快手/贴吧走 MediaCrawler,**每轮各开一次浏览器**(实测小红书单次 157 秒),
        一轮 5–8 分钟。加密到 2 小时 ⇒ 12 轮/天 ≈ **1~1.5 小时浏览器自动化 + 风控暴露 ×12**。
    ⇒ 拆成两条:B站每 2 小时;**那三个保持每天两轮**。
    """
    return [p for p in platforms_of(settings) if p not in API_PLATFORMS]


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
            # ⚠️⚠️ **部分失败也要有人知道**(2026-10-06):上面这条注释说的是"查不出来",
            # 而它只把平台名拼进运行记录的 detail —— **运行记录没人天天看**。
            # 实测代价:小红书登录态过期后**连挂 3 天**(10-04/05/06 各失败一次),
            # 最后是用户来问"群里的机器人都配置好了吗"才被发现。
            # 这一类(登录态失效)**等多久都不会自己好,要人扫码** ⇒ 归"需人工",推飞书;
            # 冷却去重由 `notify_incident` 管,不会刷屏。
            try:
                from app.services.alert_service import notify_incident

                _t = f"{type(exc).__name__}: {exc}"
                _need = any(m in _t for m in ("扫码", "登录", "qrcode", "超时"))
                _label = PLATFORMS.get(plat, {}).get("label", plat)
                # 平台 id 复用 `mediacrawler_source.PLATFORM_IDS`(单一事实源,别在这里抄一份)
                from app.services.mediacrawler_source import PLATFORM_IDS as _PIDS

                _pid = _PIDS.get(plat, plat)
                notify_incident(
                    session, user_id, "multiplatform",
                    f"🟠 跨平台热度采集失败:{_label}",
                    f"{_t[:160]}。" + (
                        f"**多半是登录态过期,需要扫码重登**:浏览器会弹二维码 —— 跑 "
                        f"`cd tools/MediaCrawler && .venv/Scripts/python.exe main.py "
                        f"--platform {_pid} --lt qrcode --type search` "
                        f"用 App 扫一下即可(登录态会缓存在 browser_data/,不用每次扫)。"
                        if _need else "该平台本轮跳过,其余平台照常。"),
                    settings=settings)
            except Exception:  # noqa: BLE001 - 告警失败绝不能影响采集
                logger.debug("跨平台热度失败告警推送失败", exc_info=True)
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


def transfer_missing_links(session, user_id: int, items: list[dict],
                           settings=None) -> dict:
    """把「**库里有这个资源、但我们还没有我方链**」的命中项**自动转存**成我方链。

    用户口径:「没有外链的平台,按名字调资源库配对 —— 配上了就**自动转存 + 推我们的链**」。

    ⚠️ **三条纪律**:
      ⒜ **已有我方链的不碰** —— 重复转存既占盘又白打接口;
      ⒝ **每轮有上限**(`presence_transfer_limit`,默认 3)—— 转存是**网络写操作**且**占盘**,
         一轮开几十个会把盘顶满(迅雷那边刚因空间不足整批停过);
      ⒞ **失败不改判** —— 转存失败只把原因记进 `link["transfer_error"]`,
         该条**照样带着原链推出去**;别让"没转成"变成"这条没热度"
         (与推送口径里"未搬要说明原因"同源)。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    limit = int(getattr(settings, "presence_transfer_limit", 3) or 0)
    from app.services.pan_discovery import transfer_pan_url

    done = ok = failed = skipped = 0
    for it in items:
        link = it.get("link") or {}
        if not link or link.get("my_link"):
            continue                                   # 没匹配 或 已经有我方链
        pan_url = str(link.get("pan_url") or "")
        if not pan_url:
            continue
        if done >= limit:
            skipped += 1
            link["transfer_error"] = "本轮转存额度已用完(下轮继续)"
            continue
        done += 1
        # ⚠️⚠️ **转存前先放掉写锁**(2026-10-06 审计逮到 —— 与 `pan_discovery` **同一个坑的第二条链**)。
        # `transfer_pan_url` 是**网络慢活**(每条 1–3 秒),而 SQLite 是**单写者**、别的作业
        # `busy_timeout` 只有 30 秒。这里的额度虽然只有 3 条(3–9 秒,通常饿不死别人),
        # 但 `transfer_pan_url` **内部还有重试/换盘**,一旦撞上就翻倍 —— 纪律不能靠"这次应该够快"。
        # 证据在 `pan_discovery`:13 条 × 1–3 秒 > 30 秒 ⇒ `11:34:30` 那批
        # 「作业心跳写入失败」正是它干的。**慢活(网络/浏览器/模拟器)一律不要在事务里做。**
        session.commit()
        # 把名字一起传进去:百度链的提取码常写在附近文字里(`transfer_pan_url` 会去找)
        res = transfer_pan_url(session, user_id, pan_url, settings,
                               snippet=str(it.get("name") or ""))
        if res.get("status") == "ok" and res.get("our_url"):
            link["my_link"] = res["our_url"]
            link["moved"] = True
            ok += 1
        else:
            link["transfer_error"] = str(res.get("message") or res.get("status") or "")[:120]
            failed += 1
    return {"attempted": done, "ok": ok, "failed": failed, "skipped": skipped}


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


def presence_bili_tick(settings=None) -> int:
    """**B站单独一条快节奏作业**(每 2 小时,2026-10-07)。

    只挑 `API_PLATFORMS` 里配了的平台。没配就返回 0(**不发请求**)——
    与 `presence_tick` 共用同一套采集/匹配/转存/推送,不另写一遍。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    plats = [p for p in platforms_of(settings) if p in API_PLATFORMS]
    if not plats:
        return 0
    return presence_tick(settings, platforms=plats)


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
                out = probe(db, uid, settings=settings,
                            platforms=(platforms if platforms is not None
                                       else browser_platforms_of(settings)))
                items = out.get("items") or []
                total += len(items)
                # **配上了就自动转存**(2026-10-04 用户口径)。放在 push 之前,
                # 这样卡片上带的就是**我方链**;转存失败也会把原因带进卡片(不静默)。
                moved = transfer_missing_links(db, uid, items, settings)
                if items:
                    push_items(items, settings)
                # ⚠️ **把失败平台写进运行记录**:`probe` 只在"全平台都失败"时才抛错,
                # 所以"小红书成了、快手挂了"这种**部分失败**本来查不出来。
                failed = out.get("failed") or []
                note = f"平台{out.get('platforms', 0)} 命中{len(items)}"
                if moved["attempted"]:
                    note += (f" 转存{moved['ok']}/{moved['attempted']}"
                             f"(失败{moved['failed']} 超额{moved['skipped']})")
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
