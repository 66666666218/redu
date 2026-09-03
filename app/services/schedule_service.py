"""每用户采集频率设置(见 doc/dev.md §6)。

用户可为 微博/闲鱼/抖音 三个板块**分别**设置采集间隔(分钟),或单独关掉某个板块。
本模块只管"频率的读写与到期判定",实际执行在 `app/services/scheduler.py`。

为什么用"间隔 + 上次运行时间"而不是每人一条 Cron:
用户改设置后无需重建调度作业(调度器每分钟读库判定),且天然避开了
"API 进程改配置、调度进程不知道"的跨进程同步问题。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, UserCookie, UserSchedule
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot", "baidu")
SECTION_LABELS = {"weibo": "微博热搜", "xianyu": "闲鱼热榜", "douhot": "抖音热点", "baidu": "百度热搜"}
# 板块 → 必需的 Cookie 平台(三个板块的 runner 都要求用户配好自己的 Cookie)
SECTION_COOKIE = {"weibo": "weibo", "xianyu": "goofish", "douhot": "douyin"}
# 可选档位(分钟);闲鱼/抖音是登录态接口,过于频繁有风控与 Cookie 失效风险
INTERVAL_CHOICES = (10, 30, 60, 180, 360, 720, 1440)
MIN_INTERVAL = 10
MAX_INTERVAL = 1440
DEFAULT_INTERVAL = 30


class ScheduleError(ValueError):
    """频率设置非法。"""


def normalize_interval(minutes: object) -> int:
    """校验采集间隔:必须是整数分钟,且不低于 `MIN_INTERVAL`(防止打爆三方接口)。"""
    try:
        value = int(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"采集间隔必须是整数分钟,收到:{minutes!r}") from exc
    if value < MIN_INTERVAL:
        raise ScheduleError(f"采集间隔不能小于 {MIN_INTERVAL} 分钟(防止触发平台风控)")
    if value > MAX_INTERVAL:
        raise ScheduleError(f"采集间隔不能大于 {MAX_INTERVAL} 分钟(1 天)")
    return value


def _to_dict(db: Session, row: UserSchedule) -> dict:
    return {
        "section": row.section,
        "label": SECTION_LABELS.get(row.section, row.section),
        "interval_minutes": row.interval_minutes,
        "enabled": bool(row.enabled),
        # 没配 Cookie 的板块会被调度器跳过,前端据此提示用户去配,免得"设了频率却没数据"
        "cookie_ready": not missing_cookie(db, row.user_id, row.section),
        "last_run_at": row.last_run_at.isoformat(sep=" ", timespec="seconds") if row.last_run_at else None,
        "next_run_at": _next_run(row).isoformat(sep=" ", timespec="seconds") if row.enabled else None,
    }


def _next_run(row: UserSchedule) -> datetime:
    """下次预计运行时间;从未跑过则视为立即。"""
    if row.last_run_at is None:
        return datetime.now()
    return row.last_run_at + timedelta(minutes=row.interval_minutes)


def get_or_create(db: Session, user_id: int, section: str) -> UserSchedule:
    """取该用户该板块的频率设置,不存在则按默认值建一条。"""
    if section not in SECTIONS:
        raise ScheduleError(f"不支持的板块:{section}")
    row = db.scalar(select(UserSchedule).where(UserSchedule.user_id == user_id, UserSchedule.section == section))
    if row is None:
        row = UserSchedule(user_id=user_id, section=section, interval_minutes=DEFAULT_INTERVAL, enabled=True)
        db.add(row)
        db.commit()
    return row


def list_schedules(db: Session, user_id: int) -> dict:
    """列出该用户三个板块的频率(缺失的按默认值补齐)。"""
    return {
        "choices": list(INTERVAL_CHOICES),
        "min_interval": MIN_INTERVAL,
        "items": [_to_dict(db, get_or_create(db, user_id, section)) for section in SECTIONS],
    }


def set_schedule(
    db: Session,
    user_id: int,
    section: str,
    interval_minutes: object | None = None,
    enabled: bool | None = None,
) -> dict:
    """设置某板块的采集间隔/开关(两者均可单独更新)。"""
    row = get_or_create(db, user_id, section)
    if interval_minutes is not None:
        row.interval_minutes = normalize_interval(interval_minutes)
    if enabled is not None:
        row.enabled = bool(enabled)
    db.commit()
    logger.info("用户 %s 的 %s 频率更新为 %s 分钟(启用=%s)", user_id, section, row.interval_minutes, row.enabled)
    return _to_dict(db, row)


def ensure_all_users(db: Session) -> int:
    """为所有用户补齐三个板块的频率记录,返回新建条数。

    调度器每轮开头调用:新注册用户、以及本功能上线前的老用户,都能自动纳入定时采集,
    不必等他们打开一次设置页。
    """
    user_ids = db.scalars(select(User.id)).all()
    existing = {tuple(r) for r in db.execute(select(UserSchedule.user_id, UserSchedule.section)).all()}
    created = 0
    for uid in user_ids:
        for section in SECTIONS:
            if (uid, section) not in existing:
                db.add(UserSchedule(user_id=uid, section=section, interval_minutes=DEFAULT_INTERVAL, enabled=True))
                created += 1
    if created:
        db.commit()
        logger.info("为用户补齐采集频率记录 %s 条", created)
    return created


def missing_cookie(db: Session, user_id: int, section: str) -> bool:
    """该板块是否因缺少 Cookie 而跑不了(跑了只会刷一堆失败记录)。"""
    platform = SECTION_COOKIE.get(section)
    if not platform:
        return False
    return db.scalar(
        select(UserCookie.id).where(UserCookie.user_id == user_id, UserCookie.platform == platform)
    ) is None


def due_schedules(db: Session, now: datetime | None = None) -> list[UserSchedule]:
    """扫描所有到期(该跑)的设置:已启用,且距上次运行已满间隔(或从未跑过)。"""
    now = now or datetime.now()
    rows = db.scalars(select(UserSchedule).where(UserSchedule.enabled.is_(True)).order_by(UserSchedule.id)).all()
    return [r for r in rows if r.last_run_at is None or now - r.last_run_at >= timedelta(minutes=r.interval_minutes)]


def mark_ran(db: Session, row: UserSchedule, now: datetime | None = None) -> None:
    """标记本轮已执行。

    **无论成败都要标记**:否则失败的任务会每分钟重试一次,把三方接口打爆;
    失败重试交给 `app/admin.py::retry_failed_runs`(每 30 分钟、限 3 次)。
    """
    row.last_run_at = now or datetime.now()
    db.commit()
