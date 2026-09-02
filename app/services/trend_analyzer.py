"""趋势分析工具(见 doc/dev.md §5.5)。

为智能体(关键词 Agent / 多平台预测)提供纯函数指标:
- `compute_growth`: 环比增长率
- `compute_slope`: 线性回归斜率

纯函数,无网络依赖,核心逻辑可单测。
"""
from __future__ import annotations

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
