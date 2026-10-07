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
    "cannot be called from a running event loop" —— 所以用 `run_async` 显式判一下。

★ 2026-10-08:那个"判一下"的helper**提升成了** `app.utils.asyncrun.run_async`
(闲鱼/贴吧的纯协议也都要跑 aiotieba,一份实现就够),并且**超时从"静默返回 None"改成抛**
—— 详见那个模块的 docstring。
"""
from __future__ import annotations

from app.utils import get_logger
from app.utils.asyncrun import run_async

logger = get_logger(__name__)

# 一次最多问几个帖子:每个 = 一次网络请求,而这一路只是"补一个指标",不该拖慢主链
DEFAULT_LIMIT = 8
_TIMEOUT = 25.0


async def _detail_of(tids: list[int], limit: int) -> dict[str, dict]:
    """取每个 tid 的**首楼**:`{tid: {"agree": 点赞, "text": 全文}}`。

    ★ **全文在这里**(2026-10-08 补):`get_posts` 的楼层对象上,正文在 **`contents`**
    (富文本片段列表),**不是 `content`** —— 后者恒为 `None`。我第一版读 `content`,
    拿到"首楼 0 字",差点把结论写成"匿名取不到全文"。而盘链恰恰就在全文里
    (搜索接口给的 `content` 是**截断的摘要**,里面通常没有链接)。

    ⇒ 所以**点赞和全文是一次请求拿回来的**,两件事不各花一次。
    """
    import aiotieba

    out: dict[str, dict] = {}
    async with aiotieba.Client() as client:
        for tid in tids[:limit]:
            try:
                posts = await client.get_posts(tid, with_comments=False)
            except Exception as exc:  # noqa: BLE001 - 单帖失败不挡其余
                logger.debug("贴吧取楼层失败 tid=%s: %s", tid, exc)
                continue
            first = next((p for p in posts if int(getattr(p, "floor", 0)) == 1), None)
            if first is None:
                continue
            out[str(tid)] = {"agree": int(getattr(first, "agree", 0) or 0),
                             "text": flatten_contents(getattr(first, "contents", None))}
    return out


def flatten_contents(contents: object) -> str:
    """`contents`(富文本片段)→ 一串文本。

    片段有两类:`FragText(text=…)` 和 `FragLink(text=…, title=…, raw_url=…)`。
    链接**不在 `text` 里** —— 那是个 `tiebaclient://` 深链,真正的网盘地址藏在它的
    查询串里(所以要把 `raw_url` 也拍进来,调用方再 `unquote` 一次)。
    """
    parts: list[str] = []
    for frag in (contents or []):
        got = False
        for name in ("text", "title", "raw_url"):
            v = getattr(frag, name, None)
            if v:
                parts.append(str(v))
                got = True
        if not got:
            parts.append(str(frag))
    return "\n".join(parts)


def fetch_posts_detail(tids: list[str | int], limit: int = DEFAULT_LIMIT) -> dict[str, dict]:
    """批量取首楼详情 `{tid: {"agree":…, "text":…}}`。**不抛异常**(旁路,拿不到就不放进去)。

    ⚠️ **`tid` 必须先转 int** —— `aiotieba.get_posts` 对字符串会报
    `'str' object cannot be interpreted as an integer`(实测踩过)。
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
        return run_async(_detail_of(nums, limit), timeout=_TIMEOUT + 5)
    except ImportError:
        logger.info("贴吧详情跳过:未安装 aiotieba(`pip install aiotieba`)")
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("贴吧详情获取失败(不影响主链):%s", str(exc)[:150])
        return {}


def fetch_agree(tids: list[str | int], limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """批量取点赞:**`{tid: agree}`**。失败/取不到**不放进结果**(调用方按"没有"处理)。

    ⚠️ **不抛异常**:这是"补一个指标"的旁路,拿不到不该拖垮发现链。
    实现走 `fetch_posts_detail`(同一次请求顺带把全文也取了,见那里的说明)。
    """
    return {tid: int(d.get("agree", 0) or 0)
            for tid, d in fetch_posts_detail(tids, limit=limit).items()}


def agree_to_metrics(agree: int | None) -> dict[str, int]:
    """点赞数 → `conversion` 认的 `metrics` 字典(**0/None 一律不放** —— 那是"没给")。"""
    try:
        v = int(agree or 0)
    except (TypeError, ValueError):
        return {}
    return {"liked_count": v} if v > 0 else {}
