"""微博/百度热搜 → 大瓜词(2026-11-28)。

用户口径:「**微博热搜和百度热搜也加进去,之前的羊麦穗这里面都有过排名**」。

这个文件守两件最容易错的事:
1. **不许把"人名型"的瓜筛掉** —— 实测微博 51 条只有 2 条命中大瓜事件词,
   而「范丞丞你要毁了李晨吗」这类明显是瓜、事件词一个都不命中;用户举的
   「羊麦穗」同理(光有人名)。硬过滤恰好会漏掉用户点名要的那类。
2. **两榜要交替取** —— 娱乐条目多的微博会把百度挤光。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.db.models import BaiduHotItem, User, WeiboHotItem  # noqa: E402
from app.services import dagua_hot  # noqa: E402
from app.services.category_topics import dagua_score  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _seed(session, *, weibo=(), baidu=(), age_hours: float = 0.0) -> None:
    """按 (标题, 名次) 往两张表里灌一批;`age_hours` 用来造"时间窗外"的行。"""
    when = datetime.now() - timedelta(hours=age_hours)
    for title, rank in weibo:
        session.add(WeiboHotItem(user_id=1, title=title, heat=1, rank=rank, captured_at=when))
    for title, rank in baidu:
        session.add(BaiduHotItem(user_id=1, title=title, heat=1, rank=rank, url="", captured_at=when))
    session.commit()


class TestDaguaScore:
    def test_命中事件词才算分(self) -> None:
        assert dagua_score("董明珠直播训员工引争议") >= 1
        #: 两个事件词(塌房/道歉)⇒ 分数应比只中一个的高
        assert dagua_score("某顶流塌房后道歉") > dagua_score("某顶流塌房")
        assert dagua_score("工业经济新动能澎湃") == 0

    def test_空与None都是0(self) -> None:
        assert dagua_score("") == 0
        assert dagua_score(None) == 0


class TestRecentDaguaWords:
    def test_人名型的瓜不许被筛掉(self, session) -> None:
        """★★ 用户举的「羊麦穗」就一个人名 —— `dagua_score` 是 0,但**必须能取到**。

        实测依据:微博 51 条里只有 2 条命中事件词,而「范丞丞你要毁了李晨吗」
        「于东来现身深圳迪厅舞姿火辣」这些明显是瓜的得分都是 0。
        """
        _seed(session, weibo=[("羊麦穗", 3), ("范丞丞你要毁了李晨吗", 6)])
        words = dagua_hot.recent_dagua_words(session, 1, hours=24, limit=5)
        assert "羊麦穗" in words, words
        assert "范丞丞你要毁了李晨吗" in words, words

    def test_命中事件词的排在前面(self, session) -> None:
        _seed(session, weibo=[("某地推进工程建设", 1), ("某某官宣离婚", 40)])
        words = dagua_hot.recent_dagua_words(session, 1, hours=24, limit=2)
        assert words[0] == "某某官宣离婚", words      # 40 名但命中事件词,仍在第 1 位

    def test_两榜交替取_微博不会把百度挤光(self, session) -> None:
        _seed(session,
              weibo=[(f"微博词{i}", i + 1) for i in range(10)],
              baidu=[(f"百度词{i}", i + 1) for i in range(10)])
        words = dagua_hot.recent_dagua_words(session, 1, hours=24, limit=4)
        assert any(w.startswith("微博") for w in words), words
        assert any(w.startswith("百度") for w in words), words

    def test_时间窗外的不取(self, session) -> None:
        _seed(session, weibo=[("很久以前的瓜", 1)], age_hours=48)
        assert dagua_hot.recent_dagua_words(session, 1, hours=24, limit=5) == []

    def test_同一词多次上榜取最好名次(self, session) -> None:
        _seed(session, weibo=[("重复的瓜", 30)])
        session.add(WeiboHotItem(user_id=1, title="重复的瓜", heat=1, rank=2,
                                 captured_at=datetime.now()))
        session.commit()
        words = dagua_hot.recent_dagua_words(session, 1, hours=24, limit=5)
        assert words.count("重复的瓜") == 1, words

    def test_limit为0时返回空(self, session) -> None:
        _seed(session, weibo=[("某个瓜", 1)])
        assert dagua_hot.recent_dagua_words(session, 1, hours=24, limit=0) == []


class TestStore:
    def test_两榜都入库_空标题跳过(self, session) -> None:
        n = dagua_hot.store(session, 1, {
            "weibo": [{"title": "微博甲", "heat": 10, "rank": 1},
                      {"title": "  ", "heat": 0, "rank": 2}],
            "baidu": [{"title": "百度乙", "heat": 5, "rank": 3}]})
        assert n == 2
        assert session.query(WeiboHotItem).count() == 1
        assert session.query(BaiduHotItem).count() == 1


class TestTickSwitch:
    def test_cron为空就早退_一个请求都不打(self, monkeypatch) -> None:
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("关掉时不该采")

        monkeypatch.setattr(dagua_hot, "fetch_all", _boom)

        class _S:
            dagua_hot_cron = ""

        assert dagua_hot.dagua_hot_tick(_S()) == 0
        assert called["n"] == 0
