"""关键词监控智能体:趋势判定、预测外推、热度查询单测(不联网)。"""
import pytest

from app.services import keyword_agent
from app.services.keyword_agent import analyze, _forecast_next


def test_analyze_rising() -> None:
    out = analyze("热词", [100, 200, 320, 500])
    assert out["growth"] is not None and out["growth"] > 0.08
    assert out["trend_label"] == "上升期"
    assert out["last_score"] == 500 and out["forecast_next"] is not None
    assert "上升期" in out["summary"]


def test_analyze_falling() -> None:
    out = analyze("退潮", [500, 400, 300, 200])
    assert out["trend_label"] == "回落期"
    assert out["growth"] < -0.08
    assert "回落" in out["summary"]


def test_analyze_insufficient_samples() -> None:
    out = analyze("刚关注", [200])
    assert out["points"] == 1
    assert out["growth"] is None and out["forecast_next"] is None
    assert out["trend_label"] == "平稳"


def test_forecast_next_trends_with_slope() -> None:
    # 等差序列:斜率恒定,外推下一项约 5
    assert _forecast_next([1, 2, 3, 4]) is not None
    assert 4.5 <= _forecast_next([1, 2, 3, 4]) <= 5.5
    # 常数序列无趋势 → None
    assert _forecast_next([5, 5, 5]) is None
    # 样本不足 → None
    assert _forecast_next([7]) is None
