"""微博热度序列指数源与热度历史查询单测。"""
from datetime import datetime, timedelta

import pytest

from app.models import HotItem, IndexSource
from app.services.index_fetcher import IndexFetchError, WeiboHeatIndexSource
from app.storage import ArchiveRepository


def test_heat_series_and_weibo_source(tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    now = datetime.now()
    runs = [
        ("run1", now - timedelta(hours=2), 1000),
        ("run2", now - timedelta(hours=1), 1500),
        ("run3", now, 2100),
    ]
    for run_id, ts, heat in runs:
        repo.save_items(run_id, [HotItem(title="关键词", heat=heat, captured_at=ts)])

    pts = repo.keyword_heat_series("关键词")
    assert len(pts) == 3
    assert [p.value for p in pts] == [1000.0, 1500.0, 2100.0]

    series = WeiboHeatIndexSource(repo).fetch("关键词")
    assert series.source == IndexSource.WEIBO
    assert [p.value for p in series.points] == [1000.0, 1500.0, 2100.0]


def test_heat_series_respects_limit(tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    for i in range(5):
        repo.save_items(f"run{i}", [HotItem(title="关键词", heat=100 * i)])
    pts = repo.keyword_heat_series("关键词", limit=3)
    assert len(pts) == 3
    assert [p.value for p in pts] == [200.0, 300.0, 400.0]


def test_weibo_source_no_history_raises(tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    with pytest.raises(IndexFetchError):
        WeiboHeatIndexSource(repo).fetch("不存在的词")
