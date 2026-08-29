"""管道编排(见 doc/dev.md §6)。

按固定顺序执行:采集 → 清洗 → 指数 → 分析 → 告警 → 归档。
任一步失败记录故障并触发系统级告警,返回带 `status` 的结果字典,不会静默失败。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from config.settings import Settings, get_settings
from app.models import Alert, HotItem, RunStatus, TrendAnalysis
from app.services import cleaner, collector
from app.services.archive import ArchiveService
from app.services.index_fetcher import build_index_fetcher
from app.services.notifier import Notifier, get_notifier
from app.services.trend_analyzer import analyze
from app.storage import ArchiveRepository
from app.utils import get_logger

logger = get_logger(__name__)


def run_pipeline(
    settings: Settings | None = None,
    repo: ArchiveRepository | None = None,
    notifier: Notifier | None = None,
    fetcher: object | None = None,
    collect_fn: Callable[[Settings], list[HotItem]] | None = None,
) -> dict:
    """执行一次完整管道,返回结果字典。

    参数可注入以便测试(默认使用真实实现)。返回结构:
    ```
    {
      "run_id", "status", "started_at", "finished_at",
      "items_collected", "analyses_count", "rising_count", "error",
    }
    ```
    """
    settings = settings or get_settings()
    owns_repo = repo is None
    repo = repo or ArchiveRepository(settings.data_dir)
    archive = ArchiveService(repo)
    notifier = notifier or get_notifier(settings)
    fetcher = fetcher or build_index_fetcher(settings, repo)
    collect_fn = collect_fn or collector.fetch_hot_search

    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    started_at = datetime.now()
    result: dict = {
        "run_id": run_id,
        "status": RunStatus.SUCCESS.value,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "items_collected": 0,
        "analyses_count": 0,
        "rising_count": 0,
        "error": None,
    }
    archive.record_run(result)

    try:
        # 1. 采集
        items = collect_fn(settings)
        result["items_collected"] = len(items)

        # 2. 清洗
        candidates = cleaner.clean(items, settings)
        keywords = [it.title for it in candidates]

        # 3. 指数获取(并行采集所有信号源,用于交叉验证)
        series_map = fetcher.fetch_parallel(keywords)  # type: ignore[attr-defined]

        # 4. 趋势分析(逐序列)
        analyses_by_keyword: dict[str, list[TrendAnalysis]] = {
            kw: [analyze(s, settings) for s in series_list] for kw, series_list in series_map.items()
        }
        all_analyses = [a for lst in analyses_by_keyword.values() for a in lst]
        result["analyses_count"] = len(all_analyses)

        # 5. 告警(交叉验证: both=所有源同涨才告警; any=任一源涨即告警)
        mode = getattr(settings, "alert_mode", "both")
        rising_keywords: list[str] = []
        for kw, lst in analyses_by_keyword.items():
            rising_list = [a for a in lst if a.rising]
            if not rising_list:
                continue
            if mode == "both" and len(rising_list) != len(lst):
                continue  # 并非所有信号源都上涨
            rising_keywords.append(kw)

        result["rising_count"] = len(rising_keywords)
        for kw in rising_keywords:
            kw_analyses = analyses_by_keyword[kw]
            alert = Alert(
                keyword=kw,
                reason=_reason_text(kw_analyses),
                sources=[_source_metric(a) for a in kw_analyses if a.rising],
            )
            notifier.notify(alert)
            archive.record_alert(run_id, alert)

        # 6. 归档
        archive.record_items(run_id, items)
        archive.record_analysis(run_id, all_analyses)
        archive.record_snapshot(
            run_id,
            {
                "items": [item.__dict__ for item in items],
                "analyses": [a.__dict__ for a in all_analyses],
            },
        )

        result["finished_at"] = datetime.now().isoformat()
        archive.record_run(result)
        logger.info(
            "管道运行完成 run=%s 采集=%s 分析=%s 上涨=%s",
            run_id,
            len(items),
            len(all_analyses),
            len(rising_keywords),
        )
        return result

    except Exception as exc:  # noqa: BLE001 - 捕获全部异常,记录故障并告警
        logger.exception("管道运行失败 run=%s", run_id)
        result.update(
            status=RunStatus.FAILED.value,
            finished_at=datetime.now().isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )
        archive.record_run(result)
        try:
            system_alert = Alert(keyword="system", reason=f"管道运行失败:{type(exc).__name__}: {exc}")
            notifier.notify(system_alert)
        except Exception:  # noqa: BLE001
            logger.exception("系统级告警发送失败")
        return result
    finally:
        # 仅关闭本函数创建的 repo;调用方(如 API 共享仓库)保留生命周期。
        if owns_repo:
            repo.close()


def _source_metric(analysis) -> dict:
    """单源指标摘要。"""
    return {"source": analysis.source.value, "growth": analysis.growth, "slope": analysis.slope}


def _reason_text(analyses: list[TrendAnalysis]) -> str:
    """将上涨分析结论(可能多个源)转为告警原因文案。"""
    parts = []
    for a in analyses:
        if a.rising:
            growth = a.growth if a.growth is not None else 0.0
            slope = a.slope if a.slope is not None else 0.0
            parts.append(f"{a.source.value} 环比 {growth:.0%}/斜率 {slope:.1f}")
    return "且".join(parts) or "上涨"


if __name__ == "__main__":
    import json

    from app.utils import setup_logging

    setup_logging()
    outcome = run_pipeline()
    print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
