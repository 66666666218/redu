"""贴吧帖子的**点赞数**(`agree`)—— 用 `aiotieba` 拿(2026-10-04)。

**为什么需要它**:MediaCrawler 的贴吧搜索接口**只给回复数**(`total_replay_num`),
**连点赞都没有** —— 而"其余平台按播放量 × 40%"需要一个曝光/互动量兜底
(见 `conversion.EXPOSURE_LADDER`:最后那档"互动总量"里,贴吧连"赞"都缺)。

**怎么拿**:搜索出来的 `tid` → `aiotieba.get_posts(tid)` → **楼主楼层(floor=1)的
`agree` 就是点赞数**。`aiotieba` 把 tbs / 签名都封装好了,**匿名即可**(不用登录)。

**实测(2026-10-04,14 条真实帖子)**:6 条 `agree` 非 0(3 / 5 / 4 / 1 / 24 / 4)
⇒ **真有值,不是恒 0**。⚠️ 单看一条会以为"全是 0"(第一条正好是 0),别拿一个样本下结论。

⚠️ **`aiotieba` 是异步库,而本服务全程同步**:
  · APScheduler 的作业跑在**线程池**里 ⇒ 那个线程没有事件循环,`asyncio.run` 可用;
  · 但**万一**将来从协程里调到(uvicorn 的路由),`asyncio.run` 会抛
    "cannot be called from a running event loop" —— 所以这里**显式判一下**,
    有运行中的循环就丢到独立线程去跑(见 `_run_async`)。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

# 一次最多问几个帖子:每个 = 一次网络请求,而这一路只是"补一个指标",不该拖慢主链
DEFAULT_LIMIT = 8
_TIMEOUT = 25.0


def _run_async(coro):
    """在**没有运行中事件循环**的上下文里跑协程;若当前线程已有循环,换独立线程跑。

    为什么不能无脑 `asyncio.run`:uvicorn 的请求处理线程里已经有一个在跑的循环,
    在那种线程里调 `asyncio.run` 会直接抛异常。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # 没有运行中的循环(APScheduler 线程就是这种)→ 直接用
    # 有循环 ⇒ 丢到独立线程,避免嵌套
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["r"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - 传回主线程再抛
            box["e"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(_TIMEOUT + 5)
    if "e" in box:
        raise box["e"]
    return box.get("r")


async def _agree_of(tids: list[int], limit: int) -> dict[str, int]:
    import aiotieba

    out: dict[str, int] = {}
    async with aiotieba.Client() as client:
        for tid in tids[:limit]:
            try:
                posts = await client.get_posts(tid, with_comments=False)
            except Exception as exc:  # noqa: BLE001 - 单帖失败不挡其余
                logger.debug("贴吧取楼层失败 tid=%s: %s", tid, exc)
                continue
            first = next((p for p in posts if int(getattr(p, "floor", 0)) == 1), None)
            if first is not None:
                out[str(tid)] = int(getattr(first, "agree", 0) or 0)
    return out


def fetch_agree(tids: list[str | int], limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """批量取点赞:**`{tid: agree}`**。失败/取不到**不放进结果**(调用方按"没有"处理)。

    ⚠️ **不抛异常**:这是"补一个指标"的旁路,拿不到不该拖垮发现链。
    ⚠️ **`tid` 必须是 int** —— `aiotieba.get_posts` 对字符串会报
    `'str' object cannot be interpreted as an integer`(实测踩过),这里先转好。
    """
    nums: list[int] = []
    for t in tids:
        try:
            nums.append(int(str(t).strip()))
        except (TypeError, ValueError):
            continue
    if not nums:
        return {}
    try:
        return _run_async(_agree_of(nums, limit))
    except ImportError:
        logger.info("贴吧点赞跳过:未安装 aiotieba(`pip install aiotieba`)")
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("贴吧点赞获取失败(不影响发现链):%s", str(exc)[:150])
        return {}


def agree_to_metrics(agree: int | None) -> dict[str, int]:
    """点赞数 → `conversion` 认的 `metrics` 字典(**0/None 一律不放** —— 那是"没给")。"""
    try:
        v = int(agree or 0)
    except (TypeError, ValueError):
        return {}
    return {"liked_count": v} if v > 0 else {}
