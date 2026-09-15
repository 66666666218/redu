"""事件层单测:归一化/相似度/归并/跨平台/峰值/终结(纯逻辑+内存库)。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (BaiduHotItem, HotspotEvent, WeiboHotItem, XianyuItem)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(__import__("app.db.models", fromlist=["User"]).User(id=1, username="a", email="a@b.c", password_hash="x"))
    db.commit()
    yield db
    db.close()


class TestNormalizeAndSimilarity:
    def test_normalize_strips_noise(self):
        from app.services.events import normalize_title

        assert normalize_title("#某明星官宣结婚#") == "某明星官宣结婚"
        assert normalize_title("某明星官宣结婚上了热搜") == "某明星官宣结婚"
        assert normalize_title("  某明星，官宣！结婚  ") == "某明星官宣结婚"

    def test_variants_are_similar(self):
        from app.services.events import similarity

        # 同一事件的 4 种标题变体必须高相似
        base = "某明星官宣结婚"
        variants = ["某明星宣布结婚", "某明星官宣婚讯", "某明星正式结婚", "某明星晒结婚证"]
        for v in variants:
            assert similarity(base, v) >= 0.42, f"{v} 相似度不足"

    def test_unrelated_titles_not_similar(self):
        from app.services.events import similarity

        assert similarity("某明星官宣结婚", "新一轮降雨预报发布") < 0.42


class TestAssignTick:
    def _seed(self, db, title, heat, hours_ago=1, board="weibo"):
        ts = datetime.now() - timedelta(hours=hours_ago)
        if board == "weibo":
            db.add(WeiboHotItem(user_id=1, title=title, heat=heat, rank=1, captured_at=ts))
        elif board == "baidu":
            db.add(BaiduHotItem(user_id=1, title=title, heat=heat, rank=1, captured_at=ts))
        elif board == "xianyu":
            db.add(XianyuItem(user_id=1, item_id=f"t{int(ts.timestamp())}", title=title,
                              hit_keywords=1, best_rank=1, price="9.9", created_at=ts))
        db.commit()

    def test_variants_merge_into_one_event(self, session):
        from app.services import events

        # 增量扫描语义:快照都是"刚采集"的(分钟级),不能构造小时级历史时间戳
        # 最旧的先处理(决定事件主标题);都在增量窗口内(分钟级"刚采集")
        self._seed(session, "某明星官宣结婚", 1000, hours_ago=3 / 12)
        self._seed(session, "某明星宣布结婚", 1500, hours_ago=2 / 12)
        self._seed(session, "某明星官宣婚讯", 2200, hours_ago=1 / 12)
        out = events.assign_tick(session, 1)
        assert out["created"] == 1 and out["merged"] >= 2
        ev = session.scalar(select(HotspotEvent))
        assert ev.sample_count == 3
        assert ev.peak_value == 2200                       # 峰值追踪
        assert (datetime.now() - ev.peak_at).total_seconds() < 3600 + 5  # 峰值时刻=最新高点

    def test_cross_platform_counts(self, session):
        from app.services import events

        self._seed(session, "某明星官宣结婚", 1000, hours_ago=2, board="weibo")
        self._seed(session, "某明星官宣结婚", 800, hours_ago=1, board="baidu")
        self._seed(session, "某明星官宣结婚", 500, hours_ago=1, board="xianyu")
        events.assign_tick(session, 1)
        ev = session.scalar(select(HotspotEvent))
        assert ev.platform_count == 3                      # 跨平台共振的架构基础

    def test_event_ends_after_silence(self, session):
        """事件先被创建(active),词掉榜 72h 无新快照 → 下一轮 tick 置 ended。"""
        from app.services import events

        self._seed(session, "已经凉透的旧热点词", 100, hours_ago=1)
        events.assign_tick(session, 1)
        ev = session.scalar(select(HotspotEvent))
        assert ev.status == "active"                       # 创建时活跃
        ev.last_seen = datetime.now() - timedelta(hours=73)  # 模拟时间流逝:73h 无新快照
        session.commit()
        events.assign_tick(session, 1)
        ev = session.scalar(select(HotspotEvent))
        assert ev.status == "ended" and ev.ended_at is not None

    def test_rerun_is_idempotent(self, session):
        """重复跑归属不新建事件、不重复计样本(调度每 15 分钟一次,幂等必须保证)。"""
        from app.services import events

        self._seed(session, "某明星官宣结婚", 1000, hours_ago=2)
        events.assign_tick(session, 1)
        first = session.scalar(select(HotspotEvent)).sample_count
        events.assign_tick(session, 1)                     # 同一窗口再跑
        rows = session.scalars(select(HotspotEvent)).all()
        assert len(rows) == 1
        assert rows[0].sample_count == first               # 样本数不涨(本轮无新快照)


def test_reignition_reactivates_ended_event(session):
    """复燃:已终结事件(7 天内)重现 → 重激活+复燃计数,而非新建事件。"""
    from app.services import events

    now = datetime.now()
    db = session
    db.add(WeiboHotItem(user_id=1, title="某综艺陷争议风波", heat=500, rank=2,
                        captured_at=now - timedelta(minutes=1)))  # 刚采集(增量扫描语义)
    db.commit()
    events.assign_tick(session, 1)
    ev = session.scalar(select(HotspotEvent))
    assert ev.status == "active"
    ev.status, ev.ended_at = "ended", now - timedelta(hours=20)  # 已终结 20h(7 天内)
    ev.last_seen = now - timedelta(hours=74)
    session.commit()

    self2 = events.assign_tick(session, 1)
    ev = session.scalar(select(HotspotEvent))
    assert ev.status == "active" and ev.reappear_count == 1  # 复燃
    assert self2["created"] == 0                              # 没有新建重复事件


def test_last_growth_recorded(session):
    """增长率事实:同一词两次快照 → last_growth = 环比。"""
    from app.services import events

    now = datetime.now()
    db = session
    db.add(WeiboHotItem(user_id=1, title="某新能源车大定破纪录", heat=1000, rank=1,
                        captured_at=now - timedelta(hours=2)))
    db.add(WeiboHotItem(user_id=1, title="某新能源车大定破纪录", heat=1600, rank=1,
                        captured_at=now - timedelta(hours=1)))
    db.commit()
    events.assign_tick(session, 1)
    ev = session.scalar(select(HotspotEvent))
    assert ev.last_growth == pytest.approx(0.6)
