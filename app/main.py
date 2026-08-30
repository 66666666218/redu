"""应用入口(见 doc/dev.md §6、§8)。

两种启动方式:
- 默认:APScheduler 按 `JOB_CRON` 定时触发 `run_pipeline()`。
- `--api`:启动 FastAPI 监控服务(uvicorn)。

用法:
    python -m app.main                 # 调度模式
    python -m app.main --api           # API 模式
"""
from __future__ import annotations

import functools
import sys
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import get_settings
from app.services.pipeline import run_pipeline
from app.services.xianyu import run_xianyu, run_xianyu_daily
from app.utils import get_logger, setup_logging

logger = get_logger(__name__)


def parse_cron(expr: str) -> dict:
    """把 5 段 Cron 表达式转成 APScheduler 需要的字段字典。

    顺序:minute hour day month day_of_week。
    """
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"JOB_CRON 需要 5 段,当前为 {len(parts)} 段:{expr!r}")
    keys = ["minute", "hour", "day", "month", "day_of_week"]
    return {k: v for k, v in zip(keys, parts)}


def _cron_trigger(expr: str, default: dict) -> CronTrigger:
    """解析 Cron,非法时回退到默认字段。"""
    try:
        return CronTrigger(**parse_cron(expr))
    except ValueError:
        logger.warning("Cron 表达式非法:%r,使用默认 %s", expr, default)
        return CronTrigger(**default)


def _safe(func: Callable) -> Callable:
    """包裹调度函数:异常只记日志,不杀死调度器。"""

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("调度任务执行失败:%s", getattr(func, "__name__", func))
        return None

    return wrapper


def run_scheduler() -> None:
    settings = get_settings()
    setup_logging()
    logger.info("启动调度模式,周期 JOB_CRON=%s", settings.job_cron)

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _safe(run_pipeline),
        _cron_trigger(settings.job_cron, {"minute": "*/30"}),
        id="hot_monitor_pipeline",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _safe(run_xianyu),
        _cron_trigger(settings.xianyu_cron, {"minute": "0", "hour": "*/2"}),
        id="xianyu_hot",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _safe(run_xianyu_daily),
        _cron_trigger(settings.daily_summary_cron, {"minute": "0", "hour": 20}),
        id="xianyu_daily_summary",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def run_api() -> None:
    import uvicorn

    setup_logging()
    settings = get_settings()
    logger.info("启动 API 模式,端口 %s", settings.app_port)
    uvicorn.run("app.api:app", host="0.0.0.0", port=settings.app_port)


if __name__ == "__main__":
    if "--api" in sys.argv:
        run_api()
    else:
        run_scheduler()
