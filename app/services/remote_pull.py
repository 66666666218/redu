# -*- coding: utf-8 -*-
"""把**远程的热榜与事件层**拉回本机(2026-11-18,档4)。

## 它补的是哪个盲区

两侧分工(`SCHEDULER_ROLE`)是:**热榜采集/事件归并跑远程(hotspot),客户推送跑本机(wechat)**。
于是本机对"平台在热什么、归并出了哪些事件"**完全瞎**:

实测(2026-11-18):
  · 远程 `hotspot_events` **42179** 行 / `event_memberships` **49779** 行,
    本机这两张表 **0 行**(`event_assign` 的 role 是 `hotspot`,本机永不跑);
  · 远程 `hot_source_items` 近 24h **33140** 行 / 45 个源,本机**只有 7102 行 `bili-pan`**
    (那是我们自己往远程推的,不是热榜);
  · 结果:前端「热点事件」页永远空、「多平台热榜」页只有 B站对标号一项。

## 为什么只拉这两样,不拉原始热榜表

⚠️⚠️ **这是本模块最要紧的一条**:`weibo_hot_items` / `baidu_hot_items` / `douhot_words`
在本地有**别的消费者** —— `focus_alert`、`early_agent`、`cross_platform` 都读它们,
而 `focus_alert` 在**本机也会被调**(`wechat_collect_tick` 每轮都调,见 scheduler:327)。

实测两端的飞书 webhook 指纹**完全相同**(主群 ec5294a31a、admin 67b9b17a68、
weibo/baidu/douhot 专属群各自一致)⇒ 一旦把原始热榜拉回本机,「同一关键词在
≥2 板块共振」就会被**本机再判一次**,往**同一个群**推第二遍 —— 纯重复告警。

所以拉取范围**只到事件层为止**:
  · `hotspot_events` + `event_memberships` —— 归并结果(只读展示,本机不跑归并);
  · `hot_source_items`(**排除 `bili-pan`**)—— 只取**每个源的最新一轮**,
    够前端「多平台热榜」页展示;不取历史,因为没有本机消费者需要它。

## 三条纪律

1. **只读远程、只写本机** —— 与 `quark_link_pull` 同一契约,`remote_sync` 的单向契约不改;
2. **幂等**:事件按 `(user_id, norm_title)` 认行、成员按 `(user_id, board, norm_title)` 认行、
   热榜按 `(source, captured_at)` 整批判重 —— 重跑只是白跑一遍;
3. **绝不静默**:拉到 0 条也要把计数写进 `runs.detail`(「这半小时没新数据」与
   「这条路根本没接上」必须能分开看,本仓反复栽在这一条上)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import EventMembership, HotSourceItem, HotspotEvent
from app.utils import get_logger

logger = get_logger(__name__)

#: 只拉远程的哪个用户。远程 5 个启用用户里 **只有 user 1 是主持久账号**,
#: user 2~5 是后来注册的空壳(实测它们的事件**多平台数永远是 0** —— 只采了百度一个源,
#: 且是同一份热榜的重复采集)⇒ 拉过来只会污染本机视图。
REMOTE_USER_ID = 1

#: 热榜**跳过**的源:这些行是**本机推上去的**,再从远程拉回来就是回环。
#: (`remote_sync` 第 ⑥ 段推 `source='bili-pan'`,本机已有原件。)
_SKIP_SOURCES = ("bili-pan",)

_EVENT_COLS = ("primary_title", "norm_title", "platforms", "platform_count", "first_seen",
               "last_seen", "peak_value", "peak_at", "sample_count", "status", "ended_at",
               "reappear_count", "last_growth")
_MEMBER_COLS = ("board", "norm_title", "latest_value", "last_seen")


def _fold_events(ev_dicts: list[dict]) -> list[dict]:
    """把同一个 `norm_title` 的多条远程行折成一条。

    ⚠️ 远程按**时间窗**分行、不按标题折:同一个词可以先是 `ended`、过两天又 `active`
    (实测 2026-11-18:近 7 天 3858 行里有 3 组 —— 「意大利vs法国」「联盟音乐赛季」
    「活塞vs奇才」)。本地按 `norm_title` 唯一认行,若不先折,这两条会**轮流**把
    `status` 写来写去,同一批数据跑两遍 `updated` 在 0 和 6 之间抖 —— 幂等就破了。

    折的规则与"合并到本地行"完全一致:时间取更超前、峰值取更大、平台取并集,
    `status`/`ended_at` 取 `last_seen` **最靠前**那条(生命周期以最新一次出现为准)。
    被折掉的行,它的 `remote_id` 必须仍能映射到本地 id(成员表按它指过来),
    所以整组 id 收在 `remote_ids` 里。
    """
    folded: dict[str, dict] = {}
    for src in ev_dicts:
        norm = str(src.get("norm_title") or "")[:255]
        if not norm:
            continue
        cur = folded.get(norm)
        if cur is None:
            item = dict(src)
            item["norm_title"] = norm
            item["remote_ids"] = [int(src.get("remote_id") or 0)]
            folded[norm] = item
            continue
        cur["remote_ids"].append(int(src.get("remote_id") or 0))
        if src.get("last_seen") and (cur.get("last_seen") is None
                                     or src["last_seen"] > cur["last_seen"]):
            cur["last_seen"], cur["status"] = src["last_seen"], src.get("status") or cur.get("status")
            cur["ended_at"] = src.get("ended_at")
        if src.get("first_seen") and (cur.get("first_seen") is None
                                      or src["first_seen"] < cur["first_seen"]):
            cur["first_seen"] = src["first_seen"]
        if float(src.get("peak_value") or 0) > float(cur.get("peak_value") or 0):
            cur["peak_value"], cur["peak_at"] = src["peak_value"], src.get("peak_at")
        if int(src.get("sample_count") or 0) > int(cur.get("sample_count") or 0):
            cur["sample_count"] = src["sample_count"]
        plats = {p for p in json.loads(str(cur.get("platforms") or "[]") or "[]") if p}
        plats |= {p for p in json.loads(str(src.get("platforms") or "[]") or "[]") if p}
        cur["platforms"], cur["platform_count"] = json.dumps(sorted(plats)), len(plats)
    return list(folded.values())


def _remote_query(settings, sql: str, params: dict | None = None) -> list:
    """只读一次远程(走 SSH 隧道或直连,与 `remote_sync._engine` 同一条路)。"""
    from app.services.remote_sync import _engine

    eng = _engine("", settings)
    try:
        with eng.connect() as conn:
            return conn.execute(text(sql), params or {}).all()
    finally:
        eng.dispose()
        tr = getattr(eng, "_rs_transport", None)
        if tr is not None:
            try:
                tr.close()          # ⚠️ SSH 通道要关,否则每轮泄漏一条(同 remote_sync)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 事件层

def pull_events(session: Session, settings, days: int = 7,
                rows: tuple[list[dict], list[dict]] | None = None) -> dict:
    """把远程近 N 天的事件与成员镜像到本机。返回 `{"events", "created", "updated", "members"}`。

    `rows` 是给测试用的接缝(`(events, members)` 两个 dict 列表),不传就真读远程 ——
    与 `quark_link_pull.pull_once(remote_rows=...)` 同一手法:判据本身可单测。
    """
    if rows is None:
        since = datetime.now() - timedelta(days=int(days))
        ev_rows = _remote_query(
            settings,
            "SELECT id, " + ", ".join(_EVENT_COLS) + " FROM hotspot_events "
            "WHERE user_id = :u AND last_seen >= :s", {"u": REMOTE_USER_ID, "s": since})
        ev_dicts = [dict(zip(("remote_id",) + _EVENT_COLS, r)) for r in ev_rows]
        remote_ids = [e["remote_id"] for e in ev_dicts]
        mb_dicts: list[dict] = []
        for i in range(0, len(remote_ids), 500):        # IN 占位符有限,分批
            chunk = remote_ids[i:i + 500]
            ph = ", ".join(f":i{j}" for j in range(len(chunk)))
            params = {f"i{j}": v for j, v in enumerate(chunk)}
            mrows = _remote_query(
                settings,
                f"SELECT event_id, {', '.join(_MEMBER_COLS)} FROM event_memberships "
                f"WHERE user_id = :u AND event_id IN ({ph})",
                {"u": REMOTE_USER_ID, **params})
            mb_dicts += [dict(zip(("remote_event_id",) + _MEMBER_COLS, r)) for r in mrows]
        rows = (ev_dicts, mb_dicts)
    ev_dicts, mb_dicts = rows

    ev_dicts = _fold_events(ev_dicts)

    #: 本地已有事件按 norm_title 建索引(**不用远程 id**:两边各自自增过,
    #: 那边的 id 落到这边来会指错行 —— 与 `remote_sync` 的 `_ARTICLE_COLS` 同一条纪律)。
    existing = {e.norm_title: e for e in session.scalars(
        select(HotspotEvent).where(HotspotEvent.user_id == REMOTE_USER_ID)).all()}
    created = updated = 0
    id_map: dict[int, int] = {}
    for src in ev_dicts:
        norm = str(src.get("norm_title") or "")[:255]
        if not norm:
            continue
        ev = existing.get(norm)
        if ev is None:
            ev = HotspotEvent(user_id=REMOTE_USER_ID,
                              primary_title=str(src.get("primary_title") or "")[:255],
                              norm_title=norm,
                              platforms=str(src.get("platforms") or ""),
                              platform_count=int(src.get("platform_count") or 1),
                              first_seen=src["first_seen"], last_seen=src["last_seen"],
                              peak_value=float(src.get("peak_value") or 0),
                              peak_at=src.get("peak_at"),
                              sample_count=int(src.get("sample_count") or 0),
                              status=str(src.get("status") or "active"),
                              ended_at=src.get("ended_at"),
                              reappear_count=int(src.get("reappear_count") or 0),
                              last_growth=src.get("last_growth"))
            session.add(ev)
            session.flush()             # 要 id 给成员表用
            existing[norm] = ev
            created += 1
        else:
            #: ⚠️ **取"更超前"的那个,不做覆盖也不做相加**:
            #: `sample_count` 相加会在每轮拉取时翻倍(它记的是**快照条数**,不是拉取次数);
            #: 而"远程比本地旧"时覆盖会把本地更新的事实冲掉(本机手动归属过就会有)。
            changed = False
            if src["last_seen"] and (ev.last_seen is None or src["last_seen"] > ev.last_seen):
                ev.last_seen, changed = src["last_seen"], True
            if src["first_seen"] and (ev.first_seen is None or src["first_seen"] < ev.first_seen):
                ev.first_seen, changed = src["first_seen"], True
            if float(src.get("peak_value") or 0) > (ev.peak_value or 0):
                ev.peak_value = float(src["peak_value"])
                ev.peak_at = src.get("peak_at")
                changed = True
            if int(src.get("sample_count") or 0) > (ev.sample_count or 0):
                ev.sample_count, changed = int(src["sample_count"]), True
            plats = {p for p in json.loads(ev.platforms or "[]") if p}
            remote_plats = {p for p in json.loads(str(src.get("platforms") or "[]") or "[]") if p}
            if remote_plats - plats:
                plats |= remote_plats
                ev.platforms = json.dumps(sorted(plats))
                ev.platform_count = len(plats)
                changed = True
            #: 生命周期**以远程为准**:归并只跑在远程,本地没有判断依据;
            #: 本机被手动归属误置成 ended 的,下一轮会被远程的真实状态纠正回来。
            if src.get("status") and ev.status != src["status"]:
                ev.status, changed = str(src["status"]), True
                ev.ended_at = src.get("ended_at")
            if changed:
                updated += 1
        for rid in src.get("remote_ids") or [int(src.get("remote_id") or 0)]:
            id_map[int(rid)] = ev.id     #: 被折掉的远程 id 也要能指到同一条本地行
    session.flush()

    have_m = {(m.user_id, m.board, m.norm_title) for m in session.scalars(
        select(EventMembership).where(EventMembership.user_id == REMOTE_USER_ID)).all()}
    members = 0
    for src in mb_dicts:
        key = (REMOTE_USER_ID, str(src.get("board") or ""), str(src.get("norm_title") or "")[:255])
        local_eid = id_map.get(int(src.get("remote_event_id") or 0))
        if not key[1] or not key[2] or local_eid is None:
            continue
        if key in have_m:
            continue
        session.add(EventMembership(user_id=REMOTE_USER_ID, board=key[1], norm_title=key[2],
                                    event_id=local_eid,
                                    latest_value=float(src.get("latest_value") or 0),
                                    last_seen=src.get("last_seen") or datetime.now()))
        have_m.add(key)
        members += 1
    session.commit()
    return {"events": len(ev_dicts), "created": created, "updated": updated,
            "members": members}


# ---------------------------------------------------------------- 多平台热榜

def pull_hot_sources(session: Session, settings, rows: list[dict] | None = None) -> dict:
    """把远程**每个源最新一轮**的多平台热榜镜像到本机。返回 `{"sources", "rows", "inserted"}`。

    只取最新一轮(不是时间窗)的理由:本机消费者只有前端「多平台热榜」页,
    它读的正是 `GROUP BY source` 的 `MAX(captured_at)` 那一批。
    取历史只会让每轮搬运量从 ~1.3k 涨到 33k(实测),而没有任何人看。
    """
    if rows is None:
        raw = _remote_query(
            settings,
            "SELECT i.source, i.rank, i.title, i.url, i.extra, i.captured_at, i.published_at "
            "FROM hot_source_items i JOIN ("
            "  SELECT source, MAX(captured_at) AS ts FROM hot_source_items "
            "  WHERE user_id = :u AND captured_at >= :s GROUP BY source"
            ") m ON m.source = i.source AND m.ts = i.captured_at "
            "WHERE i.user_id = :u",
            {"u": REMOTE_USER_ID,
             #: 最新一轮若超过 6h 没动,说明那个源早就停了 —— 别把"停了三天的榜单"
             #: 当"现在在热什么"展示(前端按 `captured_at` 算"多久前",但先挡一道)。
             "s": datetime.now() - timedelta(hours=6)})
        rows = [{"source": r[0], "rank": r[1], "title": r[2], "url": r[3],
                 "extra": r[4], "captured_at": r[5], "published_at": r[6]} for r in raw]

    rows = [r for r in rows if str(r.get("source") or "") not in _SKIP_SOURCES]
    if not rows:
        return {"sources": 0, "rows": 0, "inserted": 0}

    #: 幂等判据 = **整批判重**:同一 `(source, captured_at)` 的批次只要本机已有行就跳过。
    #: 不能逐行按标题查 —— 同一个源同一批里标题可能重复(不同榜位同一条),
    #: 而且逐行查是 N 次往返(这本是"最新一轮"的全部意义所在)。
    batches = {(str(r["source"]), r["captured_at"]) for r in rows}
    have: set[tuple[str, datetime]] = set()
    for src in {b[0] for b in batches}:
        for ts in session.scalars(select(HotSourceItem.captured_at).where(
                HotSourceItem.user_id == REMOTE_USER_ID, HotSourceItem.source == src)).all():
            have.add((src, ts))
    todo = batches - have
    if not todo:
        return {"sources": len({b[0] for b in batches}), "rows": len(rows), "inserted": 0}
    inserted = 0
    for r in rows:
        key = (str(r["source"]), r["captured_at"])
        if key not in todo:
            continue
        session.add(HotSourceItem(user_id=REMOTE_USER_ID, source=key[0],
                                  rank=int(r.get("rank") or 0),
                                  title=str(r.get("title") or "")[:500],
                                  url=str(r.get("url") or "")[:700],
                                  extra=str(r.get("extra") or "")[:200],
                                  captured_at=r["captured_at"],
                                  published_at=r.get("published_at")))
        inserted += 1
    session.commit()
    return {"sources": len({b[0] for b in batches}), "rows": len(rows), "inserted": inserted}


# ---------------------------------------------------------------- 调度入口

def remote_pull_tick(settings=None) -> int:
    """定时:把远程的热榜与事件层拉回本机。返回写入行数合计(**失败不抛**)。

    ⚠️ 没配 `remote_ssh_host`/`remote_db_url` 时直接返回 0 且**不记运行记录** ——
    与 `remote_sync_tick` 同一条纪律:运行记录里不该出现"success 但什么也没干"的空轮。
    """
    from config.settings import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "remote_ssh_host", "") and not getattr(settings, "remote_db_url", ""):
        return 0
    from app.db import get_session_local
    from app.services.tenant_base import _record_run

    db = get_session_local()()
    try:
        ev = pull_events(db, settings,
                         days=int(getattr(settings, "remote_pull_event_days", 7) or 7))
        hot = pull_hot_sources(db, settings)
        total = int(ev["created"] + ev["members"] + hot["inserted"])
        #: ⚠️ **四个数都要写出来**(不是只写合计):「事件 0」与「成员 0」意味着**不同的断点** ——
        #: 事件 0 是远程没归并或窗口没数据,成员 0 是 id 映射断了。合并成一个数字就分不清。
        #: 而 `热榜` 后面跟的是"这次看到多少个源",**源数掉下来**先于"行数为 0"暴露上游变少。
        _record_run(db, REMOTE_USER_ID, "remote_pull", "success",
                    f"事件新增{ev['created']}/更新{ev['updated']} 成员新增{ev['members']} "
                    f"热榜{hot['inserted']}行({hot['sources']}源)")
        db.commit()
        return total
    except Exception as exc:  # noqa: BLE001 - 远程任何问题都不该影响本机
        db.rollback()
        logger.warning("远程拉取失败(不影响本机):%s: %s", type(exc).__name__, str(exc)[:200])
        try:
            _record_run(db, REMOTE_USER_ID, "remote_pull", "failed", str(exc)[:200])
            db.commit()
        except Exception:  # noqa: BLE001 - 记运行记录本身再失败就只能放弃了
            db.rollback()
        return 0
    finally:
        db.close()


__all__ = ["pull_events", "pull_hot_sources", "remote_pull_tick", "REMOTE_USER_ID"]
