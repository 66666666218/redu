"""管道端到端单测(注入假采集/假指数源)。"""
from datetime import datetime, timedelta

from app.models import HotItem, IndexPoint, IndexSource, TrendSeries
from app.services.notifier import NullNotifier
from app.services.pipeline import run_pipeline
from app.storage import ArchiveRepository


class FakeFetcher:
    """根据关键词返回预定序列;未命中的返回均衡序列。"""

    RISING = [100.0, 120.0, 150.0, 205.0]
    FLAT = [100.0, 100.0, 100.0, 100.0]

    def fetch_all(self, keywords: list[str]) -> list[TrendSeries]:
        out = []
        now = datetime.now()
        for kw in keywords:
            values = self.RISING if kw == "明星B" else self.FLAT
            points = [IndexPoint(ts=now + timedelta(minutes=i), value=v) for i, v in enumerate(values)]
            out.append(TrendSeries(keyword=kw, source=IndexSource.BAIDU, points=points))
        return out

    def fetch_parallel(self, keywords: list[str]) -> dict[str, list[TrendSeries]]:
        now = datetime.now()
        out: dict[str, list[TrendSeries]] = {}
        for kw in keywords:
            values = self.RISING if kw == "明星B" else self.FLAT
            points = [IndexPoint(ts=now + timedelta(minutes=i), value=v) for i, v in enumerate(values)]
            out[kw] = [TrendSeries(keyword=kw, source=IndexSource.BAIDU, points=points)]
        return out


def _collect(settings) -> list[HotItem]:  # type: ignore[no-untyped-def]
    return [
        HotItem(title="明星A", heat=1000),
        HotItem(title="明星B", heat=2000),
        HotItem(title="置顶", heat=99999),  # 应被黑名单过滤
    ]


def test_pipeline_end_to_end(settings, tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    notifier = NullNotifier()
    result = run_pipeline(
        settings=settings,
        repo=repo,
        notifier=notifier,
        fetcher=FakeFetcher(),
        collect_fn=_collect,
    )

    assert result["status"] == "success"
    assert result["items_collected"] == 3
    assert result["analyses_count"] == 2
    # 只有"明星B"触发上涨,"置顶"被过滤,重复热搜被去重
    assert result["rising_count"] == 1
    assert len(notifier._sent) == 1
    assert notifier._sent[0].keyword == "明星B"

    latest = repo.latest_run()
    assert latest["status"] == "success"


def test_pipeline_failure_records_failed(settings, tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")

    def _boom(settings) -> list[HotItem]:  # type: ignore[no-untyped-def]
        raise RuntimeError("模拟采集崩溃")

    result = run_pipeline(settings=settings, repo=repo, collect_fn=_boom)

    assert result["status"] == "failed"
    assert "模拟采集崩溃" in result["error"]
    assert repo.latest_run()["status"] == "failed"


class CrossFetcher:
    """每个关键词返回两个源:weibo 上涨、wechat 平稳(一方涨一方不涨)。"""

    def fetch_parallel(self, keywords: list[str]) -> dict[str, list[TrendSeries]]:
        now = datetime.now()
        out: dict[str, list[TrendSeries]] = {}
        for kw in keywords:
            wb = [IndexPoint(ts=now + timedelta(minutes=i), value=v) for i, v in enumerate([100.0, 130.0, 170.0, 230.0])]
            wx = [IndexPoint(ts=now + timedelta(minutes=i), value=v) for i, v in enumerate([100.0, 100.0, 100.0, 100.0])]
            out[kw] = [
                TrendSeries(keyword=kw, source=IndexSource.WEIBO, points=wb),
                TrendSeries(keyword=kw, source=IndexSource.WECHAT, points=wx),
            ]
        return out


def test_cross_validation_both(settings, tmp_path) -> None:
    settings.alert_mode = "both"
    repo = ArchiveRepository(tmp_path / "data")
    notifier = NullNotifier()
    result = run_pipeline(settings=settings, repo=repo, notifier=notifier, fetcher=CrossFetcher(), collect_fn=_collect)
    # weibo 涨但 wechat 稳 → 并非所有源同涨 → 不告警
    assert result["rising_count"] == 0
    assert len(notifier._sent) == 0


def test_cross_validation_any(settings, tmp_path) -> None:
    settings.alert_mode = "any"
    repo = ArchiveRepository(tmp_path / "data")
    notifier = NullNotifier()
    result = run_pipeline(settings=settings, repo=repo, notifier=notifier, fetcher=CrossFetcher(), collect_fn=_collect)
    # 任一源涨即告警:两个候选词 weibo 均上涨
    assert result["rising_count"] == 2
    assert len(notifier._sent) == 2
