"""趋势分析工具单测(纯逻辑,无网络)。

注意:老单用户管线的 `analyze/analyze_all` 已随 `app/models.py` 移除,
仅保留被智能体复用的纯函数 `compute_growth` / `compute_slope`。
"""
from app.services.trend_analyzer import compute_growth, compute_slope


def test_growth_positive() -> None:
    assert compute_growth([100.0, 150.0]) == 0.5


def test_growth_zero_previous() -> None:
    assert compute_growth([0.0, 100.0]) is None


def test_growth_insufficient_samples() -> None:
    assert compute_growth([100.0]) is None


def test_slope_rising() -> None:
    assert compute_slope([100.0, 120.0, 150.0, 200.0]) > 0


def test_slope_flat_is_none() -> None:
    assert compute_slope([100.0, 100.0, 100.0]) is None
