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


def _summary(
    keyword: str,
    label: str,
    growth: float | None,
    forecast: float | None,
    last: float,
    confidence: str = "",
    burst: bool = False,
) -> str:
    """组装一段中文分析。"""
    parts = [f"「{keyword}」当前热度 {last:.0f}"]
    if growth is not None:
        parts.append(f"环比 {growth * 100:+.1f}%")
    if label == "上升期":
        parts.append("处于上升期,热度在走高,可关注")
    elif label == "回落期":
        parts.append("处于回落期,热度在降温")
    elif label == "震荡":
        parts.append("处于震荡,趋势不明朗")
    else:
        parts.append("走势平稳")
    if forecast is not None:
        parts.append(f"预测下一轮约 {forecast:.0f}")
    if burst:
        parts.append("🔥 可能爆发,建议密切关注")
    elif confidence == "数据不足":
        parts.append("(数据不足,仅供参考)")
    elif confidence == "低":
        parts.append("(波动较大,预测仅供参考)")
    return " ".join(parts)


def _confidence(values: list[float], slope: float | None, r2: float | None) -> str:
    """预测置信度:样本数 + 拟合优度共同决定。

    - 样本 <3:数据不足(参考价值低)
    - R²>=0.8 且样本>=5:高
    - R²>=0.5:中
    - 否则:低(波动大/趋势不稳)
    """
    if len(values) < 3:
        return "数据不足"
    if r2 is None:
        return "低"
    if r2 >= 0.8 and len(values) >= 5:
        return "高"
    if r2 >= 0.5:
        return "中"
    return "低"


def _r2(values: list[float]) -> float | None:
    """线性拟合的决定系数 R²;常数序列或样本<2 返回 None。"""
    if len(values) < 2:
        return None
    y = np.asarray(values, dtype=float)
    if np.allclose(y, y[0]):
        return None
    x = np.arange(len(values), dtype=float)
    b, a = np.polyfit(x, y, 1)
    y_hat = a + b * x
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0:
        return None
    return max(0.0, 1.0 - ss_res / ss_tot)


def _accel(values: list[float]) -> float | None:
    """加速度:最近一半窗口的斜率 - 前半段斜率。>0 = 上升在加速(爆点信号)。"""
    if len(values) < 4:
        return None
    half = max(2, len(values) // 2)
    early = compute_slope(values[:half])
    late = compute_slope(values[half:])
    if early is None or late is None:
        return None
    return late - early


def analyze(keyword: str, values: list[float]) -> dict:
    """对某关键词的热度序列做分析 + 预测。"""
    values = [float(v) for v in values if v is not None]
    last = values[-1] if values else 0.0
    growth = compute_growth(values)
    slope = compute_slope(values)
    forecast = _forecast_next(values)
    label = _label(growth, slope)
    r2 = _r2(values)
    accel = _accel(values)
    confidence = _confidence(values, slope, r2)
    # 爆发预警:处于上升期 && 预测明显 && 有一定置信度 && 加速度为正
    burst = bool(
        len(values) >= 3
        and label == "上升期"
        and forecast is not None
        and growth is not None
        and growth >= 0.20
        and confidence in ("高", "中")
        and (accel is None or accel > 0)
    )
    return {
        "keyword": keyword,
        "last_score": last,
        "growth": growth,           # 0.32 = +32%
        "slope": slope,
        "forecast_next": forecast,
        "trend_label": label,
        "points": len(values),
        "confidence": confidence,   # 高/中/低/数据不足
        "r2": r2,                   # 拟合优度(越接近1越稳)
        "accel": accel,             # 加速度(正=加速上升)
        "burst": burst,             # 是否可能爆发
        "summary": _summary(keyword, label, growth, forecast, last, confidence, burst),
        "series": values,           # 原始序列(供前端迷你图)
    }
