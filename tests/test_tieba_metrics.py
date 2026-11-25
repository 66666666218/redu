"""贴吧点赞(agree):用 aiotieba 补 MediaCrawler 拿不到的那个指标(2026-10-04)。

实测结论(14 条真实帖子):6 条 `agree` 非 0 —— **真有值,不是恒 0**。
⚠️ 单看第一条会以为"全是 0",所以这里专门钉住"别拿一个样本下结论"的那类边界。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import tieba_metrics as tm  # noqa: E402


def test_agree_to_metrics_drops_zero_and_none() -> None:
    """★ 0 / None **不放进去** —— 那是"平台没给",放进去会被 `conversion` 读成"没人赞"。"""
    assert tm.agree_to_metrics(24) == {"liked_count": 24}
    assert tm.agree_to_metrics(0) == {}
    assert tm.agree_to_metrics(None) == {}
    assert tm.agree_to_metrics("5") == {"liked_count": 5}
    assert tm.agree_to_metrics("n/a") == {}


def test_fetch_agree_coerces_tids_to_int(monkeypatch) -> None:
    """★ `tid` **必须是 int** —— `aiotieba.get_posts` 对字符串直接报
    `'str' object cannot be interpreted as an integer`(实测踩过这个坑)。"""
    seen: list[int] = []

    async def _fake(tids, limit):
        seen.extend(tids)
        return {str(t): {"agree": 7, "text": ""} for t in tids}

    monkeypatch.setattr(tm, "_detail_of", _fake)
    out = tm.fetch_agree(["11071928513", 9879165365, "bad", None])
    assert all(isinstance(t, int) for t in seen), f"传下去的 tid 不是 int: {seen}"
    assert seen == [11071928513, 9879165365]          # 垃圾值被丢掉,不炸
    assert out == {"11071928513": 7, "9879165365": 7}


def test_fetch_agree_never_raises(monkeypatch) -> None:
    """★ 这是"补一个指标"的**旁路** —— 拿不到**不该拖垮发现链**,所以它不抛异常。"""
    async def _boom(tids, limit):
        raise RuntimeError("贴吧抽风了")

    monkeypatch.setattr(tm, "_detail_of", _boom)
    assert tm.fetch_agree(["11071928513"]) == {}

    def _no_aiotieba(tids, limit):
        raise ImportError("No module named 'aiotieba'")
    monkeypatch.setattr(tm, "_detail_of", _no_aiotieba)
    assert tm.fetch_agree(["11071928513"]) == {}       # 没装也不炸(只是跳过)


def test_fetch_agree_empty_input() -> None:
    assert tm.fetch_agree([]) == {}
    assert tm.fetch_agree(["", "  "]) == {}


def test_limit_is_applied(monkeypatch) -> None:
    """一次别问太多 —— 每个帖子都是一次网络请求。"""
    async def _fake(tids, limit):
        return {str(t): {"agree": 1, "text": ""} for t in tids[:limit]}

    monkeypatch.setattr(tm, "_detail_of", _fake)
    out = tm.fetch_agree([str(1000 + i) for i in range(20)], limit=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# 全帖指标(2026-11-25)
# ---------------------------------------------------------------------------


def test_detail_to_metrics_全帖字段全映射() -> None:
    m = tm.detail_to_metrics({"view_num": 705811, "reply_num": 14279,
                              "share_num": 253, "collect_num": 587,
                              "thread_agree": 28344, "agree": 5115})
    assert m == {"view_count": 705811, "comment_count": 14279, "share_count": 253,
                 "collected_count": 587, "liked_count": 28344}


def test_点赞取全帖值_不是首楼() -> None:
    """★ 实测 tid 10745031844:全帖 28344 / 首楼 5115 —— 差 82%。"""
    m = tm.detail_to_metrics({"thread_agree": 28344, "agree": 5115})
    assert m["liked_count"] == 28344


def test_全帖点赞为0时回落首楼() -> None:
    assert tm.detail_to_metrics({"thread_agree": 0, "agree": 7})["liked_count"] == 7
    assert tm.detail_to_metrics({"agree": 7})["liked_count"] == 7


def test_detail_to_metrics_零和缺失都不放() -> None:
    """与 `agree_to_metrics` 同规矩:0/None = "平台没给",放进去会被读成"没人看"。"""
    assert tm.detail_to_metrics(None) == {}
    assert tm.detail_to_metrics({}) == {}
    assert tm.detail_to_metrics({"view_num": 0, "share_num": None,
                                 "reply_num": "n/a", "thread_agree": 0}) == {}


def test_有浏览量就能走_view_count_档() -> None:
    """★ 贴吧从 `engage` 兜底跳到 `view_count` —— 这是接全帖指标最大的收益。"""
    from app.services import conversion as cv

    m = tm.detail_to_metrics({"view_num": 705811, "thread_agree": 28344})
    r = cv.estimate("tieba", m)
    assert r["basis"] == "view_count"
    assert r["estimate"] == int(705811 * cv.DEFAULT_FACTOR)
