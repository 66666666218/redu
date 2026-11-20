# -*- coding: utf-8 -*-
"""同资源「**推手数突增**」提醒(2026-11-20) —— 「这个资源突然有更多人在推了」。

## 它补的是哪个盲区

在此之前全仓**没有任何一条**按"推它的人变多"触发的提醒:

| 现有机制 | 它回答的问题 | 为什么不够 |
|---|---|---|
| `push_hot_rise_pct` 去重涨幅 | 同一资源**流量**涨了吗 | 只管"要不要重复推",**从不告警**;而且按点赞/播放涨 30% 判,不是按人 |
| `focus_res`(公众号共振) | 公众号里多号同发了吗 | **只覆盖公众号**一条链 |
| `push_viral_alerts`(资源库) | 24h 内 ≥3 个号同发？ | 只读 `WechatPanLink`(公众号),且近 7 天同链**最多 2 个号** ⇒ 有史以来 0 行 |
| `cross_resonance_tick` | 跨平台共振榜 | **每天**一张日报,不回答"刚刚突然变多" |

⇒ 用户 2026-11-20 问「如果哪个资源突然推的人变多是否会有提醒」,答案是**基本没有**。

## 判据

对**同一条盘链**(`pan_id` 源链优先,没有才用 `pan_url`;均为 `pan_key` 归一后的身份):

    当前窗口的不同作者数  n   ≥  `push_surge_min_hands`(默认 6)
    比上一窗口的增量      n-p ≥  `push_surge_min_gain`(默认 4)
    且相对倍数            n/p ≥  `push_surge_min_ratio`(默认 1.8;`p=0` 时这条不适用)

三个条件**同时**成立才算突增。只用"绝对值高"会把**长期火**的资源每小时报一次;
只用"涨了"会把 2→3 也算成突增。三条一起才对应"**突然有很多人加入**"。

## 「每个板块都加上」怎么落的

数据源是 `push_ledger`(推送台账)—— **它已经是全板块的**(公众号/抖音线索/抖音对标号/
小红书/快手/贴吧/B站/知乎/微博同行各写各的行)。所以这里**不按板块分开跑**:
把全部板块的行聚到一起按盘链数人,**再在卡片里拆出板块分布**。

⚠️ 为什么不按板块各跑一遍:全局聚合是**每个板块的超集** —— 只在贴吧起量的资源
(贴吧单板块 6 个人)在全局同样是 6 个人,照样报得出来;而分开跑会让**同一次起量
报 N 次**(每个板块一次),正撞本仓"噪音化的守卫等于没有守卫"那条。
卡片里给 `板块分布`,人一眼看得出"是全网在推还是就一个群在自嗨"。

## 为什么用**作者数**而不是行数

行数会被**我们自己**污染:同一资源被 `board_digest`/补推/重试各推一遍就是多行,
而"有多少人在推"才是资源本身的热度。作者为空的行走不进 `hands`
(数不出是谁 ⇒ 不能算一个推手),这类行**单独记账**并写进运行记录 —— 不许静默丢。

## ⚠️ 链身份的已知边界(别把它当全量)

· 公众号那条链的 `pan_url` 是**我方转存链** ⇒ 多号推同一资源会收敛成一条,**数得准**;
· 小红书/快手/贴吧/B站/知乎给的是**源链** ⇒ 每个推广号自己建链 ⇒ **会低估**;
· `pan_id`(源链)只有部分推送点给(近 7 天带盘行里约 3%)⇒ 实际大多按 `pan_url` 聚。
实测近 7 天仍有 7 次真起量能被它抓到(跨 2~5 个板块),说明同一条链确实在跨板块流通;
但**"没报"不等于"没人推"**,别反推。

## 去向

**管理群**(`webhook_for(settings, "admin")`)—— 这是运营情报,不进客户群
(客户只会看到内容卡,不该收到"这个资源我们内部觉得火")。
每条盘链 **24h 冷却**(`feishu_alert_gate`);旁路失败只留日志,不抛。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)


def _identity(row) -> str:
    """台账行 → **资源身份**。源链(`pan_id`)优先,没有才退回 `pan_url`。

    ⚠️ 两者都是 `pan_key()` 归一后的串(`pan_id` 在写入时已归一)。
    """
    from app.services.push_ledger import pan_key

    pid = str(getattr(row, "pan_id", "") or "").strip()
    return pid or pan_key(str(getattr(row, "pan_url", "") or ""))


def detect_surges(session: Session, user_id: int, *, window_hours: float = 3.0,
                  min_hands: int = 6, min_gain: int = 4, min_ratio: float = 1.8,
                  top: int = 5, now: datetime | None = None,
                  stats: dict | None = None) -> list[dict]:
    """找出**这一小时**推手数突增的盘链(按增量降序,最多 `top` 条)。

    返回 `[{pan_key, pan_url, pan_type, title, hands, prev_hands,
             sections:{section: 行数}, first, last}]`。

    ⚠️ **纯读**:不开网络、不写库 —— 冷却门与发送留给 `push_surge_tick`。
    """
    from app.db.models import PushLedger

    now = now or datetime.now()
    w = timedelta(hours=float(window_hours))
    cur_start, prev_start = now - w, now - 2 * w

    rows = session.execute(
        select(PushLedger.pan_url, PushLedger.pan_id, PushLedger.section,
               PushLedger.author, PushLedger.title, PushLedger.pan_type,
               PushLedger.pushed_at)
        .where(PushLedger.user_id == user_id,
               PushLedger.pushed_at >= prev_start,
               PushLedger.pushed_at < now,
               PushLedger.pan_url != "")
        .order_by(PushLedger.pushed_at.asc())
    ).all()

    cur: dict[str, dict] = {}
    prev: dict[str, set] = {}
    n_no_author = 0
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        who = str(r.author or "").strip()
        if r.pushed_at < cur_start:                    # 上一窗口:只数人
            if who:
                prev.setdefault(pk, set()).add(who)
            continue
        if not who:
            #: ⚠️ 作者为空 ⇒ 数不出是谁,**不能算一个推手**;但必须记账(不许静默丢)
            n_no_author += 1
            continue
        g = cur.get(pk)
        if g is None:
            cur[pk] = g = {"pan_key": pk, "pan_url": str(r.pan_url or ""),
                           "pan_type": str(r.pan_type or ""), "title": "",
                           "hands": set(), "sections": {},
                           "first": r.pushed_at, "last": r.pushed_at}
        g["hands"].add(who)
        sec = str(r.section or "")
        g["sections"][sec] = g["sections"].get(sec, 0) + 1
        g["last"] = r.pushed_at
        #: 标题取**最早那条非空的**(与 `window()`/`cross_section_summary` 同一口径:
        #: 先出现的那份通常是"第一次发现它"时的原话)
        if not g["title"]:
            g["title"] = str(r.title or "")

    out: list[dict] = []
    for pk, g in cur.items():
        n = len(g["hands"])
        p = len(prev.get(pk, ()))
        if n < int(min_hands):
            continue
        if n - p < int(min_gain):
            continue
        if p and float(min_ratio or 0) > 0 and n < p * float(min_ratio):
            continue
        out.append({**g, "hands": n, "prev_hands": p})
    out.sort(key=lambda x: (-(x["hands"] - x["prev_hands"]), -x["hands"],
                            str(x.get("title") or "")))
    if stats is not None:
        stats.update({"rows": len(rows), "pans": len(cur), "no_author": n_no_author,
                      "cand": len(out)})
    return out[:int(top)] if top else out


def _render(a: dict, nl: str) -> str:
    """一条突增 → 飞书纯文本。"""
    from app.services.board_digest import board_label

    secs = sorted(a["sections"].items(), key=lambda kv: -kv[1])
    spread = " · ".join(f"{board_label(s) or s} {n}" for s, n in secs[:6])
    lines = [f"📈 资源突然有更多人在推:{a['title'][:40] or a['pan_key'][:40]}",
             f"👥 推手 **{a['prev_hands']} → {a['hands']} 人**"
             f"(+{a['hands'] - a['prev_hands']})· 涉及 {len(secs)} 个板块",
             f"🗂 {spread[:180]}"]
    if a.get("pan_type"):
        lines.append(f"🧭 {a['pan_type']}")
    if a.get("pan_url"):
        lines.append(a["pan_url"])
    return nl.join(lines)


def push_surge_tick(settings=None) -> int:
    """定时入口(每小时):找出推手数突增的盘链 → **管理群**。返回发送成功数。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.db import get_session_local
    from app.db.models import User
    from app.services.alert_service import feishu_alert_gate
    from app.services.feishu_client import FeishuClient, webhook_for

    hook = webhook_for(settings, "admin")
    if not hook:
        return 0
    kw = {"window_hours": float(getattr(settings, "push_surge_window_hours", 3) or 3),
          "min_hands": int(getattr(settings, "push_surge_min_hands", 6) or 6),
          "min_gain": int(getattr(settings, "push_surge_min_gain", 4) or 4),
          "min_ratio": float(getattr(settings, "push_surge_min_ratio", 1.8) or 1.8),
          "top": int(getattr(settings, "push_surge_top", 5) or 5)}
    cooldown = float(getattr(settings, "push_surge_cooldown_hours", 24) or 24)
    client = FeishuClient(hook, getattr(settings, "feishu_secret", ""))
    session = get_session_local()()
    nl = chr(10)
    sent = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            stats: dict = {}
            try:
                alerts = detect_surges(session, uid, stats=stats, **kw)
            except Exception:  # noqa: BLE001 - 单用户失败不挡其余
                session.rollback()
                logger.exception("推手突增检测失败 user=%s", uid)
                continue
            n_sent = 0
            for a in alerts:
                key = "surge:" + a["pan_key"][:120]
                try:
                    passed = feishu_alert_gate(
                        session, uid, "push_surge", key, cooldown,
                        f"{a['prev_hands']}->{a['hands']} 人")
                except Exception:  # noqa: BLE001 - 冷却门写不进就跳过这条
                    logger.warning("推手突增冷却门写入失败,跳过 %s", key, exc_info=True)
                    session.rollback()
                    continue
                if not passed:
                    continue
                #: ★ **冷却门先落盘再发飞书**(本仓母题:别抱着 SQLite 写锁做慢活)。
                #: 突增一天才几条,每条一次 commit 的代价可以忽略;
                #: 反之若挂到最后一个 commit,写事务会跨过整轮网络发送。
                try:
                    session.commit()
                except Exception:  # noqa: BLE001 - 提交失败只影响冷却,照发
                    logger.warning("推手突增冷却门提交失败", exc_info=True)
                    session.rollback()
                if client.send(_render(a, nl)):
                    sent += 1
                    n_sent += 1
            #: 运行记录:不写的话「今天没突增」与「作业没跑」在库里长得一样
            try:
                from app.services.tenant_base import _record_run

                _record_run(session, uid, "push_surge", "success",
                            f"窗口{kw['window_hours']:g}h 台账{stats.get('rows', 0)}行 "
                            f"盘链{stats.get('pans', 0)} 候选{stats.get('cand', 0)} "
                            f"发出{n_sent} 无作者{stats.get('no_author', 0)}行")
                session.commit()
            except Exception:  # noqa: BLE001 - 运行记录失败不挡业务
                session.rollback()
                logger.warning("推手突增运行记录失败 user=%s", uid, exc_info=True)
    finally:
        session.close()
    return sent