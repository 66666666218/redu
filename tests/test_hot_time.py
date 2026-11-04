

def test_publish_mark_一小时内外与缺省():
    """卡上的时间标记:**一小时内 🆕**,其余 `·MM-DD HH:MM`,拿不到就空串。

    2026-10-31 起这是**所有板块共用的一份**(用户:「每个板块都要看得到资源时间的新鲜度」)。
    """
    import time as _t

    from app.services.hot_time import publish_mark

    now = int(_t.time())
    assert publish_mark(now) == "🆕"
    assert publish_mark(now - 86400).startswith("·")
    assert publish_mark(0) == "" and publish_mark(None) == ""


def test_publish_mark_毫秒与越界不许炸():
    """★ 2026-11-04 事故:小红书/快手/贴吧的 `publish_at` 有的是**毫秒**,
    `datetime.fromtimestamp` 在 Windows 抛 `OSError [Errno 22]`,把**整轮 presence 打断**,
    三个平台推送从 10-31 起归零。

    要求:毫秒要能换算成秒;真正越界/垃圾值返回空串,**绝不许抛**。
    """
    import time as _t

    from app.services.hot_time import publish_mark

    ms = int(_t.time() * 1000)
    assert publish_mark(ms) in ("🆕",) or publish_mark(ms).startswith("·")
    assert publish_mark(999999999999999) == ""       # 越界 → 空串
    assert publish_mark(1) == ""                     # 1970 → 空串(不是异常)
    assert publish_mark("abc") == ""
