"""趋势分析工具单测(纯逻辑,无网络)。

注意:老单用户管线的 `analyze/analyze_all` 已随 `app/models.py` 移除,
仅保留被智能体复用的纯函数 `compute_growth` / `compute_slope`。
"""
import pytest

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


class TestRecentGrowth:
    """时间感知环比:掉榜数轮后回榜的序列不得当作'环比'(2026-09-14 审计 S3-13)。"""

    def _pts(self, *offset_hours_values):
        from datetime import datetime, timedelta
        base = datetime(2026, 9, 14, 2, 0)
        return [(base + timedelta(hours=oh), float(v)) for oh, v in offset_hours_values]

    def test_normal_series_matches_compute_growth(self):
        from app.services.trend_analyzer import compute_growth, recent_growth

        pts = self._pts((0, 100), (0.5, 120), (1.0, 150))
        assert recent_growth(pts) == pytest.approx(compute_growth([100, 120, 150]))

    def test_gap_anomaly_returns_none(self):
        """[-2] 与 [-1] 相隔远超中位间隔(掉榜数轮后回榜)→ None。"""
        from app.services.trend_analyzer import recent_growth

        pts = self._pts((0, 100), (0.5, 110), (1.0, 120), (30, 300))  # 末拍隔 29h
        assert recent_growth(pts) is None

    def test_zero_previous_returns_none(self):
        from app.services.trend_analyzer import recent_growth

        assert recent_growth(self._pts((0, 0), (0.5, 50))) is None

    def test_single_point_returns_none(self):
        from app.services.trend_analyzer import recent_growth

        assert recent_growth(self._pts((0, 100))) is None

    def test_unsorted_input_ok(self):
        from app.services.trend_analyzer import compute_growth, recent_growth

        pts = list(reversed(self._pts((0, 100), (0.5, 120), (1.0, 150))))
        assert recent_growth(pts) == pytest.approx(compute_growth([100, 120, 150]))
