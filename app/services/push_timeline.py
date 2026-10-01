"""推送时段表(2026-10-01):让"什么时间推什么"可配置,而不是写死在 settings 的 cron 里。

借鉴 TrendRadar 的 `timeline.yaml` 思路,取其轻:不做它的 collect/analyze/push 三阶段开关
(我们的采集与推送本就分离,采集有 `schedule_service` 管),只解决两件事——
**推送时刻可视配置** + **按星期差异化**(周末静默这类诉求)。

为什么不给每个类型注册一条 Cron:
沿用 `schedule_service` 的既定哲学(见 doc/dev.md §6)——用户改配置后**无需重建调度作业**,
也天然避开"API 进程改配置、调度进程不知道"的跨进程不同步。代价是每分钟唤醒一次做判定;
这点开销相对"配置改了不生效"完全可以接受。

配置存 `system_config` 的 `push_timeline`(全局一份,与其它 settings 同级——本系统实际
单活跃用户;真要多租户时把 _KEY 换成按 uid 拼即可)。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SystemConfig
from app.utils import get_logger
from config.settings import get_settings

logger = get_logger(__name__)

_KEY = "push_timeline"
_LAST_TICK_KEY = "push_timeline_last_tick"   # 上次检查到的分钟(补跑用,见 minutes_since)
_MAX_BACKFILL_MIN = 10                       # 补跑窗口上限(分钟)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# 推送类型定义:times=每天推送时刻(HH:MM),days=生效星期(**POSIX 口径:0=周日 … 6=周六**,
# 与 settings 里的 cron 一致,换算见 doc/dev.md),role=**由哪个实例发**(见 settings.scheduler_role)——
# 分体部署时两端的库不同(本机有公众号+闲鱼、远程有热点),推送必须各归各的,
# 否则同一个飞书群会收到两份标题相同、内容各异的卡。单实例(all)时照常全推。
PUSH_KINDS: dict[str, dict] = {
    "daily":    {"label": "热点日报",       "times": ["08:00"],                  "days": [0, 1, 2, 3, 4, 5, 6], "role": "hotspot"},
    "hotrank":  {"label": "多平台热榜速览", "times": ["09:30", "21:30"],         "days": [0, 1, 2, 3, 4, 5, 6], "role": "hotspot"},
    "agent":    {"label": "选题 Agent",     "times": ["09:10", "15:10", "21:10"], "days": [0, 1, 2, 3, 4, 5, 6], "role": "hotspot"},
    "insight":  {"label": "爆点回顾",       "times": ["09:00"],                  "days": [1],                   "role": "hotspot"},
    "review":   {"label": "选题复盘周报",   "times": ["10:00"],                  "days": [1],                   "role": "hotspot"},
    "weekly":   {"label": "本周热点洞察",   "times": ["20:00"],                  "days": [0],                   "role": "hotspot"},
    "analysis": {"label": "公众号选题分析", "times": ["10:00"],                  "days": [0, 1, 2, 3, 4, 5, 6], "role": "wechat"},
}


def _instance_role() -> str:
    """当前实例角色(模块级引用 `get_settings`,便于测试 monkeypatch)。"""
    return (getattr(get_settings(), "scheduler_role", "all") or "all").strip().lower()


def _role_allows(kind_role: str) -> bool:
    """本实例该不该发这一类。`all`(单实例)全发;`both` 的类别两边都发。"""
    cur = _instance_role()
    return cur == "all" or kind_role in ("both", cur)


def _posix_dow(when: datetime) -> int:
    """Python 的 weekday()(0=周一…6=周日)→ POSIX cron 口径(0=周日…6=周六)。"""
    return (when.weekday() + 1) % 7


def _run(kind: str, settings) -> int:
    """执行一次推送。runner 一律懒加载——这些模块多数反向依赖调度器,顶部 import 会成环。"""
    if kind == "daily":
        from app.services.feishu import run_feishu_daily

        return run_feishu_daily(settings)
    if kind == "hotrank":
        from app.services.hot_sources import push_hot_rank_card_all_users

        return push_hot_rank_card_all_users(settings)
    if kind == "analysis":
        from app.services.feishu import run_feishu_wechat_analysis

        return run_feishu_wechat_analysis(settings)
    if kind == "agent":
        from app.services.hotspot_agent import hotspot_agent_tick_all_users

        return hotspot_agent_tick_all_users(settings)
    if kind == "insight":
        from app.services.feishu import run_feishu_insight_digest

        return run_feishu_insight_digest(settings)
    if kind == "review":
        from app.services.weekly_review import run_weekly_review_all_users

        return run_weekly_review_all_users()
    if kind == "weekly":
        from app.services.tenant import run_weekly_summary

        return run_weekly_summary()
    raise KeyError(f"未知推送类型:{kind}")


def _merge(raw: dict | None) -> dict:
    """把库里的配置叠到默认值上,并**丢弃非法项**(坏时刻/坏星期不该把整份配置变成哑弹)。

    时刻全被剔光(= 用户实际关掉了它)时把 `enabled` 置假,免得界面上看着是开的却永不触发。
    """
    given = (raw or {}).get("kinds") or {}
    kinds: dict[str, dict] = {}
    for kind, spec in PUSH_KINDS.items():
        cur = given.get(kind) or {}
        times = [str(t) for t in (cur.get("times") if cur.get("times") is not None else spec["times"])
                 if _TIME_RE.match(str(t).strip())]
        days = [int(d) for d in (cur.get("days") if cur.get("days") is not None else spec["days"])
                if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6]
        kinds[kind] = {"label": spec["label"], "role": spec.get("role", "both"),
                       "times": times, "days": days,
                       "enabled": bool(cur.get("enabled", True)) and bool(times)}
    return {"kinds": kinds}


def load(db: Session) -> dict:
    """读推送时段配置(缺省项用默认补齐)。"""
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _KEY))
    raw = None
    if row and row.value:
        try:
            raw = json.loads(row.value)
        except ValueError:
            logger.warning("push_timeline 配置不是合法 JSON,已回落默认值")
    return _merge(raw)


def save(db: Session, payload: dict | None) -> dict:
    """保存配置(先过 `_merge` 校验);返回落库后的完整配置供前端回显。"""
    cfg = _merge(payload)
    text = json.dumps(cfg, ensure_ascii=False)
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _KEY))
    if row is None:
        db.add(SystemConfig(key=_KEY, value=text))
    else:
        row.value = text
    db.commit()
    return cfg


def due_kinds(when: datetime, cfg: dict, role: str | None = None) -> list[str]:
    """某一分钟该跑的推送类型(时刻 + 星期 + 已启用 + **本实例角色**)。

    `role` 留空则取当前实例角色(`settings.scheduler_role`)——分体部署时热点类归远程、
    公众号类归本机,各自只发自己那份,同一个群不会收到两份标题相同内容各异的卡。
    """
    cur = (role or _instance_role()).strip().lower()
    hhmm = when.strftime("%H:%M")
    dow = _posix_dow(when)
    return [kind for kind, spec in (cfg.get("kinds") or {}).items()
            if spec.get("enabled") and hhmm in (spec.get("times") or [])
            and dow in (spec.get("days") or [])
            and (cur == "all" or spec.get("role", "both") in ("both", cur))]


def _read_last_tick(session: Session, now: datetime) -> datetime:
    """上次检查到的那一分钟。

    没记录(首次部署/记录被清)时返回**上一分钟**而不是 `now`:后者会让
    `minutes_since` 得到空集、连当前这一分钟都不检查,白白漏掉部署后的第一个时刻。
    """
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _LAST_TICK_KEY))
    if row and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            logger.warning("push_timeline 的上次检查时间无法解析,按只查当前分钟处理")
    return now - timedelta(minutes=1)


def _write_last_tick(session: Session, when: datetime) -> None:
    value = when.replace(second=0, microsecond=0).isoformat(sep=" ", timespec="seconds")
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _LAST_TICK_KEY))
    if row is None:
        session.add(SystemConfig(key=_LAST_TICK_KEY, value=value))
    else:
        row.value = value
    session.commit()


def minutes_since(last: datetime, now: datetime, cap: int = _MAX_BACKFILL_MIN) -> list[datetime]:
    """`(last, now]` 之间逐分钟的时刻(含 `now` 所在的那一分钟)。

    这是"不错过"的关键:调度器偶尔会把一次 tick 拖到下一分钟才执行,而推送时刻是
    按分钟精确匹配的——只看"当前这一分钟"就会把被跳过的那一刻**永久漏掉**。
    补跑窗口封顶 `cap` 分钟:服务停了一夜后重启不该把攒了一晚上的推送全倒出来。
    """
    start = max(last, now - timedelta(minutes=cap)).replace(second=0, microsecond=0)
    out: list[datetime] = []
    cur = start + timedelta(minutes=1)
    while cur <= now:
        out.append(cur)
        cur += timedelta(minutes=1)
    return out


def tick(settings=None, db: Session | None = None, when: datetime | None = None) -> dict:
    """每分钟唤醒一次:执行"自上次检查以来跨过的"所有推送时刻。返回 `{"ran", "failed"}`。

    **为什么不是"只看当前这一分钟"**:调度器偶尔会把一次 tick 拖到下一分钟才执行
    (线程池繁忙、上一轮卡住),而推送时刻按分钟精确匹配——只看当前分钟的话,被跳过的
    那一刻就**永久漏掉且毫无提示**,用户只会觉得"今天怎么没收到日报"。所以记下上次
    检查到的分钟,把中间跨过的时刻一并补跑(窗口封顶,见 `minutes_since`)。

    单类型失败不影响其余;失败**不重试**——这类都是"当期内容",晚一分钟再推一批
    没有意义(下一轮的自然会补上),重试只会把同一条内容推两遍。
    """
    from app.db import get_session_local

    own = db is None
    session = db or get_session_local()()
    now = when or datetime.now()
    ran: list[str] = []
    failed: list[str] = []
    try:
        cfg = load(session)
        last = _read_last_tick(session, now)
        for moment in minutes_since(last, now):
            for kind in due_kinds(moment, cfg):
                try:
                    _run(kind, settings)
                    ran.append(kind)
                    logger.info("推送时段触发:%s(%s) @%s", kind,
                                cfg["kinds"][kind]["label"], moment.strftime("%H:%M"))
                except Exception:  # noqa: BLE001 - 单类型失败不拖累其余
                    failed.append(kind)
                    logger.exception("推送时段执行失败:%s", kind)
        _write_last_tick(session, now)
    finally:
        if own:
            session.close()
    return {"ran": ran, "failed": failed}
