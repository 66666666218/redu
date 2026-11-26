"""小红书「求1型」扫描的**纯逻辑**测试(2026-10-30)。不开浏览器、不联网。

浏览器那半(`search_notes`/`note_comments`)只能在**人开着并登录过的**那个 CDP 浏览器上实测,
所以这里守的是:
1. 判据与抖音**同一套**(密度/重复率来自 `demand_watch` 的归一化);
2. CDP 不可达时**明确 skip**,不自己拉起浏览器(脚本拉起的浏览器 `webdriver=True`,那条路就废了)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import xhs_demand as X


def test_评论统计用同一套判据():
    texts = ["1", "1", "求完整版", "还有吗", "求", "好看", "1", "求链接", "哈哈", "1"]
    st = X.stats_from_texts(texts)

    assert st["total"] == 10
    assert st["ones"] == 4
    assert st["demand"] == 4          # 求完整版 / 求 / 求链接 / 还有吗(「有吗」也算求)
    assert st["dup_ratio"] > 0


def test_重复短句也按归一算():
    texts = ["刚看完", "刚看完[笑哭]", "刚看完。", "冰露", "好看", "1"]
    st = X.stats_from_texts(texts)
    assert st["dup_ratio"] >= 0.3


class _S:
    demand_xhs_enabled = True
    demand_seed_words = "小小绘"


def test_CDP不可达时明确跳过_不自己开浏览器(monkeypatch):
    from app.services import demand_watch as dw

    monkeypatch.setattr(X, "available", lambda settings=None: (False, "CDP 不可达"))
    out = dw.scan_xhs(None, 1, _S())

    assert out["skipped"] == "CDP 不可达"


def test_关掉开关就不碰浏览器(monkeypatch):
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_enabled = False

    monkeypatch.setattr(X, "available", lambda settings=None: pytest.fail("关了开关不该探活"))
    assert dw.scan_xhs(None, 1, S())["skipped"] == "disabled"


# ---------------------------------------------------------------- 小样本不判

class _Page:
    def close(self):
        pass


class _Ctx:
    def new_page(self):
        return _Page()


class _Browser:
    contexts = [_Ctx()]

    def close(self):
        pass


class _P:
    def stop(self):
        pass


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401
    from app.db.database import Base
    from app.db.models import User

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def test_挂了群聊入口的笔记也要记一笔(session, monkeypatch):
    """用户:「小红书大多都会有加入群聊」—— 群消息读不到,但**挂没挂群**是个信号。"""
    from app.db.models import DemandScan
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_min_comments = 8
        demand_xhs_notes_per_seed = 1

    monkeypatch.setattr(X, "available", lambda settings=None: (True, "ok"))
    monkeypatch.setattr(X, "_connect", lambda: (_P(), _Browser()))
    monkeypatch.setattr(X, "search_notes", lambda *a, **k: [
        {"id": "n2", "token": "t", "title": "刚创了一个八卦群，想进就进", "author": "A",
         "comments": 0}])
    monkeypatch.setattr(X, "note_comments", lambda *a, **k: [])   # 评论为空
    monkeypatch.setattr(X, "page_has_group", lambda page: True)
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: pytest.fail("不该推"))

    dw.scan_xhs(session, 1, S())

    row = session.query(DemandScan).one()
    assert row.has_group is True
    assert row.why == "群聊入口"


# ---------------------------------------------------- 发布时间(2026-11-26 订正)

def test_卡片时间文案能解析成秒级时间戳():
    """★ 实测(78 条样本)搜索卡只有这四种形状,**不许靠猜字段名**:

    | 形状 | 实例 |
    |---|---|
    | 相对 | `2分钟前` / `4小时前` / `3天前` |
    | 昨天带时刻 | `昨天 21:11` |
    | 只有月日 | `11-12` / `10-05` |
    | 绝对日期 | `2025-08-21` |
    """
    from datetime import datetime

    now = datetime.now()
    for s in ("2分钟前", "4小时前", "3天前", "54分钟前", "昨天 21:11",
              "11-12", "10-05", "2025-08-21"):
        ts = X._ts_from_card_time(s)
        assert ts > 0, f"{s!r} 应能解析"
        assert abs((now - datetime.fromtimestamp(ts)).days) < 4000
    # 相对时间要落在正确量级上(粗粒度只够分档,但分档必须对)
    assert (now - datetime.fromtimestamp(X._ts_from_card_time("2分钟前"))).total_seconds() < 600
    assert 3 <= (now - datetime.fromtimestamp(X._ts_from_card_time("3天前"))).total_seconds() / 86400 < 4


def test_没见过的文案一律返回0_不猜时间():
    """判不出就是判不出 —— 猜一个时间出来会让新鲜度闸基于假数据放行/拦截。"""
    for s in ("", None, "乱码文案", "置顶", "赞 12"):
        assert X._ts_from_card_time(s) == 0


def test_只有月日时补年_落到未来就退回上一年():
    from datetime import datetime

    now = datetime.now()
    for s in ("01-01", "12-31", "11-12"):
        ts = X._ts_from_card_time(s)
        assert ts > 0
        assert datetime.fromtimestamp(ts) <= now, f"{s} 不该解析成未来时刻"


class _FakeSearchPage:
    """只喂 `search_notes` 需要的最小页面:能 goto、能 evaluate。"""

    def __init__(self, feeds):
        self.feeds = feeds
        self.goto_urls = []

    def goto(self, url, **k):
        self.goto_urls.append(url)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        #: 直接返回"页内 JS 已经拿到的形状"——这里要测的是 **Python 侧怎么用**
        #: `cornerTagInfo`,不是页内 JS 本身。
        return self.feeds


def test_search_notes从cornerTagInfo取发布时间(monkeypatch):
    """★★ 回归:此前取 `noteCard.time`/`lastUpdateTime`,实测这两个字段**恒为 null**
    ⇒ `published` 永远是 0 ⇒ **发布时间闸一次都没生效过**(用户 11-24 投诉的那条)。
    """
    page = _FakeSearchPage([
        {"id": "n1", "token": "t1", "title": "A", "author": "x", "comments": 10,
         "published": 0, "pub_text": "4小时前"},
        {"id": "n2", "token": "t2", "title": "B", "author": "y", "comments": 20,
         "published": 0, "pub_text": "3天前"},
    ])
    rows = X.search_notes(page, "吃瓜群", limit=2)
    by_id = {r["id"]: r for r in rows}
    assert by_id["n1"]["published"] > 0
    assert by_id["n2"]["published"] > 0
    assert by_id["n2"]["published"] < by_id["n1"]["published"], "3天前应早于4小时前"


def test_search_notes_毫秒时间戳仍然化到秒(monkeypatch):
    """字段回潮时的兼容:哪天 `noteCard.time` 又给了毫秒,别把 `fromtimestamp` 炸掉。"""
    page = _FakeSearchPage([
        {"id": "n1", "token": "t", "title": "A", "author": "x", "comments": 5,
         "published": 1795688117000, "pub_text": ""},
    ])
    rows = X.search_notes(page, "吃瓜", limit=1)
    assert rows[0]["published"] < 10 ** 11, "必须是秒"


# ---------------------------------------------- 平台之间不共用 try(2026-11-26)

def _patch_tick_session(monkeypatch, session):
    """`demand_watch_tick` 自己 `get_session_local()` 开会话 —— 换成本测试那个。"""
    import app.db as app_db

    monkeypatch.setattr(app_db, "get_session_local", lambda: (lambda: session))


def test_抖音抛异常也不跳过小红书(session, monkeypatch):
    """★★ 实测 11-26 04:53 / 08:53 两轮:抖音 `discover_latest` 抛
    `DouyinProtocolError(need_login)` ⇒ **小红书那段一起被跳过**,
    而 `runs` 里小红书什么都没有 = 与「调度没触发」长得一模一样。
    """
    from app.services import demand_watch as dw

    class S:
        demand_latest_enabled = True
        demand_expand_enabled = True
        demand_xhs_enabled = True

    called = []

    def boom(*a, **k):
        raise RuntimeError("need_login")

    _patch_tick_session(monkeypatch, session)
    monkeypatch.setattr(dw, "run_user", lambda db, uid, st: {"pushed": 0})
    monkeypatch.setattr(dw, "discover_latest", boom)
    monkeypatch.setattr(dw, "discover_names", boom)
    monkeypatch.setattr(dw, "scan_xhs", lambda db, uid, st: called.append(uid))

    dw.demand_watch_tick(S())

    assert called == [1], "抖音挂了,小红书仍必须跑"


def test_小红书异常不影响主流程(session, monkeypatch):
    from app.services import demand_watch as dw

    class S:
        demand_latest_enabled = False
        demand_expand_enabled = False
        demand_xhs_enabled = True

    _patch_tick_session(monkeypatch, session)
    monkeypatch.setattr(dw, "run_user", lambda db, uid, st: {"pushed": 1})
    monkeypatch.setattr(dw, "scan_xhs",
                        lambda db, uid, st: (_ for _ in ()).throw(RuntimeError("page crashed")))

    assert dw.demand_watch_tick(S()) == 1, "小红书炸了不该把整轮算失败/不返回"


# ------------------------------------- 小红书自己的运行记录必须区分三种结局

def test_CDP不可达要写skipped记录(session, monkeypatch):
    """不写记录的话,「调度没触发」与「CDP 连不上」在 `runs` 里都是空白。"""
    from app.db.models import RunRecord
    from app.services import demand_watch as dw

    monkeypatch.setattr(X, "available", lambda settings=None: (False, "CDP 不可达"))
    dw.scan_xhs(session, 1, _S())

    row = session.query(RunRecord).filter_by(kind="demand_watch_xhs").one()
    assert row.status == "skipped" and "CDP 不可达" in row.detail


def test_页面异常要写failed记录(session, monkeypatch):
    from app.db.models import RunRecord
    from app.services import demand_watch as dw

    monkeypatch.setattr(X, "available", lambda settings=None: (True, "ok"))

    def boom():
        raise RuntimeError("TargetClosedError")

    monkeypatch.setattr(X, "_connect", boom)
    out = dw.scan_xhs(session, 1, _S())

    assert "error" in out
    row = session.query(RunRecord).filter_by(kind="demand_watch_xhs").one()
    assert row.status == "failed" and "TargetClosedError" in row.detail


def test_评论太少只记不判(session, monkeypatch):
    """用户给的「小马云」那条只有 4 条评论 —— 样本太小时密度是噪音。"""
    from app.db.models import DemandScan
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_min_comments = 8
        demand_xhs_notes_per_seed = 1

    monkeypatch.setattr(X, "available", lambda settings=None: (True, "ok"))
    monkeypatch.setattr(X, "_connect", lambda: (_P(), _Browser()))
    monkeypatch.setattr(X, "search_notes", lambda *a, **k: [
        {"id": "n1", "token": "t", "title": "小马云也是放飞自我了#范小勤", "author": "A",
         "comments": 20}])
    monkeypatch.setattr(X, "note_comments", lambda *a, **k: ["求🥹", "好看", "有", "好新鲜"])
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: pytest.fail("样本不足不该推"))

    out = dw.scan_xhs(session, 1, S())

    assert out["scanned"] == 1 and out["flagged"] == 0
    row = session.query(DemandScan).one()
    assert row.why == "样本不足" and row.flagged is False
