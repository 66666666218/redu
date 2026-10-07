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
