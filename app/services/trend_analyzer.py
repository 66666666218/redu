"""趋势分析工具(见 doc/dev.md §5.5)。

为智能体(关键词 Agent / 多平台预测)提供纯函数指标:
- `compute_growth`: 环比增长率
- `compute_slope`: 线性回归斜率

纯函数,无网络依赖,核心逻辑可单测。
"""
from __future__ import annotations

from datetime import datetime

import numpy as np


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


def recent_growth(points: list[tuple[datetime, float]], gap_multiplier: float = 2.5) -> float | None:
    """时间感知环比:最后一对相邻样本间隔异常大时返回 None(不算"环比")。

    词掉榜数轮后回榜,序列 `[-2]` 与 `[-1]` 可能相隔数小时甚至数天,
    直接 compute_growth 会把"隔了三天"当成"上期→本期",增长率全部失真。
    判定基准 = 序列自身的中位间隔 × gap_multiplier(自适应各板块采集频率)。
    points 任意顺序;样本 <2 或中位间隔为 0 时退回普通环比规则。
    """
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) < 2:
        return None
    gaps = [(pts[i][0] - pts[i - 1][0]).total_seconds() for i in range(1, len(pts))]
    positive = [g for g in gaps if g > 0]
    if positive:
        med = sorted(positive)[len(positive) // 2]
        if (pts[-1][0] - pts[-2][0]).total_seconds() > med * gap_multiplier:
            return None  # 最后两拍间隔异常(掉榜后回榜),不构成有效环比
    previous = pts[-2][1]
    latest = pts[-1][1]
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
