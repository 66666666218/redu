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
from app.utils import disable_env_proxies, get_logger, setup_logging

logger = get_logger(__name__)


def _disable_dead_proxy() -> None:
    """★ 2026-10-22:系统代理指向死端口时关掉它。

    `disable_env_proxies()` 只治本进程的 requests;Playwright 的十几个启动点与
    MediaCrawler(独立 venv 子进程)**都听系统代理** ⇒ 死端口会让它们一起
    `ERR_PROXY_CONNECTION_FAILED`。这里一次性恢复净状态(详见
    `app.utils.proxy.disable_dead_system_proxy` 的边界说明)。
    """
    try:
        from app.utils import disable_dead_system_proxy

        disable_dead_system_proxy()
    except Exception:  # noqa: BLE001 - 启动自愈失败不能挡住启动
        logger.debug("关闭死系统代理失败", exc_info=True)


def run_scheduler() -> None:
    """独立调度进程:与 API 内嵌调度器完全同一套作业。"""
    setup_logging()
    disable_env_proxies()  # 忽略系统代理(2026-09-29 事故加固)
    _disable_dead_proxy()  # 浏览器/子进程也听系统代理(2026-10-22)
    logger.info("启动独立调度模式:按各用户设置的采集频率(每分钟检查到期任务)")

    from app.services.scheduler import _scheduler_kwargs

    # 参数与 API 内嵌调度器完全一致(线程池 24/合并/5min 补跑),两模式行为不漂移
    scheduler = BlockingScheduler(**_scheduler_kwargs())
    build_jobs(scheduler)
    scheduler.start()


def run_api() -> None:
    import uvicorn

    setup_logging()
    disable_env_proxies()  # 忽略系统代理(2026-09-29 事故加固)
    _disable_dead_proxy()  # 浏览器/子进程也听系统代理(2026-10-22)
    settings = get_settings()
    logger.info("启动 API 模式(多租户平台),端口 %s", settings.app_port)
    uvicorn.run("app.platform:app", host="0.0.0.0", port=settings.app_port)


if __name__ == "__main__":
    if "--api" in sys.argv:
        run_api()
    else:
        run_scheduler()
