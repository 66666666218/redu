"""风嗓词监控「智能体」:基于历史热度序列做分析 + 短期预测(纯算法,离线)。

输入某个关键词在多轮采集中的热度序列(score 按时间排序),输出:
- `trend`:  上升期 / 回落期 / 震荡 / 平稳(由环比 + 斜率综合判定)
- `growth`: 最近一轮环比涨幅(百分比,可为负)
- `slope`:  整段线性回归斜率(热度变化的绝对速度)
- `forecast_next`: 下一轮的预测热度(线性外推,并做非负约束)
- `summary`: 一段中文分析摘要
- `series`/`spark`: 原始序列与迷你趋势数据(供前端画曲线)

纯 NumPy,无外部依赖;样本不足时返回保守结果(无法预测)。
"""
from __future__ import annotations

import numpy as np

from app.services.trend_analyzer import compute_growth, compute_slope

# 判定趋势的环比阈值(±8% 视为波动)
GROWTH_UP = 0.08
GROWTH_DOWN = -0.08


def _forecast_next(values: list[float]) -> float | None:
    """用整段线性回归外推下一个点;非负约束;样本<2 或常数序列返回 None。"""
    if len(values) < 2:
        return None
    slope = compute_slope(values)
    if slope is None:
        return None
    n = len(values)
    x = np.arange(n, dtype=float)
    y = np.asarray(values, dtype=float)
    # 用最小二乘拟合 y = a + b*x
    b, a = np.polyfit(x, y, 1)
    nxt = float(a + b * n)
    return max(nxt, 0.0)


def _label(growth: float | None, slope: float | None) -> str:
    """给趋势定档:上升期/回落期/震荡/平稳。"""
    if growth is None and slope is None:
        return "平稳"
    if growth is not None:
        if growth >= GROWTH_UP:
            return "上升期"
        if growth <= GROWTH_DOWN:
            return "回落期"
        return "震荡"
    # 只有 slope(无环比)时按方向粗判
    if slope is not None:
        return "上升期" if slope > 0 else ("回落期" if slope < 0 else "平稳")
    return "平稳"


def _summary(keyword: str, label: str, growth: float | None, forecast: float | None, last: float) -> str:
    """组装一段中文分析。"""
    parts = [f"「{keyword}」当前热度 {last:.0f}"]
    if growth is not None:
        parts.append(f"环比 {growth * 100:+.1f}%")
    if forecast is not None:
        parts.append(f"预测下一轮约 {forecast:.0f}")
    if label == "上升期":
        parts.append("处于上升期,热度在走高,可关注")
    elif label == "回落期":
        parts.append("处于回落期,热度在降温")
    elif label == "震荡":
        parts.append("处于震荡,趋势不明朗")
    else:
        parts.append("走势平稳")
    return " ".join(parts)


def analyze(keyword: str, values: list[float]) -> dict:
    """对某关键词的热度序列做分析 + 预测。"""
    values = [float(v) for v in values if v is not None]
    last = values[-1] if values else 0.0
    growth = compute_growth(values)
    slope = compute_slope(values)
    forecast = _forecast_next(values)
    label = _label(growth, slope)
    return {
        "keyword": keyword,
        "last_score": last,
        "growth": growth,           # 0.32 = +32%
        "slope": slope,
        "forecast_next": forecast,
        "trend_label": label,
        "points": len(values),
        "summary": _summary(keyword, label, growth, forecast, last),
        "series": values,           # 原始序列(供前端迷你图)
    }
