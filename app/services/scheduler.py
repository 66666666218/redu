"""后台调度器(见 doc/dev.md §6)。

与旧的"每板块一个全局 Cron"不同,这里**每分钟 tick 一次**,从
`user_schedules` 表取出到期的 (用户, 板块) 逐个执行——因为频率是每个用户
自己设的,不再有统一周期。用户改频率下一分钟即生效。

随 FastAPI 应用一起启动(`app/platform.py` 的 lifespan),因此单容器部署即可,
不需要额外的调度容器。`python -m app.main` 的独立调度模式复用同一套 tick 逻辑。
"""
from __future__ import annotations

import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import Settings, get_settings
from app.services import schedule_service
from app.utils import get_logger

logger = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _runners() -> dict:
    """延迟导入,避免 `tenant` ←→ 调度器的循环导入。"""
    from app.services.tenant import run_baidu, run_douhot, run_weibo, run_xianyu

    return {"weibo": run_weibo, "xianyu": run_xianyu, "douhot": run_douhot, "baidu": run_baidu}


def collect_tick(settings: Settings | None = None, now: datetime | None = None) -> dict:
    """每分钟执行一次:把到期的 (用户, 板块) 采集跑掉。

    单条失败不影响其余;失败也会记为"已执行"(见 `schedule_service.mark_ran`),
    失败重试由 `retry_failed_runs` 负责。
    """
    from app.db import get_session_local

    settings = settings or get_settings()
    runners = _runners()
    now = now or datetime.now()
    db = get_session_local()()
    ok = failed = skipped = 0
    try:
        schedule_service.ensure_all_users(db)
        due = schedule_service.due_schedules(db, now)
        for row in due:
            runner = runners.get(row.section)
            if runner is None:
                continue
            # 缺 Cookie 就跳过且**不标记**:用户配好 Cookie 后下一分钟即可开跑,
            # 也不会每个周期刷一条"未配置 Cookie"的失败记录。
            if schedule_service.missing_cookie(db, row.user_id, row.section):
                skipped += 1
                continue
            try:
                runner(db, row.user_id, settings)
                ok += 1
                # 采集成功后触发飞书实时提醒(新增/飙升话题立即推送到群里)。
                # 异步、失败不影响本轮采集结果。
                try:
                    from app.services.cross_platform import run_cross_platform_alert
                    from app.services.feishu import run_feishu_keyword_alerts, run_feishu_keyword_realtime, run_feishu_realtime

                    run_feishu_realtime(row.section, row.user_id, settings)
                    if row.section == "douhot":
                        run_feishu_keyword_alerts(row.user_id, settings)
                        run_feishu_keyword_realtime(row.user_id, settings)  # 话题词新进/上升/爆发实时提醒
                    run_cross_platform_alert(row.user_id, settings)  # ≥2板块上升的关键词
                except Exception:  # noqa: BLE001
                    logger.exception("飞书实时提醒失败 section=%s user=%s", row.section, row.user_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                db.rollback()
                logger.warning("定时采集失败 用户=%s 板块=%s:%s", row.user_id, row.section, exc)
            finally:
                schedule_service.mark_ran(db, row, now)
    finally:
        db.close()
    if ok or failed:
        logger.info("采集 tick 完成:执行=%s 成功=%s 失败=%s 跳过=%s", ok + failed, ok, failed, skipped)
    return {"due": ok + failed, "ok": ok, "failed": failed, "skipped": skipped}


def _safe(func):  # type: ignore[no-untyped-def]
    """包裹调度作业:异常只记日志,不杀死调度器。"""

    def wrapper() -> None:
        try:
            func()
        except Exception:  # noqa: BLE001
            logger.exception("调度作业执行失败:%s", getattr(func, "__name__", func))

    wrapper.__name__ = getattr(func, "__name__", "job")
    return wrapper


def _cron_trigger(expr: str, default: dict) -> CronTrigger:
    """解析 5 段 Cron 为 APScheduler 触发;非法时回退到 default(避免死作业)。

    ⚠️ 星期几字段必须做 **POSIX → APScheduler** 换算:标准 cron 里 0=周日,而
    APScheduler 的 `day_of_week` 是 **0=周一**(0 周一,1 周二, …, 6 周日)。
    不换算会把"0 9 * * 1"(想周一)跑成周二、"0 20 * * 0"(想周日)跑成周一。
    """
    try:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"需 5 段,收到 {len(parts)} 段")
        # 星期几:数字按 POSIX(0=Sun..6=Sat)→ APScheduler(0=Mon..6=Sun) 换算
        dow = parts[4]
        if dow.isdigit() and len(dow) == 1:
            parts[4] = str((int(dow) + 6) % 7)
        cron_kw = dict(zip(("minute", "hour", "day", "month", "day_of_week"), parts))
        return CronTrigger(**cron_kw)
    except Exception:  # noqa: BLE001
        return CronTrigger(**default)


def build_jobs(scheduler: BackgroundScheduler) -> None:
    """注册后台作业:按用户频率采集、定时告警摘要、失败自动重试、飞书日报/周报、邮件周报。"""
    from app.admin import retry_failed_runs
    from app.services.alert_service import check_collect_failures, check_health_stalls, run_fixed_time_digests, run_weekly_summary
    from app.services.feishu import run_feishu_daily, run_feishu_insight_digest
    from config.settings import get_settings as _get_settings

    scheduler.add_job(
        _safe(collect_tick), CronTrigger(minute="*"), id="collect_tick", max_instances=1, coalesce=True
    )
    scheduler.add_job(
        _safe(run_fixed_time_digests), CronTrigger(minute="*"), id="alert_fixed_time", max_instances=1, coalesce=True
    )
    for func, job_id in ((retry_failed_runs, "auto_retry_failed_runs"), (check_collect_failures, "collect_failed_alert"),
                         (check_health_stalls, "health_stall_alert")):
        scheduler.add_job(_safe(func), CronTrigger(minute="*/30"), id=job_id, max_instances=1, coalesce=True)
    # 数据保留治理:每天 04:00 删除超过 DATA_RETENTION_DAYS 的旧快照/运行/日志
    from app.db.maintenance import cleanup_old_data

    scheduler.add_job(
        _safe(cleanup_old_data), CronTrigger(hour=4, minute=0), id="data_cleanup", max_instances=1, coalesce=True
    )
    jobs = [
        (run_feishu_daily, _get_settings().feishu_daily_cron, {"minute": 0, "hour": 8}, "feishu_daily"),
        (run_feishu_insight_digest, _get_settings().feishu_insight_cron, {"day_of_week": "mon", "hour": 9, "minute": 0}, "feishu_insight"),
        (run_weekly_summary, _get_settings().weekly_summary_cron, {"day_of_week": "sun", "hour": 20, "minute": 0}, "weekly_summary"),
    ]
    for func, expr, default, job_id in jobs:
        scheduler.add_job(_safe(func), _cron_trigger(expr, default), id=job_id, max_instances=1, coalesce=True)


def start(settings: Settings | None = None) -> BackgroundScheduler | None:
    """启动后台调度器(幂等:重复调用只会启动一次)。

    `SCHEDULER_ENABLED=false` 时不启动(测试环境、或改用独立调度容器时)。
    """
    global _scheduler
    settings = settings or get_settings()
    if not settings.scheduler_enabled:
        logger.info("SCHEDULER_ENABLED=false,后台调度器未启动")
        return None
    with _lock:
        if _scheduler is not None:
            return _scheduler
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        build_jobs(scheduler)
        scheduler.start()
        _scheduler = scheduler
    logger.info("后台调度器已启动:按各用户设置的频率采集(每分钟检查一次到期任务)")
    return _scheduler


def shutdown() -> None:
    """停止调度器(应用关闭时调用)。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        _scheduler.shutdown(wait=False)
        _scheduler = None
    logger.info("后台调度器已停止")
