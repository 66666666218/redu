"""趋势分析引擎单测(纯逻辑,无网络)。"""
from datetime import datetime, timedelta

from app.models import IndexPoint, IndexSource, TrendSeries
from app.services.trend_analyzer import analyze, analyze_all, compute_growth, compute_slope


def _series(values: list[float], keyword: str = "测试词", source: IndexSource = IndexSource.BAIDU) -> TrendSeries:
    now = datetime.now()
    points = [IndexPoint(ts=now + timedelta(minutes=i), value=v) for i, v in enumerate(values)]
    return TrendSeries(keyword=keyword, source=source, points=points)


def test_growth_positive() -> None:
    assert compute_growth([100.0, 150.0]) == 0.5


def test_growth_zero_previous() -> None:
    assert compute_growth([0.0, 100.0]) is None


def test_growth_insufficient_samples(settings) -> None:
    assert compute_growth([100.0]) is None


def test_slope_rising() -> None:
    assert compute_slope([100.0, 120.0, 150.0, 200.0]) > 0


def test_slope_flat_is_none() -> None:
    assert compute_slope([100.0, 100.0, 100.0]) is None


def test_analyze_rising(settings) -> None:
    series = _series([100.0, 120.0, 150.0, 205.0])  # growth>0.3, slope>0
    result = analyze(series, settings)
    assert result.rising is True
    assert result.growth is not None and result.growth > 0.3


def test_analyze_not_rising_when_growth_low(settings) -> None:
    series = _series([100.0, 102.0, 101.0, 101.0])  # growth≈0, slope≈0
    result = analyze(series, settings)
    assert result.rising is False


def test_analyze_insufficient_samples(settings) -> None:
    # 样本数 < min_samples(默认 3),直接判定不达标
    series = _series([100.0, 150.0])
    result = analyze(series, settings)
    assert result.rising is False


def test_analyze_all(settings) -> None:
    rising = _series([100.0, 130.0, 170.0, 230.0], keyword="A")
    flat = _series([100.0, 100.0, 100.0], keyword="B")
    results = analyze_all([rising, flat], settings)
    flags = [r.rising for r in results]
    assert flags == [True, False]
