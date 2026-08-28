"""趋势分析引擎(见 doc/dev.md §5.5)。

双重校验机制,**两者同时满足**才认定为"上涨趋势":
1. 环比增长率 > `growth_threshold`;
2. 线性回归斜率 > `slope_threshold`。

纯函数,无网络依赖,核心逻辑可单测。
"""
from __future__ import annotations

import numpy as np

from config.settings import Settings
from app.models import IndexSource, TrendAnalysis, TrendSeries


def compute_growth(values: list[float]) -> float | None:
    """计算整体环比增长率 `(latest - previous) / previous`。

    规则:
    - 样本数 < 2,返回 `None`(无法计算)。
    - 上期 `previous == 0`,返回 `None`(除零,视为不达标)。
    """
    if len(values) < 2:
        return None
    previous = values[-2]
    latest = values[-1]
    if previous == 0:
        return None
    return (latest - previous) / previous


def compute_slope(values: list[float]) -> float | None:
    """用 NumPy 线性回归计算趋势斜率(对横坐标 0..n-1)。

    规则:
    - 样本数 < 2,返回 `None`(无法拟合)。
    - 数值波动为 0(常数序列),返回 `None`(无趋势)。
    """
    if len(values) < 2:
        return None
    x = np.arange(len(values), dtype=float)
    y = np.asarray(values, dtype=float)
    if np.allclose(y, y[0]):
        return None
    slope = np.polyfit(x, y, 1)[0]
    return float(slope)


def analyze(series: TrendSeries, settings: Settings) -> TrendAnalysis:
    """对单个指数序列做双重校验,产出 `TrendAnalysis`。"""
    growth = compute_growth(series.values)
    slope = compute_slope(series.values)

    # 样本不足,直接判定不可分析,避免过少样本误判(见 doc/dev.md §5.5)。
    if len(series.points) < settings.min_samples:
        return TrendAnalysis(
            keyword=series.keyword,
            source=series.source,
            growth=growth,
            slope=slope,
            rising=False,
        )

    rising = bool(
        growth is not None
        and slope is not None
        and growth > settings.growth_threshold
        and slope > settings.slope_threshold
    )
    return TrendAnalysis(
        keyword=series.keyword,
        source=series.source,
        growth=growth,
        slope=slope,
        rising=rising,
    )


def analyze_all(series_list: list[TrendSeries], settings: Settings) -> list[TrendAnalysis]:
    """批量分析,返回全部结果(含未触发告警的项)。"""
    return [analyze(s, settings) for s in series_list]
