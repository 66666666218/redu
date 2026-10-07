# -*- coding: utf-8 -*-
"""作业落实性:**注册的作业** vs **真实心跳**(2026-10-04 建,2026-10-07 提到服务层并接进体检)。

## 它要回答的那一件事
"配置说这个作业每天跑" 与 "它实际跑没跑" 之间,**必须能自动对照**。
没有这个对照,一次真实的教训是:`resource_presence` 注册着、trigger 正确、`enabled=True`,
却**六天一次没跑** —— 而"小红书/快手每天跑"这件事我们一直以为成立。

## ⚠️⚠️ 为什么从 `scripts/` 提到 `app/services/`
两个缺口,都是**"守卫存在但等于没有"**这一类(2026-10-07 实测):

  ① **它从来没被定时调用** —— 只有人手跑 `scripts/job_liveness.py`。
     而"靠人记得跑脚本"正是本仓反复吃亏的模式(见 `WereadAppClient` 自愈那条)。
     ⇒ 现在 `chain_health.check_job_liveness` 把它接进**每天的链路体检**,自己会说话。

  ② **监听作业被显式排除在外**:老的 `_interval_seconds` 对 `minute='2'` + `hour='4,8,14,20'`
     这种"**一天几个定点**"直接 `return None`(注释写的是"间隔由哪一天决定,这里不算"),
     于是 `wechat_collect_tick` **永远不会**被算成超期。
     实测代价:监听轮从 10-06 20:02 起 **30 小时一轮没跑**,这个脚本即使跑了也不会报。
     ⇒ 现在按**最大空档**判(4/8/14/20 的最大空档是 8h,不是平均 6h)——
        "该跑没跑"要的是**最坏情况**,不是平均值。

③ 分层要求也是硬理由:**服务层不能依赖 `scripts/`**(Docker 镜像根本不 COPY `scripts/`)。
   所以逻辑必须在这边,脚本只是薄壳。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

#: 超期倍数:实际间隔 > 期望间隔 × 这个数 ⇒ 判"执行间隔远超预期"。
#: 取 3 而不是 1.5 —— 告警变噪音就没人看了(见 `_interval_seconds` 的注释),宁可晚一轮。
STALE_FACTOR = 3


def trigger_interval_seconds(trigger: Any) -> float | None:
    """从一个 APScheduler 触发器估**期望的最大间隔**(秒)。估不出返回 `None`(不猜)。

    ⚠️ 判"该跑没跑"要的是**最坏情况下的空档**,不是平均间隔:
    `hour='4,8,14,20'` 的平均是 6h,但 20:00→次日 04:00 的空档是 **8h** ——
    拿 6h 当基准会把每天都要发生的夜间空档算成"超期"。所以定点型一律取**最大环差**。

    ⚠️ **认不出一律 `None`**:编一个数出来会让"超期"变成噪音,然后被无视 ——
    那比不报还糟(被无视的告警等于没有告警)。
    """
    f = getattr(trigger, "fields", None)
    if not f:
        return None
    try:
        fields = {x.name: [str(e) for e in x.expressions] for x in f}
    except Exception:  # noqa: BLE001 - 触发器换了 API 就退化成"不判",别把整轮体检带崩
        return None
    m = fields.get("minute") or ["*"]
    h = fields.get("hour") or ["*"]
    dow = fields.get("day_of_week") or ["*"]
    if dow != ["*"]:                       # 每周(或每周多天)定点:间隔按天算
        n = len(dow) if all(x.isdigit() for x in dow) else 1
        return max(7 * 86400 / max(1, n), 86400)
    if h == ["*"] and len(m) == 1 and m[0].startswith("*/"):
        try:
            return float(m[0][2:]) * 60
        except ValueError:
            return None
    if h == ["*"] and len(m) == 1 and m[0].isdigit():
        return 3600.0                      # 每小时一次的定点
    if h == ["*"] and len(m) == 1 and m[0] == "*":
        return 60.0
    if h == ["*"] and len(m) > 1 and all(x.isdigit() for x in m):
        return 3600.0 / len(m)             # 每小时几个定点
    if len(h) == 1 and h[0].startswith("*/") and m and m[0].isdigit():
        try:
            return float(h[0][2:]) * 3600
        except ValueError:
            return None
    if all(x.isdigit() for x in h):
        hours = sorted(int(x) for x in h)
        if len(hours) == 1:
            return 86400.0
        # **最大环差**:4/8/14/20 → 差 4,6,6,跨夜 8 ⇒ 8h(见 docstring)
        gaps = [hours[i + 1] - hours[i] for i in range(len(hours) - 1)]
        gaps.append(hours[0] + 24 - hours[-1])
        return max(gaps) * 3600.0
    return None


def classify_missing(jobs: list, beats: dict, now: datetime,
                     baseline: datetime | None) -> tuple[list[str], list[str]]:
    """把**没有心跳**的作业分成两类,别把"还没到点"报成"从没跑过"。返回 `(真·漏跑, 还没到点)`。

    ⚠️ **基线按作业各算各的**(2026-10-05 修):此前一律用全表最早一条当 `since`,但那是
    "**心跳机制上线时刻**",不是"**这个作业的注册时刻**"。于是当天 13:10 才注册的
    `chain_report`(每天 09:30 触发)被误报成"注册了却从没执行过" —— 它只是还没到第一次点。
    **误报的代价是"重复劳动"**,而这正是本模块 docstring 里反思过的那件事。
    """
    missing: list[str] = []
    not_yet: list[str] = []
    for j in jobs:
        b = beats.get(j.id)
        if b is not None and b.last_run_at is not None:
            continue                       # 真跑过,不在这份清单里
        since = (b.first_seen_at if b is not None and b.first_seen_at else None) or baseline
        if since is not None and not should_have_fired(j.trigger, since, now):
            not_yet.append(j.id)
        else:
            missing.append(j.id)
    return missing, not_yet


def should_have_fired(trigger: Any, since: datetime, now: datetime) -> bool:
    """自 `since` 起,这个 trigger **本该已经触发过**吗?判据交给 APScheduler 自己算(别手搓 cron)。

    ⚠️ 时区必须对齐:trigger 给的是 **aware**,而库里的 `baseline` 是 **naive**,
    直接比会 `TypeError`。任何意外一律走**保守**分支(报出来),宁可报也别静默漏掉一个真没跑的。
    """
    try:
        nxt = trigger.get_next_fire_time(None, since)
        if nxt is None:
            return True                    # 再也不会触发了(end_date 已过)= 该管
        a, b = nxt, now
        if a.tzinfo is not None and b.tzinfo is None:
            a = a.astimezone().replace(tzinfo=None)
        elif a.tzinfo is None and b.tzinfo is not None:
            b = b.astimezone().replace(tzinfo=None)
        return a <= b
    except Exception:  # noqa: BLE001 - 算不出就不改判
        return True


def audit(session, scheduler: Any | None = None, now: datetime | None = None) -> dict:
    """跑一遍对账。返回 `{"jobs", "never", "not_yet", "stale", "baseline"}`。

    `scheduler` 不传就自己按当前角色建一个(**只建不启动**,只为读 trigger);
    传了就用调用方的(测试可注入)。
    """
    from sqlalchemy import select

    from app.db.models import JobHeartbeat

    if scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler

        from app.services.scheduler import build_jobs

        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        build_jobs(scheduler)
    beats = {b.job_id: b for b in session.scalars(select(JobHeartbeat)).all()}
    now = now or datetime.now()
    baseline = min((b.last_run_at for b in beats.values() if b.last_run_at), default=None)

    jobs = sorted(scheduler.get_jobs(), key=lambda x: x.id)
    stale: list[dict] = []
    for j in jobs:
        b = beats.get(j.id)
        iv = trigger_interval_seconds(j.trigger)
        if b is None or iv is None or b.last_run_at is None:
            continue
        over = (now - b.last_run_at).total_seconds()
        if over > iv * STALE_FACTOR:
            stale.append({"job_id": j.id, "last_run_at": b.last_run_at,
                          "expect_s": iv, "over_s": over})
    never, not_yet = classify_missing(jobs, beats, now, baseline)
    return {"jobs": jobs, "beats": beats, "never": never, "not_yet": not_yet,
            "stale": stale, "baseline": baseline, "now": now}
