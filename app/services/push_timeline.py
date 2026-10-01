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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SystemConfig
from app.utils import get_logger

logger = get_logger(__name__)

_KEY = "push_timeline"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# 推送类型定义:times=每天推送时刻(HH:MM),days=生效星期(**POSIX 口径:0=周日 … 6=周六**,
# 与 settings 里的 cron 一致,换算见 doc/dev.md)。这些是**默认值**,用户在「推送时段」页
# 改过的以库里为准(见 load/_merge)。
PUSH_KINDS: dict[str, dict] = {
    "daily":    {"label": "热点日报",       "times": ["08:00"],                  "days": [0, 1, 2, 3, 4, 5, 6]},
    "hotrank":  {"label": "多平台热榜速览", "times": ["09:30", "21:30"],         "days": [0, 1, 2, 3, 4, 5, 6]},
    "analysis": {"label": "公众号选题分析", "times": ["10:00"],                  "days": [0, 1, 2, 3, 4, 5, 6]},
    "agent":    {"label": "选题 Agent",     "times": ["09:10", "15:10", "21:10"], "days": [0, 1, 2, 3, 4, 5, 6]},
    "insight":  {"label": "爆点回顾",       "times": ["09:00"],                  "days": [1]},
    "review":   {"label": "选题复盘周报",   "times": ["10:00"],                  "days": [1]},
    "weekly":   {"label": "本周热点洞察",   "times": ["20:00"],                  "days": [0]},
}


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
        kinds[kind] = {"label": spec["label"], "times": times, "days": days,
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


def due_kinds(when: datetime, cfg: dict) -> list[str]:
    """当前时刻该跑的推送类型(时刻精确到分钟 + 星期匹配 + 已启用)。"""
    hhmm = when.strftime("%H:%M")
    dow = _posix_dow(when)
    return [kind for kind, spec in (cfg.get("kinds") or {}).items()
            if spec.get("enabled") and hhmm in (spec.get("times") or [])
            and dow in (spec.get("days") or [])]


def tick(settings=None, db: Session | None = None, when: datetime | None = None) -> dict:
    """每分钟唤醒一次:执行此刻到点的推送。返回 `{"ran": [...], "failed": [...]}`。

    单类型失败不影响其余;失败**不重试**——这类推送都是"当期内容",晚一分钟再推一批
    没有意义(下一轮的自然会补上),重试只会把同一条内容推两遍。
    """
    from app.db import get_session_local

    own = db is None
    session = db or get_session_local()()
    ran: list[str] = []
    failed: list[str] = []
    try:
        cfg = load(session)
        for kind in due_kinds(when or datetime.now(), cfg):
            try:
                _run(kind, settings)
                ran.append(kind)
                logger.info("推送时段触发:%s(%s)", kind, cfg["kinds"][kind]["label"])
            except Exception:  # noqa: BLE001 - 单类型失败不拖累其余
                failed.append(kind)
                logger.exception("推送时段执行失败:%s", kind)
    finally:
        if own:
            session.close()
    return {"ran": ran, "failed": failed}
