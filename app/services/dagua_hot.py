"""微博/百度热搜 → **大瓜事件词**(2026-11-28)。

## 为什么要有它

用户口径:「**微博热搜和百度热搜也加进去,之前的羊麦穗这里面都有过排名**」——
这两个榜是大瓜事件的**一手来源**,而我们此前只用抖音热点宝(`douhot`)。

## 两个接口都是**匿名公开**的(2026-11-28 实测)

- 微博热搜 `weibo.com/ajax/side/hotSearch`:51 条,**不需要 Cookie**;
- 百度热搜 `top.baidu.com/api/board`:30 条,**不需要登录**。

⇒ 采集成本 ≈ 0(两个请求),可以高频跑。**但只入库、不推任何提醒** ——
这条线只服务大瓜专轮的取词,不该在群里制造热搜噪音。
⚠️ `run_weibo` / `run_baidu` 是**板块采集**(判涨、告警、实时推送),它们的
`user_schedules` 目前是关的;本模块**不碰**那条路,也不复用它的告警。

## 为什么是"入库 + 时间窗"而不是"当场取一次词"

大瓜专轮每天只跑一次(`douyin_leads_dagua_cron`,默认 09:24)。如果取词时才现采,
就只能看见 **09:24 那一刻**的榜 —— 昨天夜里上榜的瓜就漏了。
所以这里按 30 分钟一轮**持续入库**,专轮再读「近 N 小时**上过榜**」的词。
用户在意的「羊麦穗有过排名」正是这种"上过榜、但未必此刻在榜"的情形。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

#: 取词时最多回看多少行(两张表合计)—— 24h × 48 轮 × 50 条 ≈ 2400,留足余量
_RECENT_SCAN = 4000


def fetch_all(settings: object) -> dict[str, list[dict]]:
    """采两路热搜 → `{"weibo": [{title,heat,rank}], "baidu": [...]}`。

    **单路失败不影响另一路**(两路是独立来源,一个挂了另一个照样有用);
    两路都挂时返回 `{}`,由调用方决定怎么记。
    """
    out: dict[str, list[dict]] = {}
    try:
        from app.services import collector

        items = collector.fetch_hot_search(settings)
        out["weibo"] = [{"title": it.title, "heat": it.heat, "rank": it.rank}
                        for it in items]
    except Exception as exc:  # noqa: BLE001 - 单路失败不拖垮另一路
        logger.warning("大瓜热榜:微博热搜取词失败(%s):%s",
                       type(exc).__name__, str(exc)[:120])
    try:
        from app.services import baidu

        items = baidu.fetch_hot(settings)
        out["baidu"] = [{"title": it.title, "heat": it.heat, "rank": it.rank}
                        for it in items]
    except Exception as exc:  # noqa: BLE001
        logger.warning("大瓜热榜:百度热搜取词失败(%s):%s",
                       type(exc).__name__, str(exc)[:120])
    return out


def store(session, user_id: int, batch: dict[str, list[dict]]) -> int:
    """把一批热搜写进 `weibo_hot_items` / `baidu_hot_items`。返回写入条数。

    语义与 `run_weibo` 一致:**每轮全量快照**(不按 title 去重,靠 `captured_at` 区分轮次),
    保留清理由 `db.maintenance.cleanup_old_data` 统一治理。
    ⚠️ **空标题跳过**:表宽 500,超长截断 —— 与 `run_weibo` 同一条纪律(不截会 1406 整批失败)。
    """
    from app.db.models import BaiduHotItem, WeiboHotItem

    now = datetime.now()
    n = 0
    for it in batch.get("weibo") or []:
        title = str(it.get("title") or "").strip()[:500]
        if not title:
            continue
        session.add(WeiboHotItem(user_id=user_id, title=title,
                                 heat=int(it.get("heat") or 0),
                                 rank=int(it.get("rank") or 0),
                                 captured_at=now))
        n += 1
    for it in batch.get("baidu") or []:
        title = str(it.get("title") or "").strip()[:500]
        if not title:
            continue
        session.add(BaiduHotItem(user_id=user_id, title=title,
                                 heat=int(it.get("heat") or 0),
                                 rank=int(it.get("rank") or 0),
                                 url=str(it.get("url") or "")[:500],
                                 captured_at=now))
        n += 1
    session.commit()
    return n


def recent_dagua_words(session, user_id: int, *, hours: int = 24,
                       limit: int = 5) -> list[str]:
    """近 `hours` 小时**上过榜**的词条 → 按「大瓜相关度 + 榜上名次」取前 `limit` 个。

    ⚠️⚠️ **不做 `dagua_score > 0` 的硬过滤** —— 这是实测教训(2026-11-28):
    微博 51 条里只有 **2 条**、百度 30 条里只有 **1 条**命中事件词,而
    「范丞丞你要毁了李晨吗」「于东来现身深圳迪厅舞姿火辣」这些**明显的瓜得分是 0**
    (事件词表认不出它们;用户举的「羊麦穗」同理 —— **光有人名的词条一个事件词都没有**)。
    硬筛恰好会把用户点名要的那类漏掉。
    正确做法是**只排序不过滤**:命中事件词的优先,同分按榜上名次(越靠前越热);
    **真正的筛子是搜索本身** —— 搜不出网盘/口令的词自然沉掉(与本仓既有纪律一致)。

    ⚠️ 两榜**交替取**(微博/百度轮流),否则娱乐条目多的微博会把百度挤光。
    ⚠️ 同一词条在窗口内多次上榜只算一次,名次取**最好**的那次(rank 最小)。
    ⚠️ 读库不打网络:采集是 `dagua_hot_tick` 的事,这里只回答"最近有什么"。
    """
    from app.db.models import BaiduHotItem, WeiboHotItem
    from app.services.category_topics import dagua_score

    since = datetime.now() - timedelta(hours=max(1, int(hours or 24)))
    ranked: dict[str, list[str]] = {}
    for name, model in (("weibo", WeiboHotItem), ("baidu", BaiduHotItem)):
        best: dict[str, tuple[int, int]] = {}      # 词 → (最高分, 最好名次)
        stmt = (select(model.title, model.rank)
                .where(model.user_id == user_id, model.captured_at >= since)
                .limit(_RECENT_SCAN))
        for raw, raw_rank in session.execute(stmt).all():
            t = str(raw or "").strip()
            if not t:
                continue
            r = int(raw_rank or 999)
            prev = best.get(t)
            if prev is None or r < prev[1]:
                best[t] = (dagua_score(t), r)
        #: 命中事件词多的优先,同分看榜上名次,再同则**短的优先**(更像可搜的事件名)
        ranked[name] = [w for w, _ in sorted(
            best.items(), key=lambda kv: (-kv[1][0], kv[1][1], len(kv[0])))]

    out: list[str] = []
    seen: set[str] = set()
    idx = {n: 0 for n in ranked}
    order = [n for n in ("weibo", "baidu") if ranked[n]]
    cap = max(0, int(limit or 0))
    while len(out) < cap and order:
        added = False
        for name in order:
            lst, i = ranked[name], idx[name]
            while i < len(lst) and lst[i] in seen:
                i += 1
            idx[name] = i
            if i < len(lst):
                seen.add(lst[i])
                out.append(lst[i])
                idx[name] = i + 1
                added = True
                if len(out) >= cap:
                    break
        if not added:
            break
    return out


def dagua_hot_tick(settings=None) -> int:
    """调度入口:采两路热搜 → 按用户入库。返回写入条数。

    ⚠️ `dagua_hot_cron` 为空串 = **关掉**(与 `douyin_leads_dagua_cron` 同一条纪律:
    开关判在作业里,不判在注册处 —— 改了配置不用重启就能生效)。
    ⚠️ **不推送任何东西**:这条线只供 `hot_seed_words` 取词。
    """
    from app.db import get_session_local
    from app.db.models import User

    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not str(getattr(settings, "dagua_hot_cron", "") or "").strip():
        return 0
    batch = fetch_all(settings)
    if not batch:
        logger.warning("大瓜热榜:两路热搜都没取到,本轮跳过")
        return 0
    db = get_session_local()()
    try:
        n = 0
        for uid in db.scalars(select(User.id)).all():
            n += store(db, int(uid), batch)
        logger.info("大瓜热榜:微博 %d 条 / 百度 %d 条,写入 %d 行",
                    len(batch.get("weibo") or []), len(batch.get("baidu") or []), n)
        return n
    finally:
        db.close()
