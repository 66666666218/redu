"""时间解析与格式化的统一工具(2026-10-05)。

**为什么要有它**:同一个"把各种输入解析成 datetime"的函数在仓库里**有两份**,
而且**语义还不一样** —— `wechat/_text._parse_time` 认 epoch 与带时区的 ISO 串,
`wechat_analyzer._parse_time` 认几个 `strptime` 格式。调用方看不出区别,
直到某天同一篇文的 `publish_at` 走两条路解析出不同的值(发布时段×阅读、近 N 天过滤
都会跟着偏)。

`to_dt` 是两者的**超集**(不是二选一),所以换过去不改语义。
"""
from __future__ import annotations

from datetime import datetime


def to_dt(value: object) -> datetime | None:
    """把各种形态的时间解析成 datetime;取不到返回 `None`(**不抛**)。

    接受的形态(两种老实现合起来的那一集):

    - `datetime` ⇒ **原样返回**(含 aware;`wechat_analyzer` 的老语义就是原样,
      改成"转本地再抹 tz"会对 aware 值造成偏移,所以这里保持原样);
    - 数字 / 数字串 ⇒ 当 **epoch 秒**;
    - ISO 串(含 `2026-01-01 12:00`、`2026-01-01T12:00:00Z`、纯日期)⇒ 解析。

    ⚠️ **带 Z / 偏移的 ISO 串是 UTC 墙钟**,而全库时间戳统一为"服务器本地 naive"
    (`datetime.now()`)。所以必须先 `astimezone()` 转本地再抹 `tzinfo`,
    否则会把 UTC 当本地存 —— **发布时段×阅读、近 N 天过滤会整体偏一个时区**
    (naive 串 `astimezone()` 按本地解释、值不变,安全)。这条是原实现在注释里
    用血的教训换来的,搬过来时一字未改。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return datetime.fromtimestamp(int(s))
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except (ValueError, OSError, TypeError):
        return None
