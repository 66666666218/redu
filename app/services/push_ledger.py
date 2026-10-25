# -*- coding: utf-8 -*-
"""推送台账:某个群那天**真的收到了什么** —— 日结/周结的单一数据源(2026-10-16)。

## 为什么要有它

用户口径:「**有的客户反馈有点信息太多看不过来,能否加上每日总结昨天…再加一个周结…
每个板块都配置上**」。要按板块做总结,先得能回答「**昨天那个板块推出去了什么**」。
本仓三处现成数据都答不了:

  · **业务表**(文章/线索/热榜…)只记「**采到了**」 —— 采到但没发出去的
    (被去重掉、那个板块没配群、卡片发送失败)也在表里 ⇒ 拿它当日结,会把
    **客户从没见过的东西**算成「发过了」(本仓最忌讳的那类"看起来做了");
  · `resource_presence`(小红书/快手/贴吧/B站/知乎)那张卡**从来不落库**
    ⇒ 那 5 个板块在库里**什么都没有**;
  · `notification_logs` 只记 `notify_incident` 的**事件级告警**(采集失败/停摆),
    记的是「哪里坏了」,不是「发了什么」。

⇒ 于是加这一张**只增不改**的台账:**推卡的那一刻**顺手记一行。

## 纪律(每条都对着本仓踩过的坑)

1. **一次推送一行,写的时候不聚合**(同一资源推了 3 轮 = 3 行)—— 聚合放到读的时候,
   窗口计数与「谁被反复推」才有干净语义(见 `window()` 的 `pushes`)。
2. **记不上不许挡住推送**:台账是**旁路**(推卡才是主业)⇒ 写失败只 warning +
   用 SAVEPOINT 兜住,**绝不**让一次台账写入把推送事务连坐。
   ⚠️ 但**不许静默** —— 失败必须留日志,否则「今天没内容」与「台账没写进去」
   在日结上长得一模一样。
3. **一条坏不牵连这一批的其余条目**:逐条 try,坏的记 warning 跳过。
4. 时间戳一律取 `datetime.now()`(**本地**),窗口也是本地零点 ——
   与运行记录/周报同一口径(远程那份 MySQL 的 `NOW()` 是 UTC,这里是本机进程,别混)。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 各推送点 item 的**形状本来就不同**,但它们**本来就带着这些信息**。
#: 与其在台账里再造一套 DTO(每个推送点都要适配一遍),不如把已知的键**按优先级**认下来。
_TITLE_KEYS = ("title", "name", "resource", "work", "word")
_KEY_KEYS = ("item_key", "key", "uid", "aweme_id", "note_id", "id")
_URL_KEYS = ("url", "origin_url", "source_url", "work_url")
_PAN_KEYS = ("my_link", "our_url", "pan_url", "share_url")
_AUTHOR_KEYS = ("author", "bench_author", "nickname")
#: 有些推送点把互动数**摊平**在 item 上(`douyin_leads` 的 `share_count` 就是),一并收进 metrics
_FLAT_METRIC_KEYS = ("play_count", "liked_count", "collected_count", "comment_count",
                     "share_count", "read_num")

#: 各键的取值长度上限(表字段就这么宽;超了要**截断**而不是让 INSERT 抛)
_LIMITS = {"section": 32, "item_key": 255, "title": 255, "url": 500, "pan_url": 500,
           "pan_type": 16, "author": 64, "metrics": 255}


def _first(d: dict, keys: tuple[str, ...]) -> str:
    """按优先级取第一个**非空**值(空串/None 都算没给)。"""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _norm_item(it: dict, section: str) -> dict[str, Any] | None:
    """一条 item → 台账行(认不出任何东西时返回 `None`,由调用方跳过)。"""
    from app.services.douyin_leads import _pan_name      # 盘名的**单一出处**(别抄第二份)

    title = _first(it, _TITLE_KEYS)
    url = _first(it, _URL_KEYS)
    #: ⚠️ **`resource_presence` 那条链的原帖链在 `sample_urls`(list)里** ——
    #: 它没有 `url` 这个键。2026-10-16 拿真形状渲染预览时**当场抓到**:不认它,
    #: 日结卡上「对标作品」整列恒 `—`(而数据一直都在)。
    #: 只取**能打开的第一条**;`sample_urls` 里可能有空串(源没给链)。
    if not url and isinstance(it.get("sample_urls"), (list, tuple)):
        url = next((str(u).strip() for u in it["sample_urls"] if str(u or "").strip()), "")
    pan_url = _first(it, _PAN_KEYS)
    #: ⚠️ presence 那种把链藏在 `link: {...}` 里的形状(它每行都这么给)
    link = it.get("link") if isinstance(it.get("link"), dict) else {}
    if link:
        pan_url = pan_url or _first(link, _PAN_KEYS)
        url = url or _first(link, _URL_KEYS)
    if not title and not url and not pan_url:
        return None                       #: 什么都没有 ⇒ 这行记了也没法在日结里显示
    item_key = _first(it, _KEY_KEYS) or title or url
    #: 热度:优先现成的 `metrics` dict,再把**摊平**在 item 上的互动数补进去
    raw = it.get("metrics") if isinstance(it.get("metrics"), dict) else {}
    metrics = {k: int(v) for k, v in raw.items() if isinstance(v, (int, float)) and int(v) > 0}
    for k in _FLAT_METRIC_KEYS:
        v = it.get(k)
        if isinstance(v, (int, float)) and int(v) > 0 and k not in metrics:
            metrics[k] = int(v)
    mj = json.dumps(metrics, ensure_ascii=False) if metrics else ""
    row = {
        "section": section[: _LIMITS["section"]],
        "item_key": item_key[: _LIMITS["item_key"]],
        "title": title[: _LIMITS["title"]],
        "url": url[: _LIMITS["url"]],
        "pan_url": pan_url[: _LIMITS["pan_url"]],
        "pan_type": (_pan_name(pan_url) or "")[: _LIMITS["pan_type"]],
        "author": _first(it, _AUTHOR_KEYS)[: _LIMITS["author"]],
        "metrics": mj[: _LIMITS["metrics"]],
    }
    return row


def record(session: Session, user_id: int, section: str, items: list[dict],
           when: datetime | None = None) -> int:
    """把「这一轮**真的推出去**的东西」记进台账。返回写入条数。

    ⚠️ **由推送点自己调用**(它手上才有"推了哪几条");不要试图在 `FeishuClient`
    那一层做 —— 那里只有渲染好的卡片 JSON,从中反解出行结构比每个点加一行更脆。

    ⚠️ **写失败会让调用方的 session 进入「必须 rollback」状态**(锁冲突时)。
    本函数按纪律**不吞掉调用方未提交的数据**,所以这里只撤销保存点、返回 0;
    **调用方必须在继续用这个 session 之前 `rollback()`** —— 监听轮已按此修
    (`_listen_round` 的补推兜底、`_push_listen` 的落库前自愈),见 2026-10-19 的事故。
    """
    from app.db.models import PushLedger
    from app.db.tx import savepoint

    if not items or not section:
        return 0
    ts = when or datetime.now()
    rows: list[PushLedger] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            norm = _norm_item(it, section)
        except Exception as exc:  # noqa: BLE001 - 单条坏不许牵连整批
            logger.warning("推送台账:某条条目解析失败已跳过(%s): %s",
                           type(exc).__name__, str(exc)[:100])
            continue
        if norm:
            rows.append(PushLedger(user_id=user_id, pushed_at=ts, **norm))
    if not rows:
        return 0
    try:
        #: SAVEPOINT:台账写失败**只撤销这几行**,不许把调用方尚未提交的推送数据一起抹掉
        with savepoint(session):
            session.add_all(rows)
            session.flush()
    except Exception as exc:  # noqa: BLE001 - 旁路失败只留痕,绝不挡住推送
        logger.warning("推送台账写入失败(%s: %s)—— 本批 %d 条未入账,"
                       "当天的日结会少这一部分(这是**已知会显形**的代价,不是静默)",
                       type(exc).__name__, str(exc)[:110], len(rows))
        return 0
    logger.info("推送台账:%s 记入 %d 条", section, len(rows))
    return len(rows)


def _heat(metrics: str) -> int:
    """热度排序用的**粗**代理值(只有排序用途,不做展示)。

    ⚠️ 把播放量与点赞相加在量纲上确实不严谨 —— 但这里只回答「**同一板块内谁更值得看**」,
    不对客户暴露这个数,也不参与任何结算(结算走 `conversion.estimate`)。
    """
    try:
        m = json.loads(metrics or "{}")
    except ValueError:
        return 0
    if not isinstance(m, dict):
        return 0
    try:
        return sum(int(v) for v in m.values() if isinstance(v, (int, float)))
    except (TypeError, ValueError):
        return 0


def window(session: Session, user_id: int, section: str, since: datetime,
           until: datetime, top: int = 0) -> dict:
    """读一个窗口 → 按**资源身份**聚合后的清单。

    返回 `{"rows", "resources", "repeats", "items": [...]}`:
      · `rows` = 窗口内**推送行数**(同一资源推 3 轮就是 3);
      · `resources` = **不同资源个数**(聚合后)—— 两者都给,是因为
        「推了 30 行」与「有 30 个资源」是**两件事**,而客户抱怨的正是前者;
      · `repeats` = 被推 ≥2 次的那种资源有几个(等于「刷屏了多少」);
      · `items`:有互动数据的按**热度降序**排在前;**没有数据的按首次出现顺序沉底**。
    """
    from app.db.models import PushLedger

    rows = session.scalars(
        select(PushLedger).where(
            PushLedger.user_id == user_id,
            PushLedger.section == section,
            PushLedger.pushed_at >= since,
            PushLedger.pushed_at < until,
        ).order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc())
    ).all()
    agg: dict[str, dict] = {}
    for r in rows:
        key = r.item_key or r.title or r.url or f"#{r.id}"
        g = agg.get(key)
        if g is None:
            agg[key] = {
                "item_key": r.item_key, "title": r.title, "url": r.url,
                "pan_url": r.pan_url, "pan_type": r.pan_type, "author": r.author,
                "metrics": r.metrics, "pushes": 1,
                "first": r.pushed_at, "last": r.pushed_at,
            }
            continue
        g["pushes"] += 1
        g["last"] = r.pushed_at
        #: 后来的行**只补空位**,不覆盖 —— 先出现的那份信息通常是"第一次发现它"时的原话
        for f in ("title", "url", "pan_url", "pan_type", "author", "metrics"):
            if not g.get(f) and r.__getattribute__(f):
                g[f] = r.__getattribute__(f)
    items = list(agg.values())          #: 字典保序 = **首次出现顺序**(rows 已按 pushed_at asc 取)
    #: ★★ 2026-10-26 **排序改成热度优先**(用户口径:「每日总结里按热度排序,让用户更好的观看」)。
    #: 原来是 `pushes` 主导 —— 但 `pushes` 是**我们自己推送了几次**(自家行为),
    #: 不是"这件事有多火";客户翻日结想看的是「昨天什么在火」,不是「我们重复推了谁」。
    #:
    #: ★ 同日订正(用户口径):**有数据的按热度排,没数据的按原顺序排**。
    #: 贴吧/部分源本来就没有点赞播放(`_heat` 恒 0)—— 把它们也丢进 heat 排序里比,
    #: 等于拿"没有的指标"编出一个次序(先被 `pushes` 决定、再被标题拼音决定),
    #: 那个次序**没有任何含义**。所以:热的排前面按热度降序;**没有数据的整体沉底,
    #: 内部保持首次出现的顺序**(谁先被发现谁在前)。
    hot = [g for g in items if _heat(str(g.get("metrics") or "")) > 0]
    cold = [g for g in items if _heat(str(g.get("metrics") or "")) <= 0]
    hot.sort(key=lambda g: (-_heat(str(g.get("metrics") or "")),
                            -int(g["pushes"]), str(g.get("title") or "")))
    items = hot + cold
    out = {"rows": len(rows), "resources": len(items),
           "repeats": sum(1 for g in items if int(g["pushes"]) > 1), "items": items}
    if top:
        out["items"] = items[:top]
    return out


def boards_with_content(session: Session, user_id: int, since: datetime,
                        until: datetime) -> dict[str, int]:
    """窗口内**有台账的板块** → 各有多少行。

    ⚠️ 日结的板块清单**从这里来**,不写死在配置里:`section` 取的是**落地的那个群**,
    所以「每个板块都配上」= 那个群有内容就自动有日结;没内容的日子**不发空卡**。
    """
    from app.db.models import PushLedger

    rows = session.execute(
        select(PushLedger.section, PushLedger.id).where(
            PushLedger.user_id == user_id,
            PushLedger.pushed_at >= since,
            PushLedger.pushed_at < until,
        )
    ).all()
    out: dict[str, int] = {}
    for sec, _ in rows:
        if sec:
            out[sec] = out.get(sec, 0) + 1
    return out
