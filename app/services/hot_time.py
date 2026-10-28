"""**发布时刻的统一解析**(2026-10-10)。

## 为什么要单独一个模块

热榜源的"时间字段"五花八门 —— 实测同一批源里就有 **6 种形状**:

| 形状 | 实例 | 来源 |
|---|---|---|
| Unix **秒** | `1791584793` | 华尔街见闻 `display_time`、贴吧 `create_time` |
| Unix **毫秒** | `1791599913548` | 澎湃 `pubTimeLong`、`trackPublishTime` |
| ISO/空格字符串 | `"2026-10-10 20:38:27"` | 懂球帝 `published_at`、金十 `time` |
| RFC822 | `"Sat, 10 Oct 2026 12:04:04 +0000"` | 所有 RSS 的 `pubDate` |
| **占位 0** | `juejin` 的 `content.ctime = 0` | 字段在、值是假的 |
| 相对时间 | `"10小时前"` | 澎湃 `pubTime`(所以用它旁边那个绝对的 `publishTime`) |

**这个模块只干一件事**:把这些都变成**同一个 naive 本地 `datetime`**;
解析不出来**返回 `None`**(= 不知道),绝不猜。

## 三条纪律(都是踩出来的)

1. **不猜**。返回 `None` 是合法结果 —— 上层写库就是 NULL,不会被误读成
   "1970 年发的"。**编一个值比留空更坏**:它会让新鲜度分析整体偏向"全是很久以前"。
2. **占位值按"没给"处理**。`juejin` 的 `ctime=0` 若照收,就变成 1970-01-01 ——
   比不填更有害。所以拿时间戳**卡有效区间**(2000 年以前一律不收)。
3. **未来时间按"没给"处理**。比"现在"晚一天以上,基本是**时区没带上 / 解析错**,
   收下会把"新鲜度"算成负数。

⚠️ 本模块**不依赖 `hot_sources`**(它是被依赖方)—— 保持零依赖纯函数,便于单测。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: 早于此刻的**一律当"没给"** —— 挡住 `ctime=0` 这类**占位 0**(它会变成 1970-01-01)。
#: 取 2000-01-01:热榜场景不会有比它更早的"新鲜内容"。
_MIN_TS = 946_684_800

#: 比"现在"晚超过这么多天,**一律当"没给"**(时区没带 / 解析错的典型症状)。
_MAX_FUTURE = timedelta(days=1)

#: 大于它按**毫秒**看(2026 年的秒级时间戳约 1.79e9,毫秒级约 1.79e12)。
#: 阈值取 1e11:秒级要到公元 5138 年才够得着,毫秒级 1e11 则是 1973 年(本就该被区间挡掉)。
_MS_THRESHOLD = 1e11

_ISO_FALLBACK = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
_RE_DIGITS = re.compile(r"^\d{9,13}$")

#: **相对时间**(澎湃 `pubTime`、newsnow 的 `extra.date` 都这么给)。
#: ⚠️ 这是**粗粒度**的:源说"1天前",真实值在 24~48 小时之间 ——
#: 所以它**只够用来分档**(是不是 24h 内的新东西),**不够用来精确展示**。
#: 只认下面这几种**没有歧义**的写法;`前天`/`上周`/`一个月前` 一律**不认**(返回 None),
#: 因为"昨天"能算准、"上周"算不准 —— **宁可留空,不给一个错的时刻**。
_RE_REL = re.compile(r"^(\d+)\s*(秒|分钟|分|小时|天)前$")
_RE_TODAY = re.compile(r"^(今天|昨日|昨天)\s*(\d{1,2}):(\d{2})$")


def _from_relative(s: str, now: datetime) -> datetime | None:
    s = s.strip()
    if s in ("刚刚", "刚才"):
        return now
    m = _RE_REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"秒": timedelta(seconds=n), "分钟": timedelta(minutes=n), "分": timedelta(minutes=n),
                 "小时": timedelta(hours=n), "天": timedelta(days=n)}[unit]
        return now - delta
    m = _RE_TODAY.match(s)
    if m:
        day = now.date() if m.group(1) == "今天" else now.date() - timedelta(days=1)
        try:
            return datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _as_naive_local(dt: datetime) -> datetime:
    """带时区的换算到本地后**去掉 tzinfo** —— 库里存的是 naive(与 `captured_at` 同口径),
    两边能直接比大小。**不做这一步,RSS 的 `+0000` 会比 `captured_at` 早 8 小时**。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def _from_epoch(v: float) -> datetime | None:
    if v > _MS_THRESHOLD:
        v = v / 1000.0
    if v < _MIN_TS:
        return None
    try:
        return datetime.fromtimestamp(v)
    except (OverflowError, OSError, ValueError):
        return None


def _from_text(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    if _RE_DIGITS.match(s):                      # 数字**以字符串形态**给(很常见)
        return _from_epoch(float(s))
    try:                                         # `2026-10-10T12:04:04Z` / `+08:00`
        return _as_naive_local(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in _ISO_FALLBACK:                    # `2026-10-10 20:38:27`
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:                                         # RFC822(所有 RSS 的 `pubDate`)
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            return _as_naive_local(dt)
    except (TypeError, ValueError, IndexError):
        pass
    return _from_relative(s, datetime.now())     # 最后才试相对时间(粗粒度,见常量处注释)


def parse_published(value) -> datetime | None:
    """把任意平台的"发布时间"字段解析成 **naive 本地 datetime**;解析不出返回 `None`。

    `None` 是**合法且预期**的结果(不少源就是不给时间)—— 别把它当异常。
    只在"值看着像时间但没解析出来"时打一条 debug,便于回溯字段名猜错的情况。
    """
    if value is None or isinstance(value, bool):
        return None
    dt: datetime | None = None
    if isinstance(value, (int, float)):
        dt = _from_epoch(float(value))
    elif isinstance(value, str):
        dt = _from_text(value)
    if dt is not None and dt > datetime.now() + _MAX_FUTURE:
        logger.debug("发布时间解析出未来时刻,按未给处理:raw=%r parsed=%s", value, dt)
        return None
    if dt is None and value not in (None, "", 0):
        # ⚠️ **不做静默**:源里明明有值、我们却没读懂 —— 留一条痕,别让它无声消失
        logger.debug("发布时间解析失败(字段名或格式可能变了):raw=%r", value)
    return dt


#: **粗粒度**相对时间(抖音搜索卡上的那种):`1周前` / `3月前` / `5年前`。
#: ⚠️ 与 `_RE_REL` 分开写,因为两者的精度承诺**不同**(见 `parse_published_coarse`)。
_RE_REL_COARSE = re.compile(
    r"^(\d+)\s*(秒|分钟|分|小时|天|周|个月|月|年)前$")
_RE_DAY_WORD = {"昨天": 1, "前天": 2}
_RE_MD = re.compile(r"^(\d{1,2})月(\d{1,2})日$")
_RE_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

#: 粗粒度换算用的近似长度。**只用于"比某个日期更早吗"这种二分判断**,
#: 30 天/365 天这种近似在阈值上不会改变结论(实测抖音卡片只给到周/月/年)。
_COARSE_UNIT = {"秒": timedelta(seconds=1), "分钟": timedelta(minutes=1),
                "分": timedelta(minutes=1), "小时": timedelta(hours=1),
                "天": timedelta(days=1), "周": timedelta(days=7),
                "个月": timedelta(days=30), "月": timedelta(days=30),
                "年": timedelta(days=365)}


def parse_published_coarse(value) -> datetime | None:
    """**粗粒度**版本:能读懂 `N周前` / `N月前` / `N年前` / `昨天` / `M月D日`。

    ## 为什么不并进 `parse_published`

    那个函数是**精度承诺**:"宁可留空,不给一个错的时刻"(`前天`/`上周`/`一个月前`
    一律返回 None)。而抖音搜索卡**只给这种粗粒度** —— 卡片上是 `1小时前`/`2天前`/`5年前`,
    没有精确时刻。若也返回 None,下游的新鲜度闸会把**每一条**都当成"判不出时间"丢掉。

    ⇒ 这个函数**只用于阈值比较**("是不是早于 2026-10-01"),**不许用于展示**:
    `3月前` 的真实含义是 3~4 个月,拿它当"发布日期"印在卡上会是错的。

    ## 为什么近似不会改变结论

    新鲜度闸问的是"早于 2026-10-01 吗"。30 天/365 天这类近似带来的误差(几天)
    全部落在**同一个日期两侧**,不会把 10 月的内容判成 9 月、反之亦然。
    ⚠️ 一旦哪天要用它做**精确判断**,这个前提就不成立了 —— 那时该去拿绝对时间。
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    now = datetime.now()
    if s in ("刚刚", "刚才"):
        return now
    m = _RE_REL_COARSE.match(s)
    if m:
        return now - int(m.group(1)) * _COARSE_UNIT[m.group(2)]
    for word, days in _RE_DAY_WORD.items():
        if s.startswith(word):
            return now - timedelta(days=days)
    m = _RE_YMD.match(s)
    if m:                                   # 卡片上偶尔给绝对日期
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _RE_MD.match(s)                     # `9月30日`:补当年;若因此变成未来,退回上一年
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return dt if dt <= now + _MAX_FUTURE else dt.replace(year=now.year - 1)
    return None
