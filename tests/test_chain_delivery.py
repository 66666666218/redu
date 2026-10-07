"""交付看板(「看到 → 搬成 → 推出」)单测(2026-10-07)。

这里盯的是**两类会产出"看着很合理"的假数据**的坑,不是功能有没有跑通:
  · 给**存量行**补打"现在"的 `moved_at`(upsert 每轮都会重走一遍)⇒ 搬成耗时=发布到现在,编的;
  · 把**迁移回填的 `pushed_at`**(= created_at)当成真时刻 ⇒ 时延恒 0,而 0 像"极快"。
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
from app.db.models import (  # noqa: E402
    User, WechatArticle, WechatPanLink, XunleiGroupShare,
)
from app.services import chain_delivery as cd  # noqa: E402

NOW = datetime.now()
_SEQ = [0]


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _group(db, title: str, hours_ago: float, status: str = "ok",
           our_url: str = "", moved_at=None, message: str = "") -> None:
    _SEQ[0] += 1
    db.add(XunleiGroupShare(user_id=1, group_id="g1", share_id=f"s{_SEQ[0]}",
                            title=title, status=status, our_url=our_url,
                            message=message, moved_at=moved_at,
                            msg_time=NOW - timedelta(hours=hours_ago),
                            synced_at=NOW))
    db.commit()


def _article(db, title: str, published_h: float, pushed_at, links: int = 1,
             ingested_h: float | None = None) -> None:
    """`published_h` = 发布距今几小时;`ingested_h` = **入库**距今几小时。

    ⚠️ 两者**必须能分开设**:公众号最常见的形态就是"发布很久了、我们刚刚才发现"
    (实测 发布→入库 中位几十小时),把它们绑在一起就测不出"晚发现"这件事。
    """
    ing = published_h - 1 if ingested_h is None else ingested_h
    art = WechatArticle(user_id=1, title=title,
                        publish_at=NOW - timedelta(hours=published_h),
                        created_at=NOW - timedelta(hours=ing),
                        pushed_at=pushed_at)
    db.add(art)
    db.commit()
    for i in range(links):
        db.add(WechatPanLink(user_id=1, article_id=art.id, pan_url=f"http://p/{art.id}/{i}",
                             created_at=art.created_at))
    db.commit()


class TestMarkNewlyMoved:
    """★ 这一列的唯一作用就是"测得准";测得不准比没有更糟(它会变成一句有说服力的错话)。"""

    class _Row:
        moved_at = None

    def test_首次搬成会打戳(self) -> None:
        r = self._Row()
        cd.mark_newly_moved(r, had_link=False, now=NOW)
        assert r.moved_at == NOW

    def test_此前已有链就不许打戳(self) -> None:
        """⚠️ **本模块最要紧的一条**:存量行(有链、没戳)在 upsert 重走一遍时,
        如果不判 `had_link` 就会被打上"现在" ⇒ 算出"搬成用了 96 小时",而那是编的。"""
        r = self._Row()
        cd.mark_newly_moved(r, had_link=True, now=NOW)
        assert r.moved_at is None

    def test_已有戳不覆盖(self) -> None:
        r = self._Row()
        r.moved_at = NOW - timedelta(hours=5)
        cd.mark_newly_moved(r, had_link=False, now=NOW)
        assert r.moved_at == NOW - timedelta(hours=5)      # 记的是**首次**


class TestFmt:
    def test_小数值不会被四舍五入抹掉(self) -> None:
        """实测"入库→推出"中位 0.043h —— 打成 `0.0h` 会被读成"瞬间/没测到"。"""
        assert cd.fmt_h(0.043) == "3 分钟"
        assert cd.fmt_h(21.6) == "21.6h"
        assert cd.fmt_h(72.0) == "3.0 天"
        assert cd.fmt_h(None) == "还没样本"


class TestWechat:
    def test_一篇文章挂多条链只算一篇(self, session) -> None:
        """实测一篇挂了 14 条链 ⇒ 不去重会算出"推出 308 > 采到 260"(118%)。"""
        _article(session, "多链资源", 30, pushed_at=NOW - timedelta(hours=29), links=5)
        _article(session, "单链资源", 20, pushed_at=NOW - timedelta(hours=19), links=1)
        out = cd._wechat(session, 1, NOW - timedelta(days=14))
        assert out["seen"] == 2 and out["pushed"] == 2

    def test_回填的pushed_at只算推过不算时延(self, session) -> None:
        """⚠️ 迁移为防历史文章被补推刷屏,把存量行的 `pushed_at` 直接写成了 `created_at`。
        拿它算时延必然得 0.0h —— 而那个 0 长得像"极快"。"""
        _article(session, "迁移前的老文", 40, pushed_at=None)
        old = session.query(WechatArticle).filter_by(title="迁移前的老文").one()
        old.pushed_at = old.created_at                       # 迁移回填的样子
        session.commit()
        _article(session, "真的推过的", 3, pushed_at=NOW)     # 2 小时前入库、刚刚推
        out = cd._wechat(session, 1, NOW - timedelta(days=14))
        assert out["seen"] == 2
        assert out["pushed"] == 2          # 两篇都"推过"
        assert out["backfilled"] == 1      # 但只有一篇能算时延
        assert out["latency_n"] == 1

    def test_发布到推出与入库到推出分开算(self, session) -> None:
        """前者是真正的端到端(含我们**发现得晚**),后者只量我们自己的手脚。"""
        # 发布在 100 小时前,但我们 **2 小时前才入库**,此刻推出
        _article(session, "晚发现的老文", 100, pushed_at=NOW, ingested_h=2)
        out = cd._wechat(session, 1, NOW - timedelta(days=14))
        assert out["publish_to_push_h"] == pytest.approx(100.0, abs=0.01)  # 端到端很久
        assert out["ingest_to_push_h"] == pytest.approx(2.0, abs=0.01)     # 拿到后推得快


class TestReasons:
    def test_skipped不算失败(self, session) -> None:
        """`skipped` 是我们**主动不搬**(盘满暂停/自己的分享/太泛的包名),
        算进失败率会把"自己决定不搬"读成"搬不动"。"""
        _group(session, "盘满没搬的", 3, status="skipped", message="盘满暂停")
        _group(session, "真失败的", 3, status="failed", message="迅雷刷新 token 失败")
        rep = cd.delivery_report(session, 1, days=14)
        rs = rep["current_24h"]["迅雷群"]["fail_reasons"]
        assert [r["reason"] for r in rs] == ["迅雷刷新 token 失败"]


class TestBottleneck:
    def test_窗口里的历史失败不算当前瓶颈(self, session) -> None:
        """★ 做这一层时差点栽在这:近 7 天聚合显示"失败率 35%",看着像一直在坏,
        实际是窗口含了 10-03~05 那段(迅雷 token 失效),10-06 起已归零。
        ⇒ 判定**只看当前 24h**。"""
        for i in range(6):                                   # 5 天前的历史失败
            _group(session, f"老失败{i}", 120, status="failed", message="迅雷刷新 token 失败")
        for i in range(8):                                   # 近 24h 全好
            _group(session, f"今天成的{i}", 2, status="ok", our_url=f"http://x/{i}")
        rep = cd.delivery_report(session, 1, days=14)
        assert "仍在失败" not in rep["bottleneck"]
        assert rep["chains"]["迅雷群"]["fail_reasons"]        # 窗口聚合里**仍然**能看到它们
        assert rep["current_24h"]["迅雷群"]["fail_reasons"] == []

    def test_当前真在坏就报出来(self, session) -> None:
        for i in range(4):
            _group(session, f"现在坏的{i}", 2, status="failed", message="盘满暂停")
        for i in range(4):
            _group(session, f"现在成的{i}", 2, status="ok", our_url=f"http://y/{i}")
        assert "仍在失败" in cd.delivery_report(session, 1, days=14)["bottleneck"]


class TestRecommend:
    def test_样本不足时不给调度建议(self) -> None:
        """样本少的时候给建议 = 假信号。宁可说"等攒够"。"""
        out = cd.recommend_cadence({"days": 14}, {"groups": [{"name": "x", "order": ["抖音"],
                                                             "lag_h": 1.0}]})
        assert "样本只有" in out[0] and "假信号" in out[0]

    def test_全部搬成时不该报失败(self, session) -> None:
        for i in range(8):
            _group(session, f"全成{i}", 2, status="ok", our_url=f"http://z/{i}")
        assert "仍在失败" not in cd.delivery_report(session, 1, days=14)["bottleneck"]
