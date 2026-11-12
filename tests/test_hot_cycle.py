"""`app/services/hot_cycle.py` 的判据守卫。

这些断言**全部来自真实实测**(2026-11-08/09 跑热点宝拿回的数据),不是拍的:
- `_belongs` 的 10 个 case 里有 4 个是实测里**真实出现过**的误判/命中;
- `board_words` 的输入用的是**真实榜面条目形状**(`key_word` + 小时级 `trends`),
  因为最容易犯的错是拿日级序列去判「当天首现」。
"""
from __future__ import annotations

import pytest

from app.services import hot_cycle

#: 模块导入时就抓住真函数 —— 后面的 autouse fixture 会把它替掉,
#: 探针自身的用例靠这份引用装回来(见 `_real_carrier_words`)。
_ORIG_CARRIER_WORDS = hot_cycle.carrier_words


@pytest.fixture(autouse=True)
def _no_carrier_probe(monkeypatch):
    """**默认关掉形态词探针**。

    `_candidates` 的第一条路就是探针,而 `getattr(object(), "hot_carrier_enabled", True)`
    是 True ⇒ 不管这些老用例本来想测什么,都会先去打真实 HTTP(词表 2026-11-12 起 6 个)。
    老用例各自要测的是「榜单/种子族」那条,所以这里统一把探针替成空。
    探针本身的用例用 `_real_carrier_words` 这个 fixture 把真函数装回去。
    """
    monkeypatch.setattr(hot_cycle, "carrier_words",
                        lambda *a, **kw: ([], []) if kw.get("return_groups") else [])


@pytest.fixture
def _real_carrier_words(monkeypatch):
    """把被 `_no_carrier_probe` 替掉的 `carrier_words` **换回真函数**。

    ⚠️ 必须从模块里重新取:`from app.services import hot_cycle` 拿到的模块对象上,
    属性已经被 fixture 覆盖了 ⇒ 只有 `importlib.reload` 或事先留一份原始引用才行。
    这里用 `_ORIG` 保存的原始引用(模块导入时就抓下来的那个函数对象)。
    """
    monkeypatch.setattr(hot_cycle, "carrier_words", _ORIG_CARRIER_WORDS)


def _trend(points: int, last: int, start: int = 1000) -> list[dict]:
    """造一条长度为 points 的序列,末点量为 last。"""
    vals = [start] * (points - 1) + [last]
    return [{"date": f"2026-11-09 {i:02d}:00:00", "value": v} for i, v in enumerate(vals)]


class _FakeClient:
    """替身:按 `sub_type` 回不同的榜面条目(形状照抄真实接口)。"""

    def __init__(self, boards: dict[int, list[dict]]) -> None:
        self.boards = boards
        self.calls: list[tuple[int, int]] = []

    def hot_search(self, limit: int = 20, date_window: int = 1,
                   sub_type: int = 3001, keyword: str = "") -> list[dict]:
        self.calls.append((sub_type, date_window))
        return self.boards.get(sub_type, [])


# ---- 「同族」判据 ---------------------------------------------------------

def test_同族判据_真事件的各种说法都认():
    """实测:墨尔本事件有 13 种说法,`覆面系` 是 `黑白覆面塌` 的简称。"""
    assert hot_cycle._belongs("墨尔本华人外卖员遇刺身亡", "墨尔本七个小矮人")
    assert hot_cycle._belongs("中国骑手墨尔本遇害", "墨尔本七个小矮人")
    assert hot_cycle._belongs("黑白覆面塌", "覆面系")
    assert hot_cycle._belongs("玲玲夫妇是什么瓜", "玲玲")


def test_同族判据_只共用一个字的路线人必须挡掉():
    """实测:`墨尔本七个小矮人` 会返回 `接亲七个枣核含义` —— 只共用「七」。"""
    assert not hot_cycle._belongs("接亲七个枣核含义", "墨尔本七个小矮人")
    assert not hot_cycle._belongs("接亲七个枣核寓意", "墨尔本七个小矮人")
    assert not hot_cycle._belongs("华为七个摄像头", "覆面系")
    #: 3 个字以内的种子只要求 2 字公共片段,长种子要求 3 字 —— 这里 2 字不够
    assert not hot_cycle._belongs("大耳朵图图", "兔兔图图")


def test_同族判据_省掉虚字的名字也认():
    """`狗爹和小桃` → `狗爹小桃`:推广号写名字时常把「和」省掉。"""
    assert hot_cycle._belongs("狗爹小桃vlog合集", "狗爹和小桃")


def test_最长公共子串():
    assert hot_cycle._lcs_len("墨尔本七个小矮人", "墨尔本华人外卖员遇刺身亡") == 3
    assert hot_cycle._lcs_len("覆面系", "黑白覆面塌") == 2
    assert hot_cycle._lcs_len("abc", "xyz") == 0
    assert hot_cycle._lcs_len("", "abc") == 0


# ---- 「首现几天」判据 ----------------------------------------------------

def test_首现天数从末尾往前数():
    vals = [500, 800, 20000, 30000, 40000]
    #: 末尾连续 3 天 >= 底噪(1 万)⇒ 有量的第 3 天 ⇒ 首现距今 2 天
    assert hot_cycle._emerge_days(vals) == 2
    assert hot_cycle._emerge_days([1000, 20000]) == 0
    assert hot_cycle._emerge_days([20000, 20000]) == 1


def test_序列全在底噪以下判不出来():
    assert hot_cycle._emerge_days([100, 500]) == -1
    assert hot_cycle._emerge_days([]) == -1


# ---- ★ 无种子通路:发现独立事件 -------------------------------------------

def test_无种子通路_只收当天新起的_常青词不要(monkeypatch):
    """真实形状:新事件日点 2~5,`kpl`/`仙逆` 这类常青词是 24 点满序列。"""
    boards = {
        3001: [
            {"key_word": "黄仁勋发声明承认出轨", "trends": _trend(2, 46_933_228)},
            {"key_word": "现场观战BLG vs T1", "trends": _trend(1, 3_983_200)},
            {"key_word": "kpl", "trends": _trend(24, 14_628_010)},
            {"key_word": "仙逆", "trends": _trend(24, 21_895_864)},
        ],
        3002: [
            {"key_word": "blg对决t1比分", "trends": _trend(3, 735_406)},
            {"key_word": "量太小的事件", "trends": _trend(2, 50_000)},
        ],
    }
    fake = _FakeClient(boards)
    monkeypatch.setattr(
        "app.services.douhot_client.DouhotClient",
        lambda *a, **kw: fake)

    rows = hot_cycle.board_words("cookie", None)
    words = [h.word for h in rows]
    assert "黄仁勋发声明承认出轨" in words
    assert "现场观战BLG vs T1" in words
    assert "blg对决t1比分" in words
    #: 常青词日点满 24,不是"新事件"
    assert "kpl" not in words and "仙逆" not in words
    #: 低于 20 万门槛的长尾词条
    assert "量太小的事件" not in words
    #: 两个子榜都要拉
    assert {s for s, _ in fake.calls} == {3001, 3002}
    assert all(dw == 1 for _, dw in fake.calls)


def test_无种子通路_刚上榜的排前面(monkeypatch):
    boards = {3001: [
        {"key_word": "晚两天的事件", "trends": _trend(5, 90_000_000)},
        {"key_word": "今天刚起的事件", "trends": _trend(2, 300_000)},
    ], 3002: []}
    monkeypatch.setattr("app.services.douhot_client.DouhotClient",
                        lambda *a, **kw: _FakeClient(boards))
    rows = hot_cycle.board_words("cookie", None)
    #: 点数少 = 更新 ⇒ 排前面,哪怕量小
    assert [h.word for h in rows] == ["今天刚起的事件", "晚两天的事件"]


def test_无种子通路_泛流程词不要(monkeypatch):
    boards = {3001: [
        {"key_word": "晋级半决赛", "trends": _trend(2, 3_000_000)},
        {"key_word": "现场观战BLG vs T1", "trends": _trend(2, 3_000_000)},
    ], 3002: []}
    monkeypatch.setattr("app.services.douhot_client.DouhotClient",
                        lambda *a, **kw: _FakeClient(boards))
    assert [h.word for h in hot_cycle.board_words("cookie", None)] == ["现场观战BLG vs T1"]


def test_一个榜挂了另一个榜照用(monkeypatch):
    from app.services.douhot_client import DouhotError

    class _Half(_FakeClient):
        def hot_search(self, limit=20, date_window=1, sub_type=3001, keyword=""):
            if sub_type == 3001:
                raise DouhotError("boom")
            return self.boards.get(sub_type, [])

    boards = {3002: [{"key_word": "blg对决t1比分", "trends": _trend(3, 735_406)}]}
    monkeypatch.setattr("app.services.douhot_client.DouhotClient",
                        lambda *a, **kw: _Half(boards))
    assert [h.word for h in hot_cycle.board_words("cookie", None)] == ["blg对决t1比分"]


# ---- 取词入口 -------------------------------------------------------------

def test_取词优先无种子通路_取不到才用种子族(monkeypatch):
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    monkeypatch.setattr(hot_cycle, "board_words",
                        lambda c, s: [hot_cycle.HotWord("新事件", 1_000_000, 1_000_000, 2, 0, "搜索榜")])
    called: list[str] = []
    monkeypatch.setattr(hot_cycle, "emerging",
                        lambda *a, **kw: called.append("x") or [])
    assert hot_cycle.hot_video_seeds(object(), ["狗爹和小桃"], limit=2) == ["新事件"]
    assert called == []          #: 无种子通路有结果就不该再走种子族


def test_无种子通路空_退回种子族(monkeypatch):
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    monkeypatch.setattr(hot_cycle, "board_words", lambda c, s: [])
    monkeypatch.setattr(hot_cycle, "emerging",
                        lambda *a, **kw: [hot_cycle.HotWord("小小甄选", 5_000_000, 5_000_000, 3, 0, "小小绘")])
    assert hot_cycle.hot_video_seeds(object(), ["小小绘"], limit=2) == ["小小甄选"]


def test_没有Cookie时不出网(monkeypatch):
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "")

    def _boom(*a, **kw):
        raise AssertionError("没有 Cookie 不该发请求")

    monkeypatch.setattr(hot_cycle, "board_words", _boom)
    assert hot_cycle.hot_video_seeds(object(), ["小小绘"]) == []


def test_整条链挂了也不抛(monkeypatch):
    """热点是「加分项」:热点宝挂了不能把整轮作业带崩。"""
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")

    def _boom(*a, **kw):
        raise RuntimeError("热点宝 502")

    monkeypatch.setattr(hot_cycle, "board_words", _boom)
    monkeypatch.setattr(hot_cycle, "emerging", _boom)
    assert hot_cycle.hot_video_seeds(object(), ["小小绘"]) == []


# ---- 形态词探针(2026-11-10)---------------------------------------------
#
# 这些 case 的两两相似度、序列点数、峰值**全部来自 2026-11-10 的真实探针结果**,
# 不是拍的。校准依据见 `hot_cycle.CARRIER_MERGE_JACCARD` 的注释。

class _KeywordClient:
    """替身:按 `keyword` 回条目(形状照抄真实接口:`key_word` + 小时级 `trends`)。"""

    def __init__(self, by_kw: dict[str, list[dict]]) -> None:
        self.by_kw = by_kw
        self.calls: list[str] = []

    def hot_search(self, limit: int = 50, date_window: int = 1,
                   sub_type: int = 3001, keyword: str = "") -> list[dict]:
        self.calls.append(keyword)
        return self.by_kw.get(keyword, [])


def _item(word: str, peak: int, points: int = 10) -> dict:
    return {"key_word": word, "trends": _trend(points, peak)}


def _use_client(monkeypatch, client) -> None:
    monkeypatch.setattr("app.services.douhot_client.DouhotClient",
                        lambda *a, **kw: client)


def test_同事件判据_实测的只忍一族能连上():
    """真实校准样本(2026-11-10)。"""
    #: 这三个是同一族里能连上的那一簇
    assert hot_cycle._same_event("泼粪离职事件处理", "泼粪离职事件忍5次视频")       # 0.417
    assert hot_cycle._same_event("泼粪离职事件处理", "彻底疯狂泼粪离职事件回放")     # 0.385
    assert hot_cycle._same_event("彻底疯狂泼粪离职事件回放", "离职泼粪事件回放")     # 0.385
    #: `老板骂我5次就离职后续` 那一簇
    assert hot_cycle._same_event("老板骂我5次就离职后续", "老板骂我5次就离职直播回放")  # 0.571
    assert hot_cycle._same_event("老板骂我5次就离职后续", "老板骂5次就离职回放")       # 0.462


def test_同事件判据_不同事件不许连():
    """实测:两簇之间最高只有 0.273/0.231 ⇒ 0.3 正好把它们分开。"""
    assert not hot_cycle._same_event("泼粪离职事件处理", "老板骂我5次就离职后续")   # 0.062
    assert not hot_cycle._same_event("泼粪离职事件处理", "只忍老板五次直播视频")     # 0.000
    assert not hot_cycle._same_event("中方回应黄乐", "滑县婚礼照搬事件")
    #: 空串/单字不该炸,也不该判同
    assert not hot_cycle._same_event("", "事件")
    assert not hot_cycle._same_event("a", "b")


def test_二元组去掉标点():
    assert hot_cycle._bigrams("a b") == {"ab"}
    assert hot_cycle._bigrams("删,除") == {"删除"}
    assert hot_cycle._bigrams("") == set()


def test_归并_传递闭包把一族的全部说法并成一组():
    """`_group_events` 是差分的底座:**整组**要一起记,代表只取峰值最大那条。

    只忍一族实测 10 种说法,`_merge_events` 只交代表;`_group_events` 交全组。
    """
    words = [hot_cycle.HotWord(w, p, p, 10, 0, "形态词:离职") for w, p in [
        ("泼粪离职事件处理", 100), ("泼粪离职事件忍5次视频", 500),
        ("彻底疯狂泼粪离职事件回放", 300), ("离职泼粪事件回放", 200),
        ("完全无关的事件", 900),
    ]]
    groups = hot_cycle._group_events(words)
    assert len(groups) == 2
    #: 组按峰值降序
    assert groups[0][0].word == "完全无关的事件"
    fam = groups[1]
    assert fam[0].word == "泼粪离职事件忍5次视频"          #: 峰值 500 当代表
    assert {h.word for h in fam} == {
        "泼粪离职事件处理", "泼粪离职事件忍5次视频",
        "彻底疯狂泼粪离职事件回放", "离职泼粪事件回放"}


def test_归并_代表就是峰值最大的那条():
    words = [hot_cycle.HotWord("滑县婚礼照搬事件", 300, 300, 8, 0, "形态词:事件"),
             hot_cycle.HotWord("滑县婚礼照搬原因", 100, 100, 5, 0, "形态词:原因")]
    assert [h.word for h in hot_cycle._merge_events(words)] == ["滑县婚礼照搬事件"]


def test_探针_常青词按序列点数挡掉(monkeypatch, _real_carrier_words):
    """满 24 点是常青词(`财经直播`);只忍那条是 13 点。"""
    _use_client(monkeypatch, _KeywordClient({"后续": [
        _item("财经直播后续", 5_000_000, points=24),
        _item("只忍老板五次直播视频后续", 160_000, points=13),
    ]}))
    rows = hot_cycle.carrier_words("cookie", None, carriers=("后续",))
    assert [h.word for h in rows] == ["只忍老板五次直播视频后续"]


def test_探针_峰值太低挡掉(monkeypatch, _real_carrier_words):
    _use_client(monkeypatch, _KeywordClient({"后续": [
        _item("小事件后续", 9_000),
        _item("大事件后续", 2_000_000),
    ]}))
    rows = hot_cycle.carrier_words("cookie", None, carriers=("后续",))
    assert [h.word for h in rows] == ["大事件后续"]


def test_探针_不沾形态词的条目丢掉(monkeypatch, _real_carrier_words):
    """接口声称按 keyword 过滤,但**不假定它一定照做**。"""
    _use_client(monkeypatch, _KeywordClient({"后续": [
        _item("完全不沾边的词", 5_000_000),
        _item("真事件后续", 800_000),
    ]}))
    rows = hot_cycle.carrier_words("cookie", None, carriers=("后续",))
    assert [h.word for h in rows] == ["真事件后续"]


def test_探针_一个词挂了不影响其余(monkeypatch, _real_carrier_words):
    from app.services.douhot_client import DouhotError

    class _Half(_KeywordClient):
        def hot_search(self, limit=50, date_window=1, sub_type=3001, keyword=""):
            if keyword == "回应":
                raise DouhotError("boom")
            return self.by_kw.get(keyword, [])

    _use_client(monkeypatch, _Half({"事件": [_item("滑县婚礼照搬事件", 3_000_000)]}))
    rows = hot_cycle.carrier_words("cookie", None, carriers=("回应", "事件"))
    assert [h.word for h in rows] == ["滑县婚礼照搬事件"]


def test_探针_返回分组且代表在组首(monkeypatch, _real_carrier_words):
    _use_client(monkeypatch, _KeywordClient({"离职": [
        _item("老板骂我5次就离职后续", 600_000),
        _item("老板骂我5次就离职直播回放", 400_000),
        _item("另一个事件离职", 900_000),
    ]}))
    reps, groups = hot_cycle.carrier_words("cookie", None, carriers=("离职",),
                                           return_groups=True)
    assert len(groups) == 2
    assert [h.word for h in reps] == [g[0].word for g in groups]
    assert {h.word for h in groups[1]} == {"老板骂我5次就离职后续", "老板骂我5次就离职直播回放"}


def test_探针_top只截事件数_不截说法(monkeypatch, _real_carrier_words):
    _use_client(monkeypatch, _KeywordClient({"离职": [
        _item("老板骂我5次就离职后续", 600_000),
        _item("老板骂我5次就离职直播回放", 400_000),
        _item("另一个事件离职", 900_000),
    ]}))
    reps, groups = hot_cycle.carrier_words("cookie", None, carriers=("离职",),
                                           top=1, return_groups=True)
    assert len(reps) == 1 and reps[0].word == "另一个事件离职"
    assert len(groups) == 1


def test_探针_阈值走配置项(monkeypatch, _real_carrier_words):
    """配置项必须真生效(参数默认是 None,不能把常量固化在函数签名里)。"""
    _use_client(monkeypatch, _KeywordClient({"后续": [_item("某事件后续", 200_000)]}))

    class _S:
        hot_carrier_min_peak = 500_000
        hot_carrier_full_points = 23
        hot_carrier_top_n = 5
        hot_carrier_merge_jaccard = 0.3

    assert hot_cycle.carrier_words("cookie", _S(), carriers=("后续",)) == []
    assert hot_cycle.carrier_words("cookie", None, carriers=("后续",)) != []


# ---- 差分 / 记账 / 清理 ---------------------------------------------------

@pytest.fixture
def _mem_db():
    """内存库(带上 `hot_word_seen` 表)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


def _hw(word: str, peak: int = 200_000) -> "hot_cycle.HotWord":
    return hot_cycle.HotWord(word, peak, peak, 10, 0, "形态词:后续")


def test_差分_第一次全是新的_第二次一条不剩(_mem_db, monkeypatch):
    """这是本模块存在的意义:不差分就是 4 小时推一次同一件事。"""
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    monkeypatch.setattr(hot_cycle, "_candidates", lambda *a, **kw: (
        [_hw("某事件后续"), _hw("另一个事件后续")],
        [[_hw("某事件后续")], [_hw("另一个事件后续")]]))
    first = hot_cycle.new_hot_words(object(), [], session=_mem_db, user_id=1, limit=5)
    assert [h.word for h in first] == ["某事件后续", "另一个事件后续"]
    second = hot_cycle.new_hot_words(object(), [], session=_mem_db, user_id=1, limit=5)
    assert second == []


def test_差分按事件组判_换一种说法也不算新的(_mem_db, monkeypatch):
    """只忍一族实测 10 种说法:记了代表词之后,变体涨上来不许再当新事件。"""
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    rep = _hw("老板骂我5次就离职后续", 600_000)
    variant = _hw("老板骂我5次就离职直播回放", 400_000)
    #: 第一轮:组里代表是 A
    monkeypatch.setattr(hot_cycle, "_candidates", lambda *a, **kw: (
        [rep, variant], [[rep, variant]]))
    assert [h.word for h in hot_cycle.new_hot_words(
        object(), [], session=_mem_db, user_id=1, limit=5)] == [rep.word]
    #: 第二轮:同名事件,但变体涨成了代表 ⇒ 仍然是旧事件
    monkeypatch.setattr(hot_cycle, "_candidates", lambda *a, **kw: (
        [variant, rep], [[variant, rep]]))
    assert hot_cycle.new_hot_words(object(), [], session=_mem_db, user_id=1, limit=5) == []


def test_记账把整组说法都写进去(_mem_db):
    rows = [_hw("泼粪离职事件处理"), _hw("泼粪离职事件忍5次视频")]
    assert hot_cycle.remember(_mem_db, 1, rows) == 2
    got = hot_cycle.seen_before(_mem_db, 1, ["泼粪离职事件处理", "泼粪离职事件忍5次视频"])
    assert got == {"泼粪离职事件处理", "泼粪离职事件忍5次视频"}


def test_记账重复调用只更新不新增(_mem_db):
    assert hot_cycle.remember(_mem_db, 1, [_hw("某事件后续", 100)]) == 1
    #: 第二次峰值更高 ⇒ 更新 peak,不新增行
    assert hot_cycle.remember(_mem_db, 1, [_hw("某事件后续", 900)]) == 0
    from sqlalchemy import select

    from app.db.models import HotWordSeen
    row = _mem_db.scalars(select(HotWordSeen)).one()
    assert row.peak == 900


def test_差分按用户隔离(_mem_db):
    """两个用户的 `hot_word_seen` 不能互相污染。"""
    hot_cycle.remember(_mem_db, 1, [_hw("某事件后续")])
    assert hot_cycle.seen_before(_mem_db, 2, ["某事件后续"]) == set()


def test_清理只删过期的(_mem_db):
    from datetime import datetime, timedelta

    hot_cycle.remember(_mem_db, 1, [_hw("还热的后续"), _hw("凉了的后续")])
    from sqlalchemy import select

    from app.db.models import HotWordSeen
    old = _mem_db.scalars(
        select(HotWordSeen).where(HotWordSeen.word == "凉了的后续")).one()
    old.last_at = datetime.now() - timedelta(days=30)
    _mem_db.flush()
    assert hot_cycle.prune_seen(_mem_db, 1, keep_days=14) == 1
    assert hot_cycle.seen_before(_mem_db, 1, ["还热的后续"]) == {"还热的后续"}
    assert hot_cycle.seen_before(_mem_db, 1, ["凉了的后续"]) == set()


def test_不传session也能跑_自己开短会话(monkeypatch):
    """⚠️ 写锁纪律:`session=None` 时不能借调用方的长事务。"""
    opened = []

    class _S:
        def __init__(self): self.committed = False
        def commit(self): self.committed = True
        def close(self): pass

    inst = _S()

    class _Maker:
        def __call__(self):
            opened.append(1)
            return inst

    monkeypatch.setattr("app.db.get_session_local", lambda: _Maker())
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    rep = _hw("某事件后续")
    monkeypatch.setattr(hot_cycle, "_candidates", lambda *a, **kw: (
        [rep], [[rep]]))
    monkeypatch.setattr(hot_cycle, "seen_before", lambda *a: set())
    monkeypatch.setattr(hot_cycle, "remember", lambda *a, **kw: 1)
    monkeypatch.setattr(hot_cycle, "prune_seen", lambda *a, **kw: 0)

    out = hot_cycle.new_hot_words(object(), [], user_id=1, limit=5)
    assert [h.word for h in out] == ["某事件后续"]
    assert opened, "必须自己开会话"
    assert inst.committed, "自己开的会话必须提交(否则下一轮又判成新事件)"


def test_没登录态时不做差分也不碰库(monkeypatch):
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "")

    def _boom(*a, **kw):
        raise AssertionError("没有 Cookie 不该碰库")

    monkeypatch.setattr(hot_cycle, "seen_before", _boom)
    assert hot_cycle.new_hot_words(object(), [], session=object(), user_id=1) == []


def test_新热点入口走降级链_探针有结果就不看榜单(monkeypatch):
    monkeypatch.setattr(hot_cycle, "read_cookie", lambda s: "cookie")
    called = []
    monkeypatch.setattr(hot_cycle, "board_words", lambda *a: called.append("board") or [])
    rep = _hw("探针来的后续")
    monkeypatch.setattr(hot_cycle, "carrier_words", lambda *a, **kw: (
        [rep], [[rep]]) if kw.get("return_groups") else [rep])
    out = hot_cycle._candidates(object(), [], "cookie", limit=5)
    assert [h.word for h in out] == ["探针来的后续"]
    assert called == []


# ---- 探针词喂 douyin_leads 搜索(2026-11-12)--------------------------------

def _seen_row(db, word: str, peak: float = 1000.0, *, fed=None, last_hours=0.0):
    from datetime import datetime, timedelta

    from app.db.models import HotWordSeen

    #: `fed` 直接收 datetime(传字符串 SQLAlchemy 会拒)
    if isinstance(fed, str):
        fed = datetime.fromisoformat(fed)
    db.add(HotWordSeen(user_id=1, word=word, seen_at=datetime.now(),
                       last_at=datetime.now() - timedelta(hours=last_hours),
                       peak=peak, fed_search_at=fed))
    db.flush()


def test_取词只拿没喂过且还热的(_mem_db):
    _seen_row(_mem_db, "没喂过的大事件", 3000.0)
    _seen_row(_mem_db, "没喂过的小事件", 100.0)
    _seen_row(_mem_db, "喂过的旧词", 9000.0, fed="2026-11-12 10:00:00")
    _seen_row(_mem_db, "凉掉的词", 8000.0, last_hours=48)
    words = hot_cycle.unfed_search_words(_mem_db, 1, top=3)
    assert "没喂过的大事件" in words and "没喂过的小事件" in words
    assert "喂过的旧词" not in words, "喂过没产出的不再喂,否则烧搜索额度"
    assert "凉掉的词" not in words, "凉词不占名额"


def test_取词按峰值降序_长词挡掉(_mem_db):
    _seen_row(_mem_db, "小峰值", 100.0)
    _seen_row(_mem_db, "大峰值", 9999.0)
    #: 13 字:探针词带形态词后缀,放宽到 12;超长的事件句搜了也命不中
    _seen_row(_mem_db, "特别特别特别长的事件句子后续", 5000.0)
    assert hot_cycle.unfed_search_words(_mem_db, 1, top=3) == ["大峰值", "小峰值"]


def test_取词按用户隔离(_mem_db):
    _seen_row(_mem_db, "别人的词", 100.0)
    assert hot_cycle.unfed_search_words(_mem_db, 2, top=3) == []


def test_打戳只动喂过的那些(_mem_db):
    from datetime import datetime

    _seen_row(_mem_db, "词甲", 100.0)
    _seen_row(_mem_db, "词乙", 100.0)
    n = hot_cycle.mark_fed_search(_mem_db, 1, ["词甲"])
    assert n == 1
    #: 词乙的戳必须还是 None(下轮还该被喂)
    assert hot_cycle.unfed_search_words(_mem_db, 1, top=5) == ["词乙"]
    #: 重复打戳幂等(值刷新,不新增行)
    assert hot_cycle.mark_fed_search(_mem_db, 1, ["词甲", "不存在的词"]) == 1
