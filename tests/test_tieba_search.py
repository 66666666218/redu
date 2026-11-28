"""贴吧**纯协议搜索**测试(2026-10-08)。全部离线打桩,**一个请求都不发**。

这里守的是三件容易错的事:
1. **盘链只在首楼全文里** —— 搜索返回的摘要是**截断的**,直接拿它抽链会得到 0 条
   (A/B 实测过);所以"带详情"与"不带详情"必须是两个不同的结果,不能看起来都对。
2. **硬失败要抛**,不许返回空列表 —— 否则"贴吧挂了"会被读成"今天没人发资源"。
3. **`with_detail=False` 要真的省掉那些请求** —— 否则 `resource_presence` 会白花 N 次。
"""
from __future__ import annotations

import pytest

from app.services import tieba_metrics as tm
from app.services import tieba_search as ts


class _Obj:
    """`aiotieba.SearchGlobal` 的替身(只放我们读的那几个字段)。"""

    def __init__(self, tid: int, title: str = "", content: str = "",
                 author_name: str = "甲", author_id: int = 9,
                 create_time: int = 1791358442, forum_name: str = "影视剧",
                 post_num: int = 1) -> None:
        self.tid = tid
        self.title = title
        self.content = content
        self.author_name = author_name
        self.author_id = author_id
        self.create_time = create_time
        self.forum_name = forum_name
        self.post_num = post_num


#: 摘要**被截断在链接之前** —— 这正是实测的样子
EXCERPT = "#网盘资源# 《天道》全集4K百度网盘完整版资源_百度网盘下载_百度云网盘资源整理好了,链"
#: 首楼全文里才有真链
FULL = ("《天道》全集4K百度网盘完整版观看下载链接:\n百度网盘:\n"
        "https://pan.baidu.com/s/1cpqLxmrFGsgLSD1uOinFgQ\n提取码:knm2\n")


@pytest.fixture
def patched(monkeypatch):
    """把 aiotieba 那两层换成桩,并记录 `fetch_posts_detail` 被调了几次。"""
    calls: dict[str, list] = {"detail": []}

    def _mk_search(objs):
        async def _s(keyword, pages, rn):
            return list(objs)
        return _s

    def _patch(objs, details=None):
        monkeypatch.setattr(ts, "_search_async", _mk_search(objs))

        def _fake_detail(tids, limit=8):
            calls["detail"].append(list(tids))
            return details or {}

        monkeypatch.setattr(tm, "fetch_posts_detail", _fake_detail)
        return calls

    return _patch


# ---------------------------------------------------------------------------
# 形状映射
# ---------------------------------------------------------------------------


def test_映射成与_mediacrawler_同一形状(patched):
    patched([_Obj(11084400332, title="《天道》", content=EXCERPT)],
            details={"11084400332": {"agree": 5, "text": FULL}})
    rows = ts.search(["网盘资源"])
    assert len(rows) == 1
    r = rows[0]
    assert r["url"] == "https://tieba.baidu.com/p/11084400332"
    assert r["uid"] == "9" and r["name"] == "甲"
    assert r["keyword"] == "网盘资源"
    assert r["publish_at"] == 1791358442          # ★ 比 MediaCrawler 那条多给的
    assert r["forum_name"] == "影视剧"
    # 回复数 → comment_count;点赞(取全文时顺带拿到)→ liked_count
    assert r["metrics"]["comment_count"] == 1
    assert r["metrics"]["liked_count"] == 5
    # 贴吧**没有转发数**:列成 0 是为兼容老列,不是"没人转"
    assert r["share_count"] == 0


def test_全帖指标接上_且点赞用全帖值而非首楼(patched):
    """★ 2026-11-25:`get_posts` 的 `thread` 段匿名就有值(**同一次请求**,白拿)。

    实测 tid 10745031844:全帖 `agree` **28344** / 首楼 `agree` **5115**
    —— 用首楼会把热度少报 82%。所以 `liked_count` 必须取 `thread_agree`。
    """
    patched([_Obj(10745031844, title="《天道》", content=EXCERPT)],
            details={"10745031844": {"agree": 5115, "text": FULL,
                                     "thread_agree": 28344, "view_num": 705811,
                                     "reply_num": 14279, "share_num": 253,
                                     "collect_num": 587}})
    r = ts.search(["网盘资源"])[0]
    assert r["metrics"]["liked_count"] == 28344, "必须用全帖点赞,不是首楼那层的"
    assert r["metrics"]["view_count"] == 705811
    assert r["metrics"]["share_count"] == 253
    assert r["metrics"]["collected_count"] == 587
    # share_count 顶层是兼容老列;有真值时不该再硬编码 0
    assert r["share_count"] == 253


def test_全帖值为0时才回落到首楼点赞(patched):
    """兜底:全帖值缺失/为 0 时用首楼值,但**不许反过来**丢掉已知的全帖值。"""
    patched([_Obj(1, content=EXCERPT)],
            details={"1": {"agree": 7, "text": FULL, "thread_agree": 0}})
    assert ts.search(["网盘资源"])[0]["metrics"]["liked_count"] == 7


def test_盘链从首楼全文抽出来(patched):
    """★ 核心:摘要在链接之前就断了,**链只在全文里**。"""
    patched([_Obj(1, title="《天道》", content=EXCERPT)],
            details={"1": {"agree": 0, "text": FULL}})
    rows = ts.search(["网盘资源"])
    assert rows[0]["pan_link"].startswith("https://pan.baidu.com/s/")
    assert "1cpqLxmrFGsgLSD1uOinFgQ" in rows[0]["pan_link"]


def test_不带详情时抽不到链_这正是它存在的理由(patched):
    """反面对照 —— 证明"取首楼"那一步**不是可有可无**的。

    它守的是一条**事实**:搜索接口给的摘要里**没有链**(被截断在链接之前)。
    哪天贴吧开始把正文也给全,这条会红 —— 那时就可以省掉每个帖子的详情请求了。

    ⚠️ **别把它当成"顺序守卫"**:我试过把 `_row` 里的
    `_pan_links(full) or _pan_links(excerpt)` 换成摘要优先,这条**照样绿** ——
    因为两边都没链时结果相同。那是个**等价变异**,不是漏网。
    """
    patched([_Obj(1, title="《天道》", content=EXCERPT)])
    rows = ts.search(["网盘资源"], with_detail=False)
    assert rows[0]["pan_link"] == "", "摘要里不该抽得出链(能抽到就说明判据变了)"


def test_没有_aweme_id_的条目丢掉(patched):
    patched([_Obj(0, content=EXCERPT)])
    assert ts.search(["网盘资源"]) == []


# ---------------------------------------------------------------------------
# 省请求:两条路要真的不同
# ---------------------------------------------------------------------------


def test_with_detail_False_一次详情都不取(patched):
    """★ `resource_presence` 走这条 —— 它只数条数,不该为每个帖子多花一次请求。"""
    calls = patched([_Obj(1), _Obj(2), _Obj(3)],
                    details={"1": {"agree": 0, "text": FULL}})
    ts.search(["词"], with_detail=False)
    assert calls["detail"] == [], f"不该取详情,实际取了 {calls['detail']}"


def test_with_detail_True_会取详情(patched):
    calls = patched([_Obj(7, content=EXCERPT)], details={"7": {"agree": 0, "text": FULL}})
    ts.search(["词"])
    assert calls["detail"] == [[7]], f"应当取详情,实际 {calls['detail']}"


def test_详情上限由参数封顶(patched):
    """每个帖子一次请求 ⇒ 必须有上限,否则一轮能把配额打光。"""
    calls = patched([_Obj(100 + i) for i in range(20)])
    ts.search(["词"], detail_limit=3)
    assert calls["detail"] == [[100, 101, 102]]


# ---------------------------------------------------------------------------
# 失败必须响亮
# ---------------------------------------------------------------------------


def test_搜索抛错要抛_TiebaSearchError_而不是空列表(monkeypatch):
    """★ 最重要的一条:返回 `[]` 会把"贴吧今天挂了"读成"今天没人发资源"。"""
    async def _boom(keyword, pages, rn):
        raise RuntimeError("贴吧抽风")

    monkeypatch.setattr(ts, "_search_async", _boom)
    monkeypatch.setattr(tm, "fetch_posts_detail", lambda tids, limit=8: {})
    with pytest.raises(ts.TiebaSearchError) as e:
        ts.search(["词"])
    assert e.value.kind == "api"


def test_没装_aiotieba_单独归类(monkeypatch):
    async def _no(keyword, pages, rn):
        raise ImportError("No module named 'aiotieba'")

    monkeypatch.setattr(ts, "_search_async", _no)
    monkeypatch.setattr(tm, "fetch_posts_detail", lambda tids, limit=8: {})
    with pytest.raises(ts.TiebaSearchError) as e:
        ts.search(["词"])
    assert e.value.kind == "dependency"


def test_没有关键词就不发请求(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("不该发请求")

    monkeypatch.setattr(ts, "_search_async", _boom)
    assert ts.search(["", "  ", None]) == []


# ---------------------------------------------------------------------------
# 去重与拍平
# ---------------------------------------------------------------------------


def test_同一条帖子被两个词命中只留一次(patched):
    objs = [_Obj(5, content=EXCERPT), _Obj(5, content=EXCERPT)]
    calls = patched(objs)
    # 两个词各返回同一个 tid ⇒ 结果里只该有一条(详情也只取一次)
    rows = ts.search(["词一", "词二"])
    assert len(rows) == 1
    assert calls["detail"] == [[5]]


def test_拍平_fraglink_时要把_raw_url_带进来():
    """`FragLink` 的真链在 `raw_url`/`text`(深链)里,**不在** `title` ——
    只拍 `title` 会把链接整个丢掉。"""
    class _Frag:
        def __init__(self, text="", title="", raw_url=""):
            self.text, self.title, self.raw_url = text, title, raw_url
    text = tm.flatten_contents([_Frag(text="前言"),
                                _Frag(title="百度网盘",
                                       raw_url="tiebaclient://x?url=https%3A%2F%2Fpan.baidu.com%2Fs%2FAAA")])
    assert "pan.baidu.com" in text and "前言" in text


def test_编码过的链也能抽出来():
    """首楼里的链常常是 `tiebaclient://…url=https%3A%2F%2F…` 这种**编码过**的深链。"""
    enc = "tiebaclient://x?url=https%3A%2F%2Fpan.baidu.com%2Fs%2F1cpqLxmrFGsgLSD1uOinFgQ"
    assert ts._pan_links(enc), "反转义之后应当能抽到链"


def test_搜索对象没时间时用首楼详情兜底() -> None:
    """★ 2026-10-18:aiotieba 的**搜索结果不给** `create_time`(贴吧那条链 57% 无发布时间),

    而**首楼详情里有** ⇒ 用详情兜底(那次请求本来就在打,不额外花一次)。
    """
    obj = _Obj(tid=1, title="某剧全集", content="", create_time=0)
    row = ts._row(obj, "某词", {"agree": 1, "text": "", "create_time": 1791358442})
    assert row and row["publish_at"] == 1791358442


def test_搜索对象有时间就直接用() -> None:
    obj = _Obj(tid=2, title="某剧", create_time=1791000000)
    row = ts._row(obj, "某词", None)
    assert row and row["publish_at"] == 1791000000


# ---------------------------------------------------------------------------
# 详情配额怎么分
# ---------------------------------------------------------------------------


def _multi_kw_patch(monkeypatch):
    """每个关键词返回**不同的一批**帖子,并记录详情被问了哪些 tid。"""
    calls: dict[str, list] = {"detail": []}
    base = {"甲": 1000, "乙": 2000, "丙": 3000}

    async def _by_kw(keyword, pages, rn):
        return [_Obj(base[keyword] + i) for i in range(10)]

    def _fake_detail(tids, limit=8):
        calls["detail"].append(list(tids))
        return {}

    monkeypatch.setattr(ts, "_search_async", _by_kw)
    monkeypatch.setattr(tm, "fetch_posts_detail", _fake_detail)
    monkeypatch.setattr(ts, "_GAP", 0)
    return calls


def test_详情配额按关键词轮转_而不是让第一个词吃满(monkeypatch) -> None:
    """★★ 2026-11-28:`cands` 是**按词顺序**堆的(第一个词的全部 → 第二个词的全部),
    直接 `cands[:limit]` 会让队首那个词吃满配额 —— 其余资源名在卡片上仍然没有指标。
    presence 一轮 8 个名字 × 每词最多 50 条,而配额只有 30:实测就是这样。
    """
    calls = _multi_kw_patch(monkeypatch)
    ts.search(["甲", "乙", "丙"], detail_limit=6)
    got = calls["detail"][0]
    assert got == [1000, 2000, 3000, 1001, 2001, 3001], got


def test_配额比词数还少时也先每个词各给一条(monkeypatch) -> None:
    """`limit < 词数` 时不能只给前几个词 —— 每个名字至少一条,才好判断"哪个词真没有"。"""
    calls = _multi_kw_patch(monkeypatch)
    ts.search(["甲", "乙", "丙"], detail_limit=2)
    assert calls["detail"][0] == [1000, 2000]


def test_单关键词时顺序与原实现一致(monkeypatch) -> None:
    """轮转不能把单关键词的取样顺序打乱(那是最常见的调用形态)。"""
    calls = _multi_kw_patch(monkeypatch)
    ts.search(["甲"], detail_limit=3)
    assert calls["detail"][0] == [1000, 1001, 1002]
