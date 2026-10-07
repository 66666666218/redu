"""学习台账单测(2026-10-07)。

这个模块的**唯一价值**是"不许在样本不够时下结论 + 结论要留痕"。
所以下面每条用例都在盯这两件事之一,而不是"函数跑通了没有"。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: E402,F401
from app.db.database import Base  # noqa: E402
from app.db.models import SystemConfig, User  # noqa: E402
from app.services import learning_ledger as ll  # noqa: E402

NOW = datetime.now()


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _ctx(**kw) -> dict:
    base = {
        "ordering": {"groups": []},
        "delivery": {"chains": {}, "current_24h": {"迅雷群": {}}},
        "hot": {"by_norm": {}, "freq": {}},
    }
    base.update(kw)
    return base


class TestWeiboFirst:
    """★ 这条主张**必须带对照**:三榜采样密度差 >2× 时,"谁最先"量的是采集频率,结论作废。"""

    @staticmethod
    def _multi(pairs: int = 30, weibo_first: int = 24) -> dict:
        """造 `pairs` 组重叠标题,其中 `weibo_first` 组微博最先,其余百度最先。"""
        by_norm = {}
        for i in range(pairs):
            a = NOW - timedelta(hours=10)
            b = NOW
            if i < weibo_first:
                by_norm[f"词条{i}"] = {"微博": a, "百度": b}
            else:
                by_norm[f"词条{i}"] = {"百度": a, "微博": b}
        return by_norm

    def test_采样密度相当时可以下结论(self) -> None:
        v = ll._hypo_weibo_first(_ctx(hot={"by_norm": self._multi(),
                                          "freq": {"微博": 300, "抖音": 300, "百度": 380}}))
        assert v.ok is True and v.status == ll.OK
        assert v.n == 30

    def test_采样密度差太大就作废(self) -> None:
        """⚠️ 微博每小时采一次、抖音每天采一次的话,"微博更早"**只是我们采样粗**。
        这种时候必须返回"证据不足",而不是照着 24/30 判"成立"。"""
        v = ll._hypo_weibo_first(_ctx(hot={"by_norm": self._multi(),
                                          "freq": {"微博": 300, "抖音": 20}}))
        assert v.ok is None and v.status == ll.UNKNOWN
        assert "采样密度" in v.stat

    def test_样本不足就不下结论(self) -> None:
        v = ll._hypo_weibo_first(_ctx(hot={"by_norm": self._multi(pairs=5, weibo_first=5),
                                          "freq": {"微博": 300, "抖音": 300}}))
        assert v.ok is None and "5" in v.stat

    def test_微博并不更快时判被证伪(self) -> None:
        v = ll._hypo_weibo_first(_ctx(hot={"by_norm": self._multi(pairs=30, weibo_first=3),
                                          "freq": {"微博": 300, "抖音": 300}}))
        assert v.ok is False and v.status == ll.FAILED


class TestVerdicts:
    def test_没有样本一律证据不足(self) -> None:
        """★ 全模块的地基:**空数据不许推出任何结论**。"""
        v = ll._hypo_no_broken_segment(_ctx())
        assert v.ok is None and v.status == ll.UNKNOWN
        v = ll._hypo_move_rate(_ctx())
        assert v.ok is None
        v = ll._hypo_push_fast(_ctx())
        assert v.ok is None
        v = ll._hypo_douyin_first_resource(_ctx())
        assert v.ok is None

    def test_搬成率不足时点名是哪条链(self) -> None:
        v = ll._hypo_move_rate(_ctx(delivery={"chains": {
            "抖音": {"seen": 40, "move_rate": 0.8},
            "迅雷群": {"seen": 40, "move_rate": 0.4}}}))
        assert v.ok is False and "迅雷群" in v.if_false

    def test_只看近24h不看窗口历史(self) -> None:
        """窗口里坏过、近 24h 好了 ⇒ **不算**现在在坏(与交付看板同一条纪律)。"""
        v = ll._hypo_no_broken_segment(_ctx(delivery={
            "chains": {}, "current_24h": {"迅雷群": {"seen": 10, "moved": 10}}}))
        assert v.ok is True

    def test_每条主张都自带判据与行动建议(self) -> None:
        """台账是**决策工具**:不知道"所以该做什么"的结论等于没有。"""
        for fn in ll.HYPOTHESES:
            v = fn(_ctx())
            assert v.statement and v.judge, fn.__name__
            assert v.if_true and v.if_false, fn.__name__


class TestState:
    def test_首次评估全部记成变化(self, session, monkeypatch) -> None:
        monkeypatch.setattr(ll, "build_context", lambda *a, **k: _ctx())
        r = ll.evaluate(session, 1)
        assert len(r["changes"]) == len(ll.HYPOTHESES)
        assert all(c["from"] is None for c in r["changes"])

    def test_状态没变就不报变化(self, session, monkeypatch) -> None:
        """★ "学习"的载体是**变化**。每周把同一批结论念一遍,人就再也不看了。"""
        monkeypatch.setattr(ll, "build_context", lambda *a, **k: _ctx())
        ll.evaluate(session, 1)
        r2 = ll.evaluate(session, 1)
        assert r2["changes"] == []
        assert all(v["status"] == ll.UNKNOWN for v in r2["verdicts"])

    def test_状态翻转时留历史且重开_since(self, session, monkeypatch) -> None:
        ctx_ok = _ctx(delivery={"chains": {}, "current_24h": {"迅雷群": {"seen": 10, "moved": 10}}})
        ctx_bad = _ctx(delivery={"chains": {}, "current_24h": {"迅雷群": {"seen": 10, "moved": 2}}})
        monkeypatch.setattr(ll, "build_context", lambda *a, **k: ctx_ok)
        ll.evaluate(session, 1)
        monkeypatch.setattr(ll, "build_context", lambda *a, **k: ctx_bad)
        r = ll.evaluate(session, 1)
        ch = [c for c in r["changes"] if c["key"] == "no_broken_segment"]
        assert ch and ch[0]["from"] == ll.OK and ch[0]["to"] == ll.FAILED
        hist = r["state"]["no_broken_segment"]["history"]
        assert len(hist) == 2 and hist[-1]["to"] == ll.FAILED
        assert r["state"]["no_broken_segment"]["since"] >= hist[-1]["at"]

    def test_代码里下线的主张会被清掉(self, session, monkeypatch) -> None:
        """主张写在代码里 —— 删掉一条后库里那行若不清理,会一直显示"证据不足",
        看起来像"还在攒样本",其实那条主张已经不存在了。"""
        monkeypatch.setattr(ll, "build_context", lambda *a, **k: _ctx())
        ll.evaluate(session, 1)
        stale = session.query(SystemConfig).filter_by(key=ll.KEY).one()
        import json

        blob = json.loads(stale.value)
        blob["已经删掉的主张"] = {"status": ll.UNKNOWN, "n": 0}
        stale.value = json.dumps(blob, ensure_ascii=False)
        session.commit()
        ll.evaluate(session, 1)
        still = json.loads(session.query(SystemConfig).filter_by(key=ll.KEY).one().value)
        assert "已经删掉的主张" not in still

    def test_状态坏了能自愈但会说出来(self, session) -> None:
        session.add(SystemConfig(key=ll.KEY, value="{不是 json"))
        session.commit()
        assert ll._load_state(session) == {}


class TestReviewFlags:
    def test_低相似度组进复核队列(self) -> None:
        """⚠️ 它是**线索不是结论** —— 实测最低那组(0.084)人工核过确实是同一份资源,
        拿它当"匹配质量差"的判据会把真热门资源一律误报。"""
        ctx = _ctx(ordering={"groups": [
            {"name": "泛词种子", "members": 11, "min_pair_sim": 0.084},
            {"name": "干净的", "members": 2, "min_pair_sim": 1.0},
        ]})
        flags = ll.review_flags(ctx)
        assert len(flags) == 1 and "泛词种子" in flags[0]

    def test_没有可疑组就不打扰(self) -> None:
        assert ll.review_flags(_ctx()) == []
