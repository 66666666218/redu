"""重试工具(见 doc/dev.md §1.2 高可用)。

提供带指数退避与随机抖动的重试装饰器,用于包裹所有外部网络调用。
"""
from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from app.utils.logger import get_logger

P = ParamSpec("P")
T = TypeVar("T")
logger = get_logger(__name__)


def retry(
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.2,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """指数退避重试装饰器。

    参数:
        attempts: 最大尝试次数(含首次)。
        base_delay: 基础等待秒数。
        max_delay: 单次等待上限。
        jitter: 随机抖动比例(在 [1-jitter, 1+jitter] 间乘到 delay 上)。
        exceptions: 需要重试的异常类型元组;不在其中的异常直接抛出。
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    if attempt == attempts:
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay *= random.uniform(1 - jitter, 1 + jitter)
                    logger.warning("第 %s/%s 次调用失败(%s),%.1fs 后重试", attempt, attempts, exc, delay)
                    time.sleep(max(delay, 0.0))
            raise RuntimeError("unreachable")  # pragma: no cover

        return wrapper

    return decorator
