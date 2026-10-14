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
    """阶段行现在**只用来提供信号标签**(`parts`);阶段是什么**不再影响取样**。"""
    session.add(AgentStage(user_id=1, board="weibo", kw="某资源名", norm=NORM,
                           stage="苗头", score=80, parts="共振",
                           updated_at=datetime.now() - timedelta(days=3)))
    session.commit()


def _push(session, *, kw: str = KW, days_ago: int = 3, repost: int | None = None) -> None:
    """**一条推送记录** = 一个回测样本(2026-10-14 起样本源从「苗头/上升阶段行」换成它)。

    ⚠️ 为什么必须换:阶段表是**一行一个词、阶段原地升级**,早期阶段**存不下来**
    (远程实测 287 行全是「爆发」)⇒ 老取样恒为空、`backtested` 恒 0、权重永远不更新。
    `repost=None` = **这条还没结算** ⇒ 标签退回热度(那是设计内的退路,不是缺陷)。
    """
    row = HotspotSuggestion(user_id=1, keyword=kw, growth=0, kind="llm",
                            created_at=datetime.now() - timedelta(days=days_ago))
    if repost is not None:
        row.settled_at = datetime.now()
        row.repost_gain = repost
    session.add(row)
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
        _push(session, repost=0)                      # 结算说"没传播开"

        out = al.backtest_and_learn(session, 1)
        assert out["backtested"] == 1, out
        assert out["labels"] == {"结算": 1, "热度": 0}, out
        assert out["hits"] == 0, "没传播开就是没命中 —— 老口径会把它算成 hit"

    def test_传播开了就是hit_哪怕热度没涨(self, session, monkeypatch) -> None:
        """反向:热度没涨(50→50),但资源真被疯转(repost_gain=9)⇒ **hit**。"""
        _stage(session)
        _series(monkeypatch, [50, 50, 50])
        _push(session, repost=9)                      # 结算说"真传播开了"

        out = al.backtest_and_learn(session, 1)
        assert out["labels"] == {"结算": 1, "热度": 0}, out
        assert out["hits"] == 1, out

    def test_没结算的退回热度标签_不能丢样本(self, session, monkeypatch) -> None:
        """★ **反向里的反向**:没结算的建议**不能因此不记样本** —— 那会让样本大量流失,
        自学习直接停摆(样本永远凑不齐)。"""
        _stage(session)
        _series(monkeypatch, [100, 100, 300])       # 热度涨了 ⇒ 退回热度时算 hit
        _push(session)                              # 有推送、但**故意不结算**

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
        _push(session, kw="小米18Pro 防窥屏", repost=0)

        out = al.backtest_and_learn(session, 1)
        assert out["labels"]["结算"] == 1, f"带空格的关键词没对上:{out}"


def test_样本来自推送_阶段表里没有苗头行也照样回测(session, monkeypatch) -> None:
    """★ **2026-10-14 的回归守卫**:老取样查「`AgentStage` 里 stage∈{苗头,上升} 且 2 天前」,
    而那张表**一行一个词、阶段原地升级** ⇒ 早期阶段存不下来(远程 287 行全是「爆发」)
    ⇒ 取样恒为空 ⇒ 权重永远不更新。现在样本源是**推送记录**,与阶段表当前状态无关。"""
    _stage(session)                     # 阶段行给信号标签(注意:它这里是"苗头",但真有它也行)
    session.query(AgentStage).delete()  # ⚠️ 把阶段表清空 —— 老口径下这里就回测不出东西了
    session.commit()
    _series(monkeypatch, [100, 100, 300])
    _push(session, repost=1)
    out = al.backtest_and_learn(session, 1)
    assert out["backtested"] == 1, f"取样不该依赖阶段表的当前状态:{out}"
    assert out["hits"] == 1


def test_推送不足两天不回测_还没到能判断的时候(session, monkeypatch) -> None:
    _stage(session)
    _series(monkeypatch, [100, 100, 300])
    _push(session, days_ago=0, repost=9)
    assert al.backtest_and_learn(session, 1)["backtested"] == 0, "刚推出去的样本不该当天就判"


def test_有结算的样本_没有热度序列也要回测(session, monkeypatch) -> None:
    """★★ **2026-10-14 的第二个回归守卫**:实测这些建议的词是**资源/需求类**
    (`下载`/`问卷`/`人格测试`/剧名…),与热榜词表**几乎不重叠(198 条里只有 2 条能对到序列)**
    ⇒ 若"先判序列、再看结算",**41 条有结算的样本会被一起挡掉**(等于没修)。
    结算标签**不需要任何序列** ⇒ 它必须排在前头。"""
    _series(monkeypatch, [])            # 故意:四路序列全是空的(模拟"词表不重叠")
    _push(session, kw="下载", repost=3)
    _push(session, kw="问卷", repost=0)
    out = al.backtest_and_learn(session, 1)
    assert out["backtested"] == 2, f"有结算就该回测,与有没有热度序列无关:{out}"
    assert out["hits"] == 1 and out["labels"] == {"结算": 2, "热度": 0}, out


def test_归因数带出来_样本有而归因没有时要能看见(session, monkeypatch) -> None:
    """⚠️ 归因只存在 `AgentStage.parts` 里,而**被推送的词从没被早期信号打过标签** ⇒
    实测 41 条已结算样本里 0 条能对上 ⇒ 权重那一步永远过不去。**必须有个数说出来**,
    否则它静默成"学习在跑、只是没调",与"没有样本"长得一模一样。"""
    _series(monkeypatch, [])            # 无序列 ⇒ 也顺带证明结算路不依赖它
    _push(session, kw="下载", repost=3)
    out = al.backtest_and_learn(session, 1)
    assert out["backtested"] == 1
    assert out["attributed"] == 0, "没有阶段行 ⇒ 归因数必须是 0(而不是悄悄等于样本数)"
    assert out["weights"] == al.DEFAULT_WEIGHTS, "归因 0 时权重一个都不许动"
