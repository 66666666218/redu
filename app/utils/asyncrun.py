# -*- coding: utf-8 -*-
"""在**同步**代码里跑协程(2026-10-08 从 `tieba_metrics` 提升上来)。

## 为什么不能无脑 `asyncio.run`
uvicorn 的请求处理线程里**已经有一个在跑的事件循环**,在那种线程里调 `asyncio.run`
会直接抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`。
APScheduler 的作业线程没有循环,可以直接用。

## ⚠️ 超时**必须抛**,不能返回 `None`
被提升之前那份实现是 `t.join(timeout)` 之后直接 `return box.get("r")` —— 线程还没跑完时
它返回 **`None`**,调用方会把它当成"没有数据"。那是本仓最忌讳的那种失败:
**看起来像"没结果",其实是"没跑完"**。
现在超时抛 `TimeoutError`,由调用方决定是"降级"还是"算失败" —— 决定权在它手里,
但它至少**看得见**。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine

#: 默认上限。aiotieba 自己有请求级超时,这里是**兜底**(防线程卡死拖住整轮)。
DEFAULT_TIMEOUT = 30.0


def run_async(coro: Coroutine[Any, Any, Any], timeout: float = DEFAULT_TIMEOUT) -> Any:
    """跑一个协程并返回结果;当前线程已有事件循环时换**独立线程**跑。

    `timeout` 只在"换线程"那条路上生效(`asyncio.run` 本身不带超时)——
    没有循环时协程自己会按它内部的超时返回,这一点写在这里免得被读成"两条路都管超时"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # 没有运行中的循环(APScheduler 线程就是这种)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["r"] = asyncio.run(coro)
        except BaseException as exc:      # noqa: BLE001 - 原样传回主线程再抛(含 KeyboardInterrupt)
            box["e"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # ⚠️ **这里必须抛**:返回 None 会让调用方把"没跑完"读成"没数据"(见模块 docstring)
        raise TimeoutError(f"协程超过 {timeout:g}s 未返回(线程仍在跑)")
    if "e" in box:
        raise box["e"]
    return box["r"]
