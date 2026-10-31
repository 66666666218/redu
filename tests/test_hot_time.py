

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
