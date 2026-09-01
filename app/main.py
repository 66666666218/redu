"""应用入口(见 doc/dev.md §6、§8)。

两种启动方式:
- 默认:独立调度进程(BlockingScheduler),按**各用户自己设置的频率**采集。
- `--api`:启动 FastAPI 平台服务(uvicorn);该进程默认已内嵌同一套后台调度器,
  单容器部署只跑这个即可。

用法:
    python -m app.main                 # 独立调度模式
    python -m app.main --api           # API 模式(含内嵌调度器)

注意:两种模式不要同时跑同一套库,否则会重复采集;若拆成独立调度容器,
API 容器需设 `SCHEDULER_ENABLED=false`。
"""
from __future__ import annotations

import sys

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import get_settings
from app.services.scheduler import build_jobs
from app.utils import get_logger, setup_logging

logger = get_logger(__name__)


def run_scheduler() -> None:
    """独立调度进程:与 API 内嵌调度器完全同一套作业。"""
    setup_logging()
    logger.info("启动独立调度模式:按各用户设置的采集频率(每分钟检查到期任务)")

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    build_jobs(scheduler)
    scheduler.start()


def run_api() -> None:
    import uvicorn

    setup_logging()
    settings = get_settings()
    logger.info("启动 API 模式(多租户平台),端口 %s", settings.app_port)
    uvicorn.run("app.platform:app", host="0.0.0.0", port=settings.app_port)


if __name__ == "__main__":
    if "--api" in sys.argv:
        run_api()
    else:
        run_scheduler()
