"""Agent 学习的**标签**:从"话题热不热"换成"资源真的传播开了没有"(2026-10-07)。

## 为什么改
`HotspotSuggestion.repost_gain`(每晚 22:00 结算)= 发文后**全网新增的该文盘链数**,
也就是"这条资源**真的被疯转了**没有" —— 比"关键词热度涨没涨"更接近拉新要的东西。
**它一直在库里算着,却从没被回测用过**(只被结算端自己消费)。

改完之后:
  · 该关键词**有已结算的建议** ⇒ **以结算为准**;
  · 没有 ⇒ 退回热度标签 —— **不能因为"没结算"就不记样本**,那会让样本大量流失。

## 这个文件测的就是那条分界线
**热度涨了 200%、但资源没传播开** ⇒ 必须记成 **miss**(老口径会记成 hit)。
这正是"接上结算"的全部意义。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import AgentStage, HotspotSuggestion, User  # noqa: E402
from app.services import agent_learning as al  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


KW = "某资源名"
NORM = "某资源名"


def _stage(session) -> None:
    session.add(AgentStage(user_id=1, board="weibo", kw="某资源名", norm=NORM,
                           stage="苗头", score=80, parts="共振",
                           updated_at=datetime.now() - timedelta(days=3)))
    session.commit()


def _series(monkeypatch, values, key=NORM):
    """热度序列:老标签看的那个东西。"""
    class _Repo:
        @staticmethod
        def weibo_heat_series(db, uid):
            # ⚠️ 返回的是 **dict(归一化关键词 → [(t, 值)])** 而不是裸 list ——
            # 调用侧是 `series_map.get(board, {}).get(norm)`(实测踩到)。
            return {key: [(datetime.now() - timedelta(days=4 - i), v)
                          for i, v in enumerate(values)]}

        @staticmethod
        def xianyu_want_series(db, uid):
            return []

        @staticmethod
        def douhot_score_series(db, uid):
            return []

        @staticmethod
        def baidu_heat_series(db, uid):
            return []
    monkeypatch.setattr(al, "repository", _Repo)


class TestSettlementLabelWins:
    def test_热度翻了倍但没传播开_要记成miss(self, session, monkeypatch) -> None:
        """★★ 核心:热度 100→300(+200%,老口径=hit),而结算 repost_gain=0(没传播)
        ⇒ **必须记成 miss** —— 否则这个改动等于没做。"""
        _stage(session)
        _series(monkeypatch, [100, 100, 300])
        session.add(HotspotSuggestion(user_id=1, keyword=KW, growth=0, kind="llm",
                                      settled_at=datetime.now(), repost_gain=0))
        session.commit()

        out = al.backtest_and_learn(session, 1)
        assert out["backtested"] == 1, out
        assert out["labels"] == {"结算": 1, "热度": 0}, out
        assert out["hits"] == 0, "没传播开就是没命中 —— 老口径会把它算成 hit"

    def test_传播开了就是hit_哪怕热度没涨(self, session, monkeypatch) -> None:
        """反向:热度没涨(50→50),但资源真被疯转(repost_gain=9)⇒ **hit**。"""
        _stage(session)
        _series(monkeypatch, [50, 50, 50])
        session.add(HotspotSuggestion(user_id=1, keyword=KW, growth=0, kind="llm",
                                      settled_at=datetime.now(), repost_gain=9))
        session.commit()

        out = al.backtest_and_learn(session, 1)
        assert out["labels"] == {"结算": 1, "热度": 0}, out
        assert out["hits"] == 1, out

    def test_没结算的退回热度标签_不能丢样本(self, session, monkeypatch) -> None:
        """★ **反向里的反向**:没结算的建议**不能因此不记样本** —— 那会让样本大量流失,
        自学习直接停摆(样本永远凑不齐)。"""
        _stage(session)
        _series(monkeypatch, [100, 100, 300])       # 热度涨了 ⇒ 老口径 hit
        session.commit()                            # 故意不加建议

        out = al.backtest_and_learn(session, 1)
        assert out["backtested"] == 1, "没结算也要记样本"
        assert out["labels"] == {"结算": 0, "热度": 1}, out
        assert out["hits"] == 1

    def test_关键词要按同一口径归一才认得出(self, session, monkeypatch) -> None:
        """⚠️ 实测:`stage.kw='小米18Pro 防窥屏'` 而 `norm='小米18pro防窥屏'`,
        建议表存的是**原始 keyword**。不归一化两边永远对不上(而且**不报错**,只是永远走不到)。"""
        session.add(AgentStage(user_id=1, board="weibo", kw="小米18Pro 防窥屏",
                               norm="小米18pro防窥屏", stage="苗头", score=80, parts="共振",
                               updated_at=datetime.now() - timedelta(days=3)))
        session.commit()
        _series(monkeypatch, [100, 100, 300], key="小米18pro防窥屏")
        session.add(HotspotSuggestion(user_id=1, keyword="小米18Pro 防窥屏", growth=0, kind="llm",
                                      settled_at=datetime.now(), repost_gain=0))
        session.commit()

        out = al.backtest_and_learn(session, 1)
        assert out["labels"]["结算"] == 1, f"带空格的关键词没对上:{out}"
