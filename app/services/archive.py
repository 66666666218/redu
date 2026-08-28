"""归档服务(见 doc/dev.md §5.7)。

`ArchiveService` 是"日志与归档"阶段的业务入口,内部委托给
`ArchiveRepository`(SQLite)与 JSON 快照,向上层提供统一的高层方法。
"""
from __future__ import annotations

from pathlib import Path

from app.models import Alert, HotItem, TrendAnalysis
from app.storage import ArchiveRepository


class ArchiveService:
    """高层归档接口,封装持久化细节。"""

    def __init__(self, repo: ArchiveRepository) -> None:
        self._repo = repo

    # ---- 写入 ----
    def record_run(self, run: dict) -> None:
        self._repo.save_run(run)

    def record_items(self, run_id: str, items: list[HotItem]) -> None:
        self._repo.save_items(run_id, items)

    def record_analysis(self, run_id: str, analyses: list[TrendAnalysis]) -> None:
        self._repo.save_analysis(run_id, analyses)

    def record_alert(self, run_id: str, alert: Alert) -> None:
        self._repo.save_alert(run_id, alert)

    def record_snapshot(self, run_id: str, payload: dict) -> Path:
        return self._repo.snapshot(run_id, payload)

    # ---- 查询(代理给仓库) ----
    def latest_analysis(self, limit: int = 20) -> list[dict]:
        return self._repo.latest_analysis(limit)

    def latest_alerts(self, limit: int = 20) -> list[dict]:
        return self._repo.latest_alerts(limit)

    def latest_run(self) -> dict | None:
        return self._repo.latest_run()
