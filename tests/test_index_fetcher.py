"""指数获取器单测:Mock 源、降级链。"""
from app.models import IndexSource as Src
from app.services.index_fetcher import IndexFetchError, IndexFetcher, MockIndexSource, build_index_fetcher


class _FlakySource:
    """总是失败的源,用于测试降级。"""

    def fetch(self, keyword: str):  # type: ignore[no-untyped-def]
        raise IndexFetchError("模拟失败")


def test_mock_source_is_deterministic() -> None:
    src = MockIndexSource(points=5)
    a = src.fetch("关键词")
    b = src.fetch("关键词")
    assert [p.value for p in a.points] == [p.value for p in b.points]
    assert len(a.points) == 5
    assert a.keyword == "关键词"


def test_build_mock_fetcher(settings) -> None:
    fetcher = build_index_fetcher(settings)
    series = fetcher.fetch("关键词")
    assert series is not None
    assert series.source in (Src.DOUYIN, Src.BAIDU)


def test_fallback_chain() -> None:
    # 第一源失败,自动降级到 MockSource
    fetcher = IndexFetcher([_FlakySource(), MockIndexSource(points=4)])
    series = fetcher.fetch("词")
    assert series is not None
    assert len(series.points) == 4


def test_fetch_all_skips_failures() -> None:
    fetcher = IndexFetcher([_FlakySource()])
    results = fetcher.fetch_all(["a", "b"])
    assert results == []


def test_fetch_returns_none_when_all_fail() -> None:
    fetcher = IndexFetcher([_FlakySource()])
    assert fetcher.fetch("词") is None
