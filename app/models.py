"""领域数据模型。

内部管道使用薄 `@dataclass` DTO(见 doc/dev.md §3)。
API 层另有 pydantic 响应模型(见 app/api.py)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from enum import Enum


class IndexSource(str, Enum):
    """指数数据源。"""

    WEIBO = "weibo"     # 微博热度序列(由多轮采集累积)
    DOUYIN = "douyin"
    BAIDU = "baidu"


class RunStatus(str, Enum):
    """一次管道运行的最终状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class HotItem:
    """微博热搜条目(见 doc/dev.md §3.1)。"""

    title: str
    heat: int
    rank: int = 0
    category: str | None = None
    url: str | None = None
    tag: str | None = None
    captured_at: datetime = field(default_factory=datetime.now)


@dataclass
class IndexPoint:
    """某一时刻的指数采样点(见 doc/dev.md §3.2)。"""

    ts: datetime
    value: float


@dataclass
class TrendSeries:
    """某个关键词在某一数据源的指数时间序列(见 doc/dev.md §3.3)。"""

    keyword: str
    source: IndexSource
    points: list[IndexPoint] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)

    @property
    def values(self) -> list[float]:
        return [p.value for p in self.points]


@dataclass
class TrendAnalysis:
    """趋势分析结果(见 doc/dev.md §3.4)。"""

    keyword: str
    source: IndexSource
    growth: float | None
    slope: float | None
    rising: bool
    decided_at: datetime = field(default_factory=datetime.now)


@dataclass
class Alert:
    """触发的一次告警(见 doc/dev.md §3.5)。"""

    keyword: str
    reason: str
    sources: list[dict] = field(default_factory=list)
    triggered_at: datetime = field(default_factory=datetime.now)
