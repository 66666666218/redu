"""`app/services/hot_cycle.py` 的判据守卫。

这些断言**全部来自真实实测**(2026-11-08/09 跑热点宝拿回的数据),不是拍的:
- `_belongs` 的 10 个 case 里有 4 个是实测里**真实出现过**的误判/命中;
- `board_words` 的输入用的是**真实榜面条目形状**(`key_word` + 小时级 `trends`),
  因为最容易犯的错是拿日级序列去判「当天首现」。
"""
from __future__ import annotations

from app.services import hot_cycle


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
